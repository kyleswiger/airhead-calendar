"""AgentTurn persistence, held to one contract across both backends.

Same shape as `test_repo_contract.py`: every test runs once against SQLite and once
against DynamoDB-on-moto, because a turn written by one has to be readable by the
other and the audit log is the thing that has to survive a backend swap intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from airhead.repo.sqlite import connect
from airhead.repo.turns import (
    TURN_TTL,
    AgentTurn,
    DynamoTurnRepo,
    PendingCall,
    SqliteTurnRepo,
    ToolCall,
    TurnRepo,
    TurnUsage,
)
from conftest import TEST_TABLE

HOUSEHOLD = "hh_1"
OTHER_HOUSEHOLD = "hh_2"
NOW = datetime(2026, 8, 5, 14, 30, tzinfo=UTC)


@dataclass(frozen=True)
class Backend:
    turns: TurnRepo
    name: str
    raw: Any  # sqlite3.Connection | dynamodb.Table, for the TTL attribute assertions


@pytest.fixture(params=["sqlite", "dynamo"])
def backend(request: pytest.FixtureRequest):
    if request.param == "sqlite":
        conn = connect(":memory:")
        yield Backend(turns=SqliteTurnRepo(conn), name="sqlite", raw=conn)
        conn.close()
    else:
        resource = request.getfixturevalue("dynamo_resource")
        yield Backend(
            turns=DynamoTurnRepo(TEST_TABLE, resource=resource),
            name="dynamo",
            raw=resource.Table(TEST_TABLE),
        )


def make_turn(
    turn_id: str,
    *,
    household_id: str = HOUSEHOLD,
    conversation_id: str = "cnv_1",
    actor: str = "mem_alex",
    created_at: datetime = NOW,
    message: str = "add soccer thursday at 4",
    reply: str | None = "Added soccer practice Thursday at 4:00 PM.",
    actions: list[ToolCall] | None = None,
    pending: PendingCall | None = None,
    history: list[dict[str, Any]] | None = None,
    usage: TurnUsage | None = None,
    confirmed_call_id: str | None = None,
) -> AgentTurn:
    return AgentTurn(
        turn_id=turn_id,
        household_id=household_id,
        conversation_id=conversation_id,
        actor_member_id=actor,
        created_at=created_at,
        message=message,
        reply=reply,
        actions=actions if actions is not None else [],
        pending=pending,
        history=history if history is not None else [],
        usage=usage or TurnUsage(),
        confirmed_call_id=confirmed_call_id,
    )


def ttl_values(backend: Backend) -> list[int]:
    """Every stored `ttl`, as the store actually holds it."""
    if backend.name == "sqlite":
        rows = backend.raw.execute("SELECT ttl FROM agent_turns").fetchall()
        return [r["ttl"] for r in rows]
    items = backend.raw.scan().get("Items", [])
    return [item["ttl"] for item in items if item.get("entity") == "agentTurn"]


class TestRoundTrip:
    def test_every_field_survives(self, backend):
        turn = make_turn(
            "turn_1",
            actions=[
                ToolCall(tool="create_event", status="ok", event_id="evt_1"),
                ToolCall(tool="delete_event", status="error", detail="not found"),
            ],
            history=[
                {"role": "user", "content": "add soccer thursday at 4"},
                {"role": "assistant", "content": [{"type": "text", "text": "Added."}]},
            ],
            usage=TurnUsage(input_tokens=1200, output_tokens=80, cache_read_input_tokens=980),
            confirmed_call_id="call_prev",
        )
        backend.turns.put(turn)

        stored = backend.turns.latest(HOUSEHOLD, "cnv_1")
        assert stored is not None
        assert stored.turn_id == "turn_1"
        assert stored.actor_member_id == "mem_alex"
        assert stored.created_at == NOW
        assert stored.message == "add soccer thursday at 4"
        assert stored.reply == "Added soccer practice Thursday at 4:00 PM."
        assert [(a.tool, a.status, a.event_id) for a in stored.actions] == [
            ("create_event", "ok", "evt_1"),
            ("delete_event", "error", None),
        ]
        assert stored.actions[1].detail == "not found"
        assert stored.pending is None
        assert stored.history[1]["content"][0]["text"] == "Added."
        assert stored.usage == TurnUsage(
            input_tokens=1200, output_tokens=80, cache_read_input_tokens=980
        )
        assert stored.confirmed_call_id == "call_prev"

    def test_pending_confirmation_survives(self, backend):
        backend.turns.put(
            make_turn(
                "turn_1",
                reply=None,
                pending=PendingCall(
                    call_id="call_a",
                    tool="delete_event",
                    summary='Delete "Soccer practice" on Thursday at 4:00 PM?',
                    event_id="evt_1",
                    args={"event_id": "evt_1", "all_day": False},
                ),
                actions=[ToolCall(tool="delete_event", status="pending_confirmation")],
            )
        )
        stored = backend.turns.latest(HOUSEHOLD, "cnv_1")
        assert stored is not None
        assert stored.reply is None
        assert stored.pending is not None
        assert stored.pending.call_id == "call_a"
        assert stored.pending.event_id == "evt_1"
        # The stored arguments are what an approval replays — types intact.
        assert stored.pending.args == {"event_id": "evt_1", "all_day": False}
        # A gated call is still recorded: the audit log says what was attempted.
        assert stored.actions[0].status == "pending_confirmation"

    def test_usage_reads_back_as_int(self, backend):
        # DynamoDB hands numbers back as Decimal, which is not what the wire model wants.
        backend.turns.put(make_turn("turn_1", usage=TurnUsage(input_tokens=5)))
        stored = backend.turns.latest(HOUSEHOLD, "cnv_1")
        assert type(stored.usage.input_tokens) is int


class TestLatest:
    def test_returns_the_newest_turn(self, backend):
        backend.turns.put(make_turn("turn_1", created_at=NOW))
        backend.turns.put(make_turn("turn_2", created_at=NOW + timedelta(seconds=30)))
        backend.turns.put(make_turn("turn_3", created_at=NOW + timedelta(seconds=60)))
        assert backend.turns.latest(HOUSEHOLD, "cnv_1").turn_id == "turn_3"

    def test_conversations_are_isolated(self, backend):
        backend.turns.put(make_turn("turn_1", conversation_id="cnv_a"))
        backend.turns.put(
            make_turn("turn_2", conversation_id="cnv_b", created_at=NOW + timedelta(seconds=30))
        )
        assert backend.turns.latest(HOUSEHOLD, "cnv_a").turn_id == "turn_1"
        assert backend.turns.latest(HOUSEHOLD, "cnv_b").turn_id == "turn_2"

    def test_unknown_conversation_is_none(self, backend):
        assert backend.turns.latest(HOUSEHOLD, "cnv_nope") is None

    def test_other_household_is_none(self, backend):
        backend.turns.put(make_turn("turn_1"))
        assert backend.turns.latest(OTHER_HOUSEHOLD, "cnv_1") is None


class TestListRecent:
    def test_newest_first(self, backend):
        for i in range(3):
            backend.turns.put(make_turn(f"turn_{i}", created_at=NOW + timedelta(minutes=i)))
        assert [t.turn_id for t in backend.turns.list_recent(HOUSEHOLD)] == [
            "turn_2",
            "turn_1",
            "turn_0",
        ]

    def test_limit(self, backend):
        for i in range(5):
            backend.turns.put(make_turn(f"turn_{i}", created_at=NOW + timedelta(minutes=i)))
        assert len(backend.turns.list_recent(HOUSEHOLD, limit=2)) == 2

    def test_member_filter(self, backend):
        backend.turns.put(make_turn("turn_alex", actor="mem_alex"))
        backend.turns.put(
            make_turn("turn_riley", actor="mem_riley", created_at=NOW + timedelta(minutes=1))
        )
        rows = backend.turns.list_recent(HOUSEHOLD, member_id="mem_riley")
        assert [t.turn_id for t in rows] == ["turn_riley"]

    def test_member_filter_looks_past_a_full_page_of_other_actors(self, backend):
        # The filter is applied after the read on DynamoDB, so a naive Limit would
        # return nothing here even though a match exists further back.
        backend.turns.put(make_turn("turn_old", actor="mem_riley", created_at=NOW))
        for i in range(1, 6):
            backend.turns.put(
                make_turn(f"turn_{i}", actor="mem_alex", created_at=NOW + timedelta(minutes=i))
            )
        rows = backend.turns.list_recent(HOUSEHOLD, member_id="mem_riley", limit=3)
        assert [t.turn_id for t in rows] == ["turn_old"]

    def test_other_household_is_excluded(self, backend):
        backend.turns.put(make_turn("turn_1"))
        backend.turns.put(make_turn("turn_2", household_id=OTHER_HOUSEHOLD))
        assert [t.turn_id for t in backend.turns.list_recent(HOUSEHOLD)] == ["turn_1"]


class TestTtl:
    def test_ttl_is_epoch_seconds_ninety_days_out(self, backend):
        # An ISO string here is the silent bug: DynamoDB ignores a non-numeric `ttl`
        # and the rows then live forever.
        backend.turns.put(make_turn("turn_1"))
        (ttl,) = ttl_values(backend)
        assert not isinstance(ttl, str)  # not "2026-11-03T14:30:00.000000Z"
        assert int(ttl) == int((NOW + TURN_TTL).timestamp())

    def test_ttl_is_ninety_days(self, backend):
        assert timedelta(days=90) == TURN_TTL

    def test_expired_turns_are_not_returned(self, backend):
        # DynamoDB's sweep runs up to 48h late, so both backends filter on read.
        stale = datetime.now(UTC) - timedelta(days=100)
        backend.turns.put(make_turn("turn_old", created_at=stale))
        assert backend.turns.latest(HOUSEHOLD, "cnv_1") is None
        assert backend.turns.list_recent(HOUSEHOLD) == []

    def test_live_turns_are_returned(self, backend):
        backend.turns.put(make_turn("turn_new", created_at=datetime.now(UTC)))
        assert backend.turns.latest(HOUSEHOLD, "cnv_1").turn_id == "turn_new"


class TestPurge:
    def test_sqlite_deletes_expired_rows(self):
        conn = connect(":memory:")
        turns = SqliteTurnRepo(conn)
        turns.put(make_turn("turn_old", created_at=datetime.now(UTC) - timedelta(days=100)))
        turns.put(make_turn("turn_new", conversation_id="cnv_2", created_at=datetime.now(UTC)))
        assert turns.purge_expired(HOUSEHOLD) == 1
        assert conn.execute("SELECT COUNT(*) c FROM agent_turns").fetchone()["c"] == 1
        conn.close()

    def test_dynamo_leaves_it_to_the_table(self, dynamo_resource):
        turns = DynamoTurnRepo(TEST_TABLE, resource=dynamo_resource)
        turns.put(make_turn("turn_1"))
        assert turns.purge_expired(HOUSEHOLD) == 0


class TestProtocol:
    def test_both_backends_satisfy_the_protocol(self, backend):
        assert isinstance(backend.turns, TurnRepo)
