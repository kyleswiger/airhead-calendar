"""RoutineRepo contract: SQLite and DynamoDB must answer identically.

Runs through the `repos` fixture so every assertion here is made twice, once
per backend. A routine is small enough that a whole-record `asdict` comparison
is the cheapest way to catch a field one backend forgot to serialise.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime

from airhead.domain import (
    Anchor,
    Completion,
    Event,
    EventSource,
    IntervalSource,
    Routine,
    SourceKind,
    Tier,
    Visibility,
)
from airhead.repo.base import AgendaQuery

HH = "hh_1"


def make_routine(**overrides) -> Routine:
    base = {
        "routine_id": "rtn_1",
        "household_id": HH,
        "name": "Haircut",
        "owner_member_id": "mem_alex",
    }
    return Routine(**{**base, **overrides})


def make_completion(**overrides) -> Completion:
    base = {
        "completion_id": "cmp_1",
        "household_id": HH,
        "routine_id": "rtn_1",
        "done_on": date(2026, 9, 1),
        "by_member_id": "mem_alex",
    }
    return Completion(**{**base, **overrides})


class TestRoundTrip:
    def test_put_get_preserves_every_field(self, repos):
        routine = make_routine(
            category="vehicle",
            interval_days=365,
            interval_source=IntervalSource.ESTIMATED,
            interval_note="Kia says every 15,000 mi.",
            interval_confidence=0.75,
            anchor=Anchor.CALENDAR,
            involves=["mem_sam", "mem_riley"],
            tier=Tier.HOUSEHOLD,
            visibility=Visibility.ADULTS,
            catalog_key="ev6_cabin_air_filter",
            last_done_on=date(2026, 9, 1),
            due_on=date(2027, 9, 1),
            due_event_id="evt_due",
            paused=True,
            created_by="mem_alex",
            deleted_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        )
        stored = repos.routines.put(routine)

        fetched = repos.routines.get(HH, "rtn_1")

        assert stored.updated_at is not None
        assert asdict(fetched) == asdict(stored)
        assert isinstance(fetched.interval_confidence, float)

    def test_put_get_preserves_the_empty_case(self, repos):
        """None interval, no dates, no involves - the `unscheduled` routine."""
        stored = repos.routines.put(make_routine())

        fetched = repos.routines.get(HH, "rtn_1")

        assert asdict(fetched) == asdict(stored)
        assert fetched.interval_days is None
        assert fetched.interval_confidence is None
        assert fetched.last_done_on is None
        assert fetched.due_on is None
        assert fetched.involves == []
        assert fetched.paused is False
        assert fetched.deleted_at is None

    def test_put_replaces(self, repos):
        repos.routines.put(make_routine(interval_days=35, due_on=date(2026, 10, 1)))
        repos.routines.put(make_routine(interval_days=None, due_on=None))

        fetched = repos.routines.get(HH, "rtn_1")

        assert fetched.interval_days is None
        assert fetched.due_on is None

    def test_get_missing_is_none(self, repos):
        assert repos.routines.get(HH, "rtn_nope") is None


class TestListAndDelete:
    def test_list_ordered_by_routine_id(self, repos):
        for rid in ("rtn_c", "rtn_a", "rtn_b"):
            repos.routines.put(make_routine(routine_id=rid))

        assert [r.routine_id for r in repos.routines.list(HH)] == ["rtn_a", "rtn_b", "rtn_c"]

    def test_soft_delete_hides_from_list_but_not_get(self, repos):
        repos.routines.put(make_routine(routine_id="rtn_a"))
        repos.routines.put(make_routine(routine_id="rtn_b"))
        at = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)

        deleted = repos.routines.delete(HH, "rtn_a", at=at)

        assert deleted is not None and deleted.deleted_at == at
        assert [r.routine_id for r in repos.routines.list(HH)] == ["rtn_b"]
        assert [r.routine_id for r in repos.routines.list(HH, include_deleted=True)] == [
            "rtn_a",
            "rtn_b",
        ]
        assert repos.routines.get(HH, "rtn_a").is_deleted

    def test_delete_missing_is_none(self, repos):
        assert repos.routines.delete(HH, "rtn_nope", at=datetime.now(UTC)) is None

    def test_households_are_isolated(self, repos):
        repos.routines.put(make_routine(routine_id="rtn_1", household_id="hh_1"))
        repos.routines.put(make_routine(routine_id="rtn_1", household_id="hh_2", name="Other"))
        repos.routines.add_completion(make_completion(household_id="hh_2"))

        assert repos.routines.get("hh_1", "rtn_1").name == "Haircut"
        assert repos.routines.get("hh_2", "rtn_1").name == "Other"
        assert [r.household_id for r in repos.routines.list("hh_1")] == ["hh_1"]
        assert repos.routines.list_completions("hh_1", "rtn_1") == []
        assert len(repos.routines.list_completions("hh_2", "rtn_1")) == 1


class TestCompletions:
    def test_add_and_list_round_trip(self, repos):
        stored = repos.routines.add_completion(make_completion(note="Bosch filter"))

        listed = repos.routines.list_completions(HH, "rtn_1")

        assert stored.created_at is not None
        assert [asdict(c) for c in listed] == [asdict(stored)]

    def test_explicit_created_at_is_kept(self, repos):
        at = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

        stored = repos.routines.add_completion(make_completion(created_at=at))

        assert stored.created_at == at
        assert repos.routines.list_completions(HH, "rtn_1")[0].created_at == at

    def test_ordered_by_done_on_then_id(self, repos):
        repos.routines.add_completion(
            make_completion(completion_id="cmp_z", done_on=date(2026, 9, 1))
        )
        repos.routines.add_completion(
            make_completion(completion_id="cmp_b", done_on=date(2026, 10, 1))
        )
        repos.routines.add_completion(
            make_completion(completion_id="cmp_a", done_on=date(2026, 10, 1))
        )
        repos.routines.add_completion(
            make_completion(completion_id="cmp_m", done_on=date(2025, 12, 31))
        )

        listed = repos.routines.list_completions(HH, "rtn_1")

        assert [c.completion_id for c in listed] == ["cmp_m", "cmp_z", "cmp_a", "cmp_b"]

    def test_scoped_to_one_routine(self, repos):
        repos.routines.add_completion(make_completion(routine_id="rtn_1"))
        repos.routines.add_completion(make_completion(routine_id="rtn_10", completion_id="cmp_2"))

        assert [c.routine_id for c in repos.routines.list_completions(HH, "rtn_1")] == ["rtn_1"]

    def test_delete_completion_reports_existence(self, repos):
        repos.routines.add_completion(make_completion())

        assert repos.routines.delete_completion(HH, "rtn_1", "cmp_1") is True
        assert repos.routines.list_completions(HH, "rtn_1") == []
        assert repos.routines.delete_completion(HH, "rtn_1", "cmp_1") is False
        assert repos.routines.delete_completion(HH, "rtn_1", "cmp_never") is False


class TestDueEvent:
    def test_event_routine_id_round_trips(self, repos):
        """The due event is a plain Event with `routine_id` set - both stores keep it."""
        start = datetime(2026, 10, 1, tzinfo=UTC)
        event = Event(
            event_id="evt_due",
            household_id=HH,
            title="Haircut",
            start_utc=start,
            end_utc=datetime(2026, 10, 2, tzinfo=UTC),
            tz="America/New_York",
            owner_member_id="mem_alex",
            source=EventSource(kind=SourceKind.NATIVE),
            all_day=True,
            routine_id="rtn_1",
        )
        repos.events.put(event)

        fetched = repos.events.get(HH, "evt_due")
        page = repos.events.list_range(
            AgendaQuery(
                household_id=HH,
                start_utc=datetime(2026, 9, 28, tzinfo=UTC),
                end_utc=datetime(2026, 10, 5, tzinfo=UTC),
            )
        )

        assert fetched.routine_id == "rtn_1"
        assert [e.routine_id for e in page.events] == ["rtn_1"]

    def test_event_without_routine_id_stays_none(self, repos):
        event = Event(
            event_id="evt_plain",
            household_id=HH,
            title="Dentist",
            start_utc=datetime(2026, 10, 1, 14, tzinfo=UTC),
            end_utc=datetime(2026, 10, 1, 15, tzinfo=UTC),
            tz="America/New_York",
            owner_member_id="mem_alex",
            source=EventSource(kind=SourceKind.NATIVE),
        )
        repos.events.put(event)

        assert repos.events.get(HH, "evt_plain").routine_id is None
