"""SQLite-backed repositories.

Not a test double. pytest runs against this in-memory, and a Pi-only
deployment would run the same code off a file, so it has to be a real
implementation with a real index behind the range query.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from airhead.domain import (
    Event,
    EventSource,
    Member,
    MemberRole,
    Source,
    SourceKind,
    Tier,
    TierSource,
    Visibility,
)
from airhead.repo import decode_instant, decode_instants, encode_instant, encode_instants
from airhead.repo.base import TIER_ORDER, AgendaQuery, Page, RepoError

DEFAULT_PAGE_SIZE = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    household_id         TEXT NOT NULL,
    event_id             TEXT NOT NULL,
    title                TEXT NOT NULL,
    start_utc            TEXT NOT NULL,
    end_utc              TEXT NOT NULL,
    tz                   TEXT NOT NULL,
    all_day              INTEGER NOT NULL,
    rrule                TEXT,
    exdates              TEXT NOT NULL,
    owner_member_id      TEXT NOT NULL,
    involves             TEXT NOT NULL,
    location             TEXT,
    tier                 TEXT NOT NULL,
    tier_source          TEXT NOT NULL,
    visibility           TEXT NOT NULL,
    source_kind          TEXT NOT NULL,
    source_id            TEXT,
    external_id          TEXT,
    etag                 TEXT,
    merge_group_id       TEXT,
    recurrence_parent_id TEXT,
    recurrence_id        TEXT,
    created_by           TEXT,
    updated_at           TEXT,
    deleted_at           TEXT,
    PRIMARY KEY (household_id, event_id)
);

-- Mirrors the DynamoDB sort key: the agenda read is always "one household, one
-- window, in start order", so the index has to cover ordering too or every
-- agenda request pays for a sort.
CREATE INDEX IF NOT EXISTS idx_events_window ON events (household_id, start_utc, event_id);
CREATE INDEX IF NOT EXISTS idx_events_external ON events (source_id, external_id);

CREATE TABLE IF NOT EXISTS members (
    household_id TEXT NOT NULL,
    member_id    TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL,
    color        TEXT NOT NULL,
    cognito_sub  TEXT,
    PRIMARY KEY (household_id, member_id)
);

CREATE TABLE IF NOT EXISTS sources (
    household_id    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    kind            TEXT NOT NULL,
    owner_member_id TEXT NOT NULL,
    label           TEXT NOT NULL,
    default_tier    TEXT,
    sync_cursor     TEXT,
    last_sync_at    TEXT,
    enabled         INTEGER NOT NULL,
    PRIMARY KEY (household_id, source_id)
);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection with the schema applied. Safe to call repeatedly."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _utc_now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _translate() -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as exc:  # Nothing above the repo layer may see a sqlite3 type.
        raise RepoError(str(exc)) from exc


def _encode_cursor(start_utc: str, event_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([start_utc, event_id]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        start_utc, event_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception as exc:
        raise RepoError(f"malformed cursor: {cursor!r}") from exc
    return start_utc, event_id


class _SqliteRepo:
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
            self.conn = connect(conn_or_path)
        self._clock = clock


class SqliteEventRepo(_SqliteRepo):
    def __init__(
        self,
        conn_or_path: sqlite3.Connection | str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] = _utc_now,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__(conn_or_path, clock=clock)
        self.page_size = page_size

    def get(self, household_id: str, event_id: str) -> Event | None:
        with _translate():
            row = self.conn.execute(
                "SELECT * FROM events WHERE household_id = ? AND event_id = ?",
                (household_id, event_id),
            ).fetchone()
        return _row_to_event(row) if row else None

    def put(self, event: Event) -> Event:
        stored = replace(event, updated_at=self._clock())
        with _translate():
            self.conn.execute(
                """
                INSERT OR REPLACE INTO events (
                    household_id, event_id, title, start_utc, end_utc, tz, all_day, rrule,
                    exdates, owner_member_id, involves, location, tier, tier_source,
                    visibility, source_kind, source_id, external_id, etag, merge_group_id,
                    recurrence_parent_id, recurrence_id, created_by, updated_at, deleted_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    stored.household_id,
                    stored.event_id,
                    stored.title,
                    encode_instant(stored.start_utc),
                    encode_instant(stored.end_utc),
                    stored.tz,
                    int(stored.all_day),
                    stored.rrule,
                    encode_instants(stored.exdates),
                    stored.owner_member_id,
                    json.dumps(list(stored.involves)),
                    stored.location,
                    stored.tier.value,
                    stored.tier_source.value,
                    stored.visibility.value,
                    stored.source.kind.value,
                    stored.source.source_id,
                    stored.source.external_id,
                    stored.source.etag,
                    stored.merge_group_id,
                    stored.recurrence_parent_id,
                    stored.recurrence_id,
                    stored.created_by,
                    encode_instant(stored.updated_at) if stored.updated_at else None,
                    encode_instant(stored.deleted_at) if stored.deleted_at else None,
                ),
            )
            self.conn.commit()
        return stored

    def delete(self, household_id: str, event_id: str, *, at: datetime) -> Event | None:
        existing = self.get(household_id, event_id)
        if existing is None:
            return None
        with _translate():
            self.conn.execute(
                "UPDATE events SET deleted_at = ?, updated_at = ? "
                "WHERE household_id = ? AND event_id = ?",
                (encode_instant(at), encode_instant(self._clock()), household_id, event_id),
            )
            self.conn.commit()
        return self.get(household_id, event_id)

    def list_range(self, query: AgendaQuery, *, cursor: str | None = None) -> Page:
        sql = ["SELECT * FROM events WHERE household_id = ?"]
        args: list[object] = [query.household_id]

        # A recurring master can start long before the window and still put an
        # instance inside it, so it is exempt from the range predicate here and
        # `airhead.recurrence` decides above this layer.
        sql.append("AND (rrule IS NOT NULL OR (start_utc < ? AND end_utc > ?))")
        args += [encode_instant(query.end_utc), encode_instant(query.start_utc)]

        if not query.include_deleted:
            sql.append("AND deleted_at IS NULL")
        if query.visibility_scope is Visibility.ALL:
            sql.append("AND visibility = ?")
            args.append(Visibility.ALL.value)

        allowed_tiers = [t.value for t in Tier if TIER_ORDER[t] <= TIER_ORDER[query.min_tier]]
        sql.append(f"AND tier IN ({','.join('?' * len(allowed_tiers))})")
        args += allowed_tiers

        if cursor is not None:
            after_start, after_id = _decode_cursor(cursor)
            sql.append("AND (start_utc > ? OR (start_utc = ? AND event_id > ?))")
            args += [after_start, after_start, after_id]

        sql.append("ORDER BY start_utc, event_id LIMIT ?")
        args.append(self.page_size + 1)

        with _translate():
            rows = self.conn.execute(" ".join(sql), args).fetchall()

        next_cursor = None
        if len(rows) > self.page_size:
            last = rows[self.page_size - 1]
            rows = rows[: self.page_size]
            next_cursor = _encode_cursor(last["start_utc"], last["event_id"])

        # The SQL above only narrows. `allows` is still the authority, so member
        # filtering (which spans a JSON column) and the visibility rule give the
        # same answer here as in every other backend - the contract test proves it.
        events = [e for e in (_row_to_event(r) for r in rows) if query.allows(e)]
        return Page(events=events, cursor=next_cursor)

    def get_by_external_id(self, source_id: str, external_id: str) -> Event | None:
        with _translate():
            row = self.conn.execute(
                "SELECT * FROM events WHERE source_id = ? AND external_id = ? LIMIT 1",
                (source_id, external_id),
            ).fetchone()
        return _row_to_event(row) if row else None


class SqliteMemberRepo(_SqliteRepo):
    def get(self, household_id: str, member_id: str) -> Member | None:
        with _translate():
            row = self.conn.execute(
                "SELECT * FROM members WHERE household_id = ? AND member_id = ?",
                (household_id, member_id),
            ).fetchone()
        return _row_to_member(row) if row else None

    def list(self, household_id: str) -> list[Member]:
        with _translate():
            rows = self.conn.execute(
                "SELECT * FROM members WHERE household_id = ? ORDER BY member_id",
                (household_id,),
            ).fetchall()
        return [_row_to_member(r) for r in rows]

    def put(self, member: Member) -> Member:
        with _translate():
            self.conn.execute(
                "INSERT OR REPLACE INTO members "
                "(household_id, member_id, display_name, role, color, cognito_sub) "
                "VALUES (?,?,?,?,?,?)",
                (
                    member.household_id,
                    member.member_id,
                    member.display_name,
                    member.role.value,
                    member.color,
                    member.cognito_sub,
                ),
            )
            self.conn.commit()
        return member


class SqliteSourceRepo(_SqliteRepo):
    def get(self, household_id: str, source_id: str) -> Source | None:
        with _translate():
            row = self.conn.execute(
                "SELECT * FROM sources WHERE household_id = ? AND source_id = ?",
                (household_id, source_id),
            ).fetchone()
        return _row_to_source(row) if row else None

    def list(self, household_id: str) -> list[Source]:
        with _translate():
            rows = self.conn.execute(
                "SELECT * FROM sources WHERE household_id = ? ORDER BY source_id",
                (household_id,),
            ).fetchall()
        return [_row_to_source(r) for r in rows]

    def put(self, source: Source) -> Source:
        with _translate():
            self.conn.execute(
                "INSERT OR REPLACE INTO sources (household_id, source_id, kind, "
                "owner_member_id, label, default_tier, sync_cursor, last_sync_at, enabled) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    source.household_id,
                    source.source_id,
                    source.kind.value,
                    source.owner_member_id,
                    source.label,
                    source.default_tier.value if source.default_tier else None,
                    source.cursor,
                    encode_instant(source.last_sync_at) if source.last_sync_at else None,
                    int(source.enabled),
                ),
            )
            self.conn.commit()
        return source


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        event_id=row["event_id"],
        household_id=row["household_id"],
        title=row["title"],
        start_utc=decode_instant(row["start_utc"]),
        end_utc=decode_instant(row["end_utc"]),
        tz=row["tz"],
        owner_member_id=row["owner_member_id"],
        source=EventSource(
            kind=SourceKind(row["source_kind"]),
            source_id=row["source_id"],
            external_id=row["external_id"],
            etag=row["etag"],
        ),
        all_day=bool(row["all_day"]),
        rrule=row["rrule"],
        exdates=decode_instants(row["exdates"]),
        involves=list(json.loads(row["involves"])),
        location=row["location"],
        tier=Tier(row["tier"]),
        tier_source=TierSource(row["tier_source"]),
        visibility=Visibility(row["visibility"]),
        merge_group_id=row["merge_group_id"],
        recurrence_parent_id=row["recurrence_parent_id"],
        recurrence_id=row["recurrence_id"],
        created_by=row["created_by"],
        updated_at=decode_instant(row["updated_at"]) if row["updated_at"] else None,
        deleted_at=decode_instant(row["deleted_at"]) if row["deleted_at"] else None,
    )


def _row_to_member(row: sqlite3.Row) -> Member:
    return Member(
        member_id=row["member_id"],
        household_id=row["household_id"],
        display_name=row["display_name"],
        role=MemberRole(row["role"]),
        color=row["color"],
        cognito_sub=row["cognito_sub"],
    )


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        source_id=row["source_id"],
        household_id=row["household_id"],
        kind=SourceKind(row["kind"]),
        owner_member_id=row["owner_member_id"],
        label=row["label"],
        default_tier=Tier(row["default_tier"]) if row["default_tier"] else None,
        cursor=row["sync_cursor"],
        last_sync_at=decode_instant(row["last_sync_at"]) if row["last_sync_at"] else None,
        enabled=bool(row["enabled"]),
    )
