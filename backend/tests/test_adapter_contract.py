"""Proves the adapter contract suite is runnable and the seam holds, using an
in-memory fake. Concrete adapters (google, caldav, graph, ics) will subclass
`CalendarSourceContract` with their own fixtures when M3 lands."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from airhead.adapters.base import (
    CalendarRef,
    CalendarSource,
    Credentials,
    CursorInvalid,
    ExternalEvent,
    ExternalRef,
    PullResult,
    SourceConfig,
    SyncCursor,
)
from airhead.adapters.contract import CalendarSourceContract
from airhead.domain import SourceKind

TIMED = ExternalEvent(
    external_id="ext_soccer",
    title="Soccer practice",
    tz="America/New_York",
    start_utc=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
    end_utc=datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
    rrule="FREQ=WEEKLY;BYDAY=TH",
    location="Riverside Park Field 3",
)
ALL_DAY = ExternalEvent(
    external_id="ext_teacher_day",
    title="Teacher workday - no school",
    tz="America/New_York",
    all_day=True,
    start_date=date(2026, 8, 7),
    end_date=date(2026, 8, 8),
)


class FakeIcsSource:
    """Minimal pull-only adapter over canned data. No I/O anywhere."""

    kind = SourceKind.ICS

    def authorize(self, config: SourceConfig) -> Credentials:
        return Credentials(kind=self.kind, token_ref=config.secret_ref or "ssm:/fake/ics")

    def list_calendars(self, creds: Credentials) -> list[CalendarRef]:
        return [CalendarRef(calendar_id="cal_1", display_name="Family", read_only=True)]

    def pull(self, creds: Credentials, cursor: SyncCursor | None) -> PullResult:
        if cursor is None:
            return PullResult(upserts=(TIMED, ALL_DAY), cursor=SyncCursor(value="etag-1"))
        if cursor.value != "etag-1":
            raise CursorInvalid(cursor.value)
        return PullResult(cursor=cursor)

    def push(self, creds: Credentials, payload: ExternalEvent) -> ExternalRef:
        raise NotImplementedError("push is v2")

    def remove(self, creds: Credentials, ref: ExternalRef) -> None:
        raise NotImplementedError("remove is v2")


class TestFakeIcsContract(CalendarSourceContract):
    @pytest.fixture
    def adapter(self) -> CalendarSource:
        return FakeIcsSource()

    @pytest.fixture
    def config(self) -> SourceConfig:
        return SourceConfig(
            source_id="src_ics",
            household_id="hh_1",
            kind=SourceKind.ICS,
            secret_ref="ssm:/airhead/test/ics-url",
        )


class TestExternalEventInvariants:
    def test_timed_event_rejects_date_fields(self) -> None:
        with pytest.raises(ValueError):
            ExternalEvent(
                external_id="x",
                title="t",
                tz="UTC",
                start_utc=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 6, 21, 0, tzinfo=UTC),
                start_date=date(2026, 8, 6),
            )

    def test_all_day_event_rejects_instants(self) -> None:
        with pytest.raises(ValueError):
            ExternalEvent(
                external_id="x",
                title="t",
                tz="UTC",
                all_day=True,
                start_date=date(2026, 8, 6),
                end_date=date(2026, 8, 7),
                start_utc=datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
            )

    def test_all_day_event_requires_both_dates(self) -> None:
        with pytest.raises(ValueError):
            ExternalEvent(
                external_id="x", title="t", tz="UTC", all_day=True, start_date=date(2026, 8, 6)
            )

    def test_stale_cursor_raises_cursor_invalid(self) -> None:
        adapter = FakeIcsSource()
        creds = adapter.authorize(
            SourceConfig(source_id="s", household_id="h", kind=SourceKind.ICS)
        )
        with pytest.raises(CursorInvalid):
            adapter.pull(creds, SyncCursor(value="etag-stale"))
