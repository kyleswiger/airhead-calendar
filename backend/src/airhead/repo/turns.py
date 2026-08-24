"""AgentTurn persistence — the M2 audit log (PRD §7, docs/M2-CONTRACT.md).

    PK = HH#<hh>   SK = TURN#<createdAt>#<turnId>    the audit record
                   SK = CONV#<conversationId>        pointer -> the head turn's SK

`<createdAt>` is `airhead.repo.encode_instant`, so the sort key is fixed width and a
descending query is genuinely "newest first" — the microsecond padding is what keeps
":00Z" from sorting after ":00.5Z".

*The pointer.* Continuing a conversation needs its most recent turn, and the sort key
embeds a timestamp the caller does not have. Reading the household's whole `TURN#`
partition to find it would grow with the audit log forever, so the same two-GetItem
pointer trade the event repo makes is bought here too.

*The TTL.* `ttl` is **epoch seconds**. DynamoDB silently ignores an ISO string, and the
failure mode is a table that quietly never expires anything. Both backends compute it
the same way, and both filter expired rows on read: DynamoDB's sweep runs up to 48h
late, so "expired" has to mean the same thing in the application either way.

Nothing here imports boto3 at module scope — `airhead.api.deps` imports `TurnRepo` for
its type, and a Pi-only deployment must not pay for an AWS SDK it never calls.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from airhead.history import sanitize_history, to_wire_message
from airhead.repo import decode_instant, encode_instant
from airhead.repo.base import RepoError

log = logging.getLogger("airhead.repo.turns")

# PRD §7. Long enough to answer "what did it do last quarter", short enough that a
# household's transcript is not kept indefinitely.
TURN_TTL = timedelta(days=90)

DEFAULT_TABLE = "airhead"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# `ToolOutcome.status` for a call the harness gated. Per the contract these are recorded
# but never reported as actions on the wire: nothing was applied.
PENDING_STATUS = "pending_confirmation"


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --- records ------------------------------------------------------------------


@dataclass(slots=True)
class ToolCall:
    """One tool invocation and what came of it. Mirrors `runner.ToolOutcome`."""

    tool: str
    status: str  # "ok" | "error" | "pending_confirmation"
    event_id: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class PendingCall:
    """The gate a turn stopped on. Mirrors `runner.PendingConfirmation`."""

    call_id: str
    tool: str
    summary: str
    event_id: str | None = None
    # The gated call's original arguments, verbatim — what an approval replays.
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(slots=True)
class AgentTurn:
    """One request/response round trip, and everything needed to audit or continue it.

    `history` is the model's context for the next turn. It is written by the runner and
    read back by the route; a client never sends or sees it, which is the point — a
    rewritten history is a rewritten set of instructions.
    """

    turn_id: str
    household_id: str
    conversation_id: str
    actor_member_id: str
    created_at: datetime
    message: str
    reply: str | None = None
    actions: list[ToolCall] = field(default_factory=list)
    pending: PendingCall | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    usage: TurnUsage = field(default_factory=TurnUsage)
    # The gate this turn answered, if any. Audit-only: replay protection comes from the
    # gate living on the head turn, which answering it replaces.
    confirmed_call_id: str | None = None

    def expires_at(self) -> int:
        return int((self.created_at + TURN_TTL).timestamp())


@runtime_checkable
class TurnRepo(Protocol):
    def put(self, turn: AgentTurn) -> AgentTurn:
        """Append a turn and make it the conversation's head."""
        ...

    def latest(self, household_id: str, conversation_id: str) -> AgentTurn | None:
        """The most recent turn of a conversation, or None if there is none.

        This is the only carrier of a pending confirmation, so it is also the thing
        that makes a confirmation single-use.
        """
        ...

    def list_recent(
        self, household_id: str, *, member_id: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[AgentTurn]:
        """Newest first. `member_id` narrows to one actor's turns."""
        ...

    def purge_expired(self, household_id: str, *, now: datetime | None = None) -> int:
        """Drop turns past their TTL. Returns how many rows were removed."""
        ...


# --- shared codec -------------------------------------------------------------


def _action_dicts(turn: AgentTurn) -> list[dict[str, Any]]:
    return [
        {"tool": a.tool, "status": a.status, "eventId": a.event_id, "detail": a.detail}
        for a in turn.actions
    ]


def _actions_from(raw: Any) -> list[ToolCall]:
    return [
        ToolCall(
            tool=str(a["tool"]),
            status=str(a["status"]),
            event_id=a.get("eventId"),
            detail=a.get("detail"),
        )
        for a in (raw or [])
    ]


def _pending_dict(turn: AgentTurn) -> dict[str, Any] | None:
    if turn.pending is None:
        return None
    return {
        "callId": turn.pending.call_id,
        "tool": turn.pending.tool,
        "summary": turn.pending.summary,
        "eventId": turn.pending.event_id,
        # A JSON string for the same reason as `historyJson`: the args are tool
        # input of arbitrary shape, and DynamoDB's Map coercion must not touch it.
        "argsJson": json.dumps(turn.pending.args, sort_keys=True, default=str),
    }


def _pending_from(raw: Any) -> PendingCall | None:
    if not raw:
        return None
    return PendingCall(
        call_id=str(raw["callId"]),
        tool=str(raw["tool"]),
        summary=str(raw["summary"]),
        event_id=raw.get("eventId"),
        args=json.loads(raw.get("argsJson") or "{}"),
    )


def _history_json(turn: AgentTurn) -> str:
    """The conversation history, as JSON that is still valid history when read back.

    Every entry is normalized to plain wire dicts first. `default=str` stays only as
    a crash guard for something genuinely unexpected — it must never be what makes a
    content block serializable, because what it produces is a `repr()` string that
    the model rejects on the next turn (issue #5).
    """
    return json.dumps([to_wire_message(m) for m in (turn.history or [])], default=str)


def _history_from(raw: Any, conversation_id: str | None = None) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(raw or "[]")
    except (TypeError, ValueError):
        log.warning("turn_history_unreadable", extra={"conversation_id": conversation_id})
        return []
    return sanitize_history(loaded, conversation_id=conversation_id)


def _usage_dict(turn: AgentTurn) -> dict[str, int]:
    return {
        "inputTokens": int(turn.usage.input_tokens),
        "outputTokens": int(turn.usage.output_tokens),
        "cacheReadInputTokens": int(turn.usage.cache_read_input_tokens),
    }


def _usage_from(raw: Any) -> TurnUsage:
    raw = raw or {}
    # DynamoDB hands numbers back as Decimal; the domain wants plain ints.
    return TurnUsage(
        input_tokens=int(raw.get("inputTokens") or 0),
        output_tokens=int(raw.get("outputTokens") or 0),
        cache_read_input_tokens=int(raw.get("cacheReadInputTokens") or 0),
    )


def _sk(created_at: datetime, turn_id: str) -> str:
    return f"TURN#{encode_instant(created_at)}#{turn_id}"


def _conv_sk(conversation_id: str) -> str:
    return f"CONV#{conversation_id}"


def _pk(household_id: str) -> str:
    return f"HH#{household_id}"


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


# --- DynamoDB -----------------------------------------------------------------


@contextmanager
def _translate_dynamo() -> Iterator[None]:
    # Imported inside the guard so the module stays importable without botocore.
    from botocore.exceptions import ClientError

    try:
        yield
    except ClientError as exc:  # Nothing above the repo layer may see a botocore type.
        raise RepoError(str(exc)) from exc


class DynamoTurnRepo:
    def __init__(
        self,
        table_name: str | None = None,
        *,
        resource: Any = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.table_name = table_name or os.environ.get("AIRHEAD_TABLE", DEFAULT_TABLE)
        self._resource = resource
        self._clock = clock
        self._table: Any = None

    @property
    def table(self) -> Any:
        # Built lazily so importing this module never needs credentials, and so a moto
        # mock entered after construction is the one that gets used.
        if self._table is None:
            import boto3

            resource = self._resource or boto3.resource("dynamodb")
            self._table = resource.Table(self.table_name)
        return self._table

    def put(self, turn: AgentTurn) -> AgentTurn:
        item = _turn_to_item(turn)
        with _translate_dynamo():
            self.table.put_item(Item=item)
            self.table.put_item(
                Item={
                    "PK": _pk(turn.household_id),
                    "SK": _conv_sk(turn.conversation_id),
                    "entity": "conversationHead",
                    "householdId": turn.household_id,
                    "conversationId": turn.conversation_id,
                    "actorMemberId": turn.actor_member_id,
                    "targetSk": item["SK"],
                    "ttl": turn.expires_at(),
                }
            )
        return turn

    def latest(self, household_id: str, conversation_id: str) -> AgentTurn | None:
        with _translate_dynamo():
            head = self.table.get_item(
                Key={"PK": _pk(household_id), "SK": _conv_sk(conversation_id)}
            ).get("Item")
            if not head:
                return None
            item = self.table.get_item(Key={"PK": _pk(household_id), "SK": head["targetSk"]}).get(
                "Item"
            )
        if not item or not self._live(item):
            return None
        return _item_to_turn(item)

    def list_recent(
        self, household_id: str, *, member_id: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[AgentTurn]:
        from boto3.dynamodb.conditions import Key

        want = _clamp(limit)
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(_pk(household_id))
            & Key("SK").begins_with("TURN#"),
            "ScanIndexForward": False,  # newest first; the SK is a fixed-width instant
            "Limit": want,
        }
        found: list[AgentTurn] = []
        with _translate_dynamo():
            while len(found) < want:
                response = self.table.query(**kwargs)
                for item in response.get("Items", []):
                    if not self._live(item):
                        continue
                    if member_id is not None and item.get("actorMemberId") != member_id:
                        continue
                    found.append(_item_to_turn(item))
                    if len(found) == want:
                        break
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                # A member filter is applied after the read, so a page can come back
                # empty of matches; keep walking rather than short-changing the caller.
                kwargs["ExclusiveStartKey"] = last_key
        return found

    def purge_expired(self, household_id: str, *, now: datetime | None = None) -> int:
        """DynamoDB sweeps expired items itself; reads already ignore them."""
        del household_id, now
        return 0

    def _live(self, item: dict[str, Any]) -> bool:
        ttl = item.get("ttl")
        return ttl is None or int(ttl) > int(self._clock().timestamp())


def _turn_to_item(turn: AgentTurn) -> dict[str, Any]:
    item: dict[str, Any] = {
        "PK": _pk(turn.household_id),
        "SK": _sk(turn.created_at, turn.turn_id),
        "entity": "agentTurn",
        "householdId": turn.household_id,
        "turnId": turn.turn_id,
        "conversationId": turn.conversation_id,
        "actorMemberId": turn.actor_member_id,
        "createdAt": encode_instant(turn.created_at),
        "message": turn.message,
        "actions": _action_dicts(turn),
        # A JSON string, not a Map: history is whatever shape the SDK's message list
        # takes, and a nested Map would have to survive DynamoDB's type coercion
        # (floats, empty sets) on every round trip.
        "historyJson": _history_json(turn),
        "usage": _usage_dict(turn),
        "ttl": turn.expires_at(),
    }
    if turn.reply is not None:
        item["reply"] = turn.reply
    pending = _pending_dict(turn)
    if pending is not None:
        item["pending"] = pending
    if turn.confirmed_call_id is not None:
        item["confirmedCallId"] = turn.confirmed_call_id
    return item


def _item_to_turn(item: dict[str, Any]) -> AgentTurn:
    return AgentTurn(
        turn_id=item["turnId"],
        household_id=item["householdId"],
        conversation_id=item["conversationId"],
        actor_member_id=item["actorMemberId"],
        created_at=decode_instant(item["createdAt"]),
        message=item.get("message", ""),
        reply=item.get("reply"),
        actions=_actions_from(item.get("actions")),
        pending=_pending_from(item.get("pending")),
        history=_history_from(item.get("historyJson"), item.get("conversationId")),
        usage=_usage_from(item.get("usage")),
        confirmed_call_id=item.get("confirmedCallId"),
    )


# --- SQLite -------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_turns (
    household_id      TEXT NOT NULL,
    turn_id           TEXT NOT NULL,
    conversation_id   TEXT NOT NULL,
    actor_member_id   TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    message           TEXT NOT NULL,
    reply             TEXT,
    actions           TEXT NOT NULL,
    pending           TEXT,
    history           TEXT NOT NULL,
    usage             TEXT NOT NULL,
    confirmed_call_id TEXT,
    ttl               INTEGER NOT NULL,
    PRIMARY KEY (household_id, turn_id)
);

-- Mirrors the DynamoDB sort key: the audit read is "one household, newest first", and
-- the conversation index answers "the head turn of this conversation" without a scan.
CREATE INDEX IF NOT EXISTS idx_turns_recent
    ON agent_turns (household_id, created_at, turn_id);
CREATE INDEX IF NOT EXISTS idx_turns_conversation
    ON agent_turns (household_id, conversation_id, created_at, turn_id);
"""


@contextmanager
def _translate_sqlite() -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as exc:  # Nothing above the repo layer may see a sqlite3 type.
        raise RepoError(str(exc)) from exc


class SqliteTurnRepo:
    """Not a test double: pytest and a Pi-only deployment both run against this."""

    def __init__(
        self,
        conn_or_path: sqlite3.Connection | str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if isinstance(conn_or_path, sqlite3.Connection):
            self.conn = conn_or_path
            self.conn.row_factory = sqlite3.Row
        else:
            from airhead.repo.sqlite import connect

            self.conn = connect(conn_or_path)
        self._clock = clock
        # Owned by this module rather than `sqlite.connect`, so the turn log can be
        # added to an existing database without a migration step.
        with _translate_sqlite():
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    def put(self, turn: AgentTurn) -> AgentTurn:
        with _translate_sqlite():
            self.conn.execute(
                """
                INSERT OR REPLACE INTO agent_turns (
                    household_id, turn_id, conversation_id, actor_member_id, created_at,
                    message, reply, actions, pending, history, usage, confirmed_call_id, ttl
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    turn.household_id,
                    turn.turn_id,
                    turn.conversation_id,
                    turn.actor_member_id,
                    encode_instant(turn.created_at),
                    turn.message,
                    turn.reply,
                    json.dumps(_action_dicts(turn)),
                    json.dumps(_pending_dict(turn)) if turn.pending else None,
                    _history_json(turn),
                    json.dumps(_usage_dict(turn)),
                    turn.confirmed_call_id,
                    turn.expires_at(),
                ),
            )
            self.conn.commit()
        return replace(turn)

    def latest(self, household_id: str, conversation_id: str) -> AgentTurn | None:
        with _translate_sqlite():
            row = self.conn.execute(
                "SELECT * FROM agent_turns "
                "WHERE household_id = ? AND conversation_id = ? AND ttl > ? "
                "ORDER BY created_at DESC, turn_id DESC LIMIT 1",
                (household_id, conversation_id, self._now_epoch()),
            ).fetchone()
        return _row_to_turn(row) if row else None

    def list_recent(
        self, household_id: str, *, member_id: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[AgentTurn]:
        sql = ["SELECT * FROM agent_turns WHERE household_id = ? AND ttl > ?"]
        args: list[object] = [household_id, self._now_epoch()]
        if member_id is not None:
            sql.append("AND actor_member_id = ?")
            args.append(member_id)
        sql.append("ORDER BY created_at DESC, turn_id DESC LIMIT ?")
        args.append(_clamp(limit))
        with _translate_sqlite():
            rows = self.conn.execute(" ".join(sql), args).fetchall()
        return [_row_to_turn(r) for r in rows]

    def purge_expired(self, household_id: str, *, now: datetime | None = None) -> int:
        cutoff = int((now or self._clock()).timestamp())
        with _translate_sqlite():
            cursor = self.conn.execute(
                "DELETE FROM agent_turns WHERE household_id = ? AND ttl <= ?",
                (household_id, cutoff),
            )
            self.conn.commit()
        return cursor.rowcount

    def _now_epoch(self) -> int:
        return int(self._clock().timestamp())


def _row_to_turn(row: sqlite3.Row) -> AgentTurn:
    return AgentTurn(
        turn_id=row["turn_id"],
        household_id=row["household_id"],
        conversation_id=row["conversation_id"],
        actor_member_id=row["actor_member_id"],
        created_at=decode_instant(row["created_at"]),
        message=row["message"],
        reply=row["reply"],
        actions=_actions_from(json.loads(row["actions"])),
        pending=_pending_from(json.loads(row["pending"]) if row["pending"] else None),
        history=_history_from(row["history"], row["conversation_id"]),
        usage=_usage_from(json.loads(row["usage"])),
        confirmed_call_id=row["confirmed_call_id"],
    )
