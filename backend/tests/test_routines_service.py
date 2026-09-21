"""Projection, completion and interval resolution (docs/ROUTINES-CONTRACT.md).

Runs against the real SQLite repositories rather than the in-memory fakes:
the service is the only thing that writes routines *and* events together, so
these tests are also the first place a cross-repo assumption would break.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from airhead.domain import (
    Anchor,
    Completion,
    Event,
    IntervalSource,
    Member,
    MemberRole,
    Routine,
    Tier,
    TierSource,
    Visibility,
)
from airhead.repo.base import AgendaQuery
from airhead.repo.sqlite import SqliteEventRepo, SqliteRoutineRepo, connect
from airhead.routines import service
from airhead.routines.service import UNKNOWN, FutureCompletion, Resolution

TODAY = date(2026, 9, 20)
TZ = "America/New_York"
HOUSEHOLD = "hh_1"

# Declared here rather than imported from `fakes`: that module builds the whole
# FastAPI app, and this suite must not depend on the API layer being importable.
ALEX = Member("mem_alex", HOUSEHOLD, "Alex", MemberRole.ADULT, "#7aa2f7")
SAM = Member("mem_sam", HOUSEHOLD, "Sam", MemberRole.ADULT, "#9ece6a")
RILEY = Member("mem_riley", HOUSEHOLD, "Riley", MemberRole.MINOR, "#f7768e")


class TickingClock:
    """Strictly increasing instants, so "no write happened" is provable via updated_at."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store():
    conn = connect(":memory:")
    clock = TickingClock()
    yield SqliteRoutineRepo(conn, clock=clock), SqliteEventRepo(conn, clock=clock)
    conn.close()


def make_routine(**overrides) -> Routine:
    base = {
        "routine_id": "rtn_1",
        "household_id": HOUSEHOLD,
        "name": "Haircut",
        "owner_member_id": ALEX.member_id,
        "interval_days": 35,
        "interval_source": IntervalSource.CATALOG,
    }
    return Routine(**{**base, **overrides})


def make_completion(done_on: date, completion_id: str = "cmp_x") -> Completion:
    return Completion(
        completion_id=completion_id,
        household_id=HOUSEHOLD,
        routine_id="rtn_1",
        done_on=done_on,
        by_member_id=ALEX.member_id,
    )


def completions(*days: date) -> list[Completion]:
    return [make_completion(d, f"cmp_{i}") for i, d in enumerate(days)]


def all_events(events: SqliteEventRepo, *, include_deleted: bool = False) -> list[Event]:
    return events.list_range(
        AgendaQuery(
            household_id=HOUSEHOLD,
            start_utc=datetime(2020, 1, 1, tzinfo=UTC),
            end_utc=datetime(2040, 1, 1, tzinfo=UTC),
            visibility_scope=Visibility.ADULTS,
            include_deleted=include_deleted,
        )
    ).events


# --- interval resolution -----------------------------------------------------


class TestObservedInterval:
    def test_needs_three_completions(self):
        assert service.observed_interval(completions(date(2026, 1, 1), date(2026, 2, 1))) is None

    def test_median_of_gaps(self):
        history = completions(
            date(2026, 1, 1), date(2026, 1, 11), date(2026, 1, 31), date(2026, 5, 11)
        )
        # Gaps 10, 20, 100 -> median 20: the outlier does not drag the cadence.
        assert service.observed_interval(history) == 20

    def test_same_day_duplicates_do_not_count(self):
        history = completions(date(2026, 1, 1), date(2026, 1, 1), date(2026, 2, 1))
        assert service.observed_interval(history) is None

    def test_never_below_one_day(self):
        history = completions(date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3))
        assert service.observed_interval(history) == 1


class TestResolveInterval:
    def test_human_beats_everything(self):
        history = completions(date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1))
        r = service.resolve_interval("Haircut", stated=10, completions=history)
        assert (r.interval_days, r.source) == (10, IntervalSource.HUMAN)
        # The catalog still contributes its metadata even when it loses on the number.
        assert r.catalog_key == "haircut_mens"
        assert r.category == "personal_care"

    def test_observed_beats_catalog(self):
        history = completions(date(2026, 1, 1), date(2026, 1, 31), date(2026, 3, 6))
        r = service.resolve_interval("Haircut", completions=history)
        assert (r.interval_days, r.source) == (32, IntervalSource.OBSERVED)
        assert r.catalog_key == "haircut_mens"

    def test_catalog_when_history_is_thin(self):
        r = service.resolve_interval("Haircut", completions=completions(date(2026, 1, 1)))
        assert (r.interval_days, r.source) == (35, IntervalSource.CATALOG)
        assert r.range_days == (28, 42)
        assert r.usage_dependent is False
        assert r.note

    def test_catalog_flags_usage_dependence(self):
        r = service.resolve_interval("Cabin air filter 2023 Kia EV6")
        assert r.source is IntervalSource.CATALOG
        assert r.usage_dependent is True
        assert r.anchor is Anchor.ELAPSED

    def test_unknown_when_nothing_matches(self):
        r = service.resolve_interval("buy milk")
        assert r == UNKNOWN
        assert r.interval_days is None and r.source is None


class TestApplyInterval:
    def test_never_overwrites_human(self):
        routine = make_routine(interval_days=10, interval_source=IntervalSource.HUMAN)
        observed = Resolution(interval_days=30, source=IntervalSource.OBSERVED)

        service.apply_interval(routine, observed)

        assert (routine.interval_days, routine.interval_source) == (10, IntervalSource.HUMAN)

    def test_human_replaces_human(self):
        routine = make_routine(interval_days=10, interval_source=IntervalSource.HUMAN)

        service.apply_interval(routine, Resolution(interval_days=12, source=IntervalSource.HUMAN))

        assert routine.interval_days == 12

    def test_unknown_is_a_no_op(self):
        routine = make_routine()
        service.apply_interval(routine, UNKNOWN)
        assert (routine.interval_days, routine.interval_source) == (35, IntervalSource.CATALOG)

    def test_fills_metadata_without_clobbering(self):
        routine = make_routine(
            interval_days=None, category="custom", catalog_key="haircut_longer_styles"
        )
        estimate = Resolution(
            interval_days=40,
            source=IntervalSource.ESTIMATED,
            note="guess",
            confidence=0.6,
            catalog_key="haircut_mens",
            category="personal_care",
        )

        service.apply_interval(routine, estimate)

        assert routine.interval_days == 40
        assert routine.interval_note == "guess"
        assert routine.interval_confidence == 0.6
        assert routine.catalog_key == "haircut_longer_styles"  # already set: kept
        assert routine.category == "custom"  # not "other": kept

    def test_fills_default_category(self):
        routine = make_routine(interval_days=None, category="other", catalog_key=None)
        service.apply_interval(
            routine,
            Resolution(
                interval_days=35,
                source=IntervalSource.CATALOG,
                catalog_key="haircut_mens",
                category="personal_care",
            ),
        )
        assert routine.category == "personal_care"
        assert routine.catalog_key == "haircut_mens"


# --- projection --------------------------------------------------------------


class TestNextDue:
    def test_no_interval_is_none(self):
        assert service.next_due(make_routine(interval_days=None), today=TODAY) is None

    def test_no_history_is_today(self):
        assert service.next_due(make_routine(), today=TODAY) == TODAY

    def test_elapsed_adds_interval(self):
        routine = make_routine(last_done_on=date(2026, 9, 1))
        assert service.next_due(routine, today=TODAY) == date(2026, 10, 6)

    def test_calendar_is_next_anniversary(self):
        routine = make_routine(
            anchor=Anchor.CALENDAR, interval_days=365, last_done_on=date(2025, 11, 28)
        )
        assert service.next_due(routine, today=TODAY) == date(2026, 11, 28)

    def test_calendar_feb_29_falls_to_feb_28(self):
        routine = make_routine(
            anchor=Anchor.CALENDAR, interval_days=365, last_done_on=date(2028, 2, 29)
        )
        assert service.next_due(routine, today=TODAY) == date(2029, 2, 28)


class TestStatus:
    @pytest.mark.parametrize(
        ("due_on", "paused", "expected", "days"),
        [
            (None, True, "paused", None),
            (TODAY - timedelta(days=3), True, "paused", -3),
            (None, False, "unscheduled", None),
            (TODAY - timedelta(days=1), False, "overdue", -1),
            (TODAY, False, "due_soon", 0),
            (TODAY + timedelta(days=7), False, "due_soon", 7),
            (TODAY + timedelta(days=8), False, "ok", 8),
        ],
    )
    def test_status_and_days(self, due_on, paused, expected, days):
        routine = make_routine(due_on=due_on, paused=paused)
        assert service.status(routine, today=TODAY) == expected
        assert service.days_until_due(routine, today=TODAY) == days

    def test_sort_key_orders_the_contract_way(self):
        rows = {
            "overdue_10": make_routine(routine_id="a", due_on=TODAY - timedelta(days=10)),
            "overdue_1": make_routine(routine_id="b", due_on=TODAY - timedelta(days=1)),
            "soon_2": make_routine(routine_id="c", due_on=TODAY + timedelta(days=2)),
            "ok_30": make_routine(routine_id="d", due_on=TODAY + timedelta(days=30)),
            "ok_9": make_routine(routine_id="e", due_on=TODAY + timedelta(days=9)),
            "unscheduled": make_routine(routine_id="f", due_on=None),
            "paused": make_routine(routine_id="g", due_on=TODAY - timedelta(days=99), paused=True),
        }
        ordered = sorted(rows, key=lambda k: service.sort_key(rows[k], today=TODAY))
        assert ordered == [
            "overdue_10",
            "overdue_1",
            "soon_2",
            "ok_9",
            "ok_30",
            "unscheduled",
            "paused",
        ]


class TestVisibleTo:
    def test_minor_never_sees_adults_only(self):
        routine = make_routine(visibility=Visibility.ADULTS)
        assert service.visible_to(RILEY, routine) is False
        assert service.visible_to(ALEX, routine) is True

    def test_everyone_sees_all(self):
        assert service.visible_to(RILEY, make_routine()) is True

    def test_tombstone_is_invisible_to_everyone(self):
        routine = make_routine(deleted_at=datetime.now(UTC))
        assert service.visible_to(ALEX, routine) is False


# --- the due event -----------------------------------------------------------


class TestReproject:
    def test_creates_exactly_one_all_day_event(self, store):
        routines, events = store
        routine = routines.put(
            make_routine(
                due_on=date(2026, 10, 6),
                involves=[SAM.member_id],
                tier=Tier.HOUSEHOLD,
                visibility=Visibility.ADULTS,
                created_by=ALEX.member_id,
            )
        )

        projected = service.reproject(routine, routines, events, today=TODAY, tz=TZ)

        rows = all_events(events)
        assert len(rows) == 1
        event = rows[0]
        assert projected.due_event_id == event.event_id
        assert routines.get(HOUSEHOLD, "rtn_1").due_event_id == event.event_id
        assert event.routine_id == "rtn_1"
        assert event.title == "Haircut"
        assert event.all_day is True
        assert event.start_utc == datetime(2026, 10, 6, tzinfo=UTC)
        assert event.end_utc == datetime(2026, 10, 7, tzinfo=UTC)
        assert event.tz == TZ
        assert event.owner_member_id == ALEX.member_id
        assert event.involves == [SAM.member_id]
        assert event.tier is Tier.HOUSEHOLD
        assert event.tier_source is TierSource.HUMAN
        assert event.visibility is Visibility.ADULTS
        assert event.created_by == ALEX.member_id

    def test_idempotent(self, store):
        routines, events = store
        routine = routines.put(make_routine(due_on=date(2026, 10, 6)))
        first = service.reproject(routine, routines, events, today=TODAY, tz=TZ)
        event_stamp = events.get(HOUSEHOLD, first.due_event_id).updated_at

        second = service.reproject(first, routines, events, today=TODAY, tz=TZ)

        assert second.updated_at == first.updated_at
        assert routines.get(HOUSEHOLD, "rtn_1").updated_at == first.updated_at
        assert events.get(HOUSEHOLD, first.due_event_id).updated_at == event_stamp
        assert len(all_events(events)) == 1

    def test_overdue_rolls_forward_to_today(self, store):
        routines, events = store
        routine = routines.put(make_routine(due_on=TODAY - timedelta(days=12)))

        projected = service.reproject(routine, routines, events, today=TODAY, tz=TZ)

        event = events.get(HOUSEHOLD, projected.due_event_id)
        assert event.start_utc.date() == TODAY
        # The routine still knows it is overdue; only the display date moved.
        assert projected.due_on == TODAY - timedelta(days=12)
        assert service.status(projected, today=TODAY) == "overdue"

    def test_overdue_keeps_following_today(self, store):
        routines, events = store
        routine = routines.put(make_routine(due_on=TODAY - timedelta(days=12)))
        projected = service.reproject(routine, routines, events, today=TODAY, tz=TZ)

        tomorrow = TODAY + timedelta(days=1)
        service.reproject(projected, routines, events, today=tomorrow, tz=TZ)

        assert events.get(HOUSEHOLD, projected.due_event_id).start_utc.date() == tomorrow
        assert len(all_events(events)) == 1

    def test_paused_soft_deletes_the_event(self, store):
        routines, events = store
        routine = routines.put(make_routine(due_on=date(2026, 10, 6)))
        projected = service.reproject(routine, routines, events, today=TODAY, tz=TZ)
        event_id = projected.due_event_id

        projected.paused = True
        paused = service.reproject(projected, routines, events, today=TODAY, tz=TZ)

        assert paused.due_event_id is None
        assert routines.get(HOUSEHOLD, "rtn_1").due_event_id is None
        assert events.get(HOUSEHOLD, event_id).is_deleted
        assert all_events(events) == []

    def test_unscheduled_has_no_event(self, store):
        routines, events = store
        routine = routines.put(make_routine(interval_days=None, due_on=None))

        projected = service.reproject(routine, routines, events, today=TODAY, tz=TZ)

        assert projected.due_event_id is None
        assert all_events(events) == []

    def test_rename_updates_the_same_event(self, store):
        routines, events = store
        routine = routines.put(make_routine(due_on=date(2026, 10, 6)))
        projected = service.reproject(routine, routines, events, today=TODAY, tz=TZ)

        projected.name = "Haircut (barber on 5th)"
        renamed = service.reproject(projected, routines, events, today=TODAY, tz=TZ)

        assert renamed.due_event_id == projected.due_event_id
        assert events.get(HOUSEHOLD, renamed.due_event_id).title == "Haircut (barber on 5th)"
        assert len(all_events(events)) == 1

    def test_recreates_when_the_event_was_deleted_underneath(self, store):
        """Someone deleted the due row from the agenda; the routine still owns a date."""
        routines, events = store
        routine = routines.put(make_routine(due_on=date(2026, 10, 6)))
        projected = service.reproject(routine, routines, events, today=TODAY, tz=TZ)
        old_event_id = projected.due_event_id
        events.delete(HOUSEHOLD, old_event_id, at=datetime.now(UTC))

        again = service.reproject(projected, routines, events, today=TODAY, tz=TZ)

        assert again.due_event_id != old_event_id
        assert routines.get(HOUSEHOLD, "rtn_1").due_event_id == again.due_event_id
        assert len(all_events(events)) == 1


# --- completions -------------------------------------------------------------


class TestComplete:
    def test_appends_completion_and_moves_due_date(self, store):
        routines, events = store
        routine = routines.put(make_routine(last_done_on=date(2026, 8, 1), due_on=date(2026, 9, 5)))

        updated, completion = service.complete(
            routine,
            routines,
            events,
            by=SAM,
            done_on=date(2026, 9, 18),
            note="  short back and sides ",
            today=TODAY,
            tz=TZ,
        )

        assert completion.by_member_id == SAM.member_id
        assert completion.note == "short back and sides"
        assert completion.done_on == date(2026, 9, 18)
        assert updated.last_done_on == date(2026, 9, 18)
        assert updated.due_on == date(2026, 10, 23)
        history = routines.list_completions(HOUSEHOLD, "rtn_1")
        assert [c.completion_id for c in history] == [completion.completion_id]
        assert events.get(HOUSEHOLD, updated.due_event_id).start_utc.date() == date(2026, 10, 23)

    def test_done_on_defaults_to_today_and_blank_note_is_none(self, store):
        routines, events = store
        routine = routines.put(make_routine())

        updated, completion = service.complete(
            routine, routines, events, by=ALEX, done_on=None, note="   ", today=TODAY, tz=TZ
        )

        assert completion.done_on == TODAY
        assert completion.note is None
        assert updated.due_on == TODAY + timedelta(days=35)

    def test_clears_a_snooze(self, store):
        routines, events = store
        # Snoozed well past what the interval would say.
        routine = routines.put(
            make_routine(last_done_on=date(2026, 8, 1), due_on=date(2026, 12, 1))
        )

        updated, _ = service.complete(
            routine, routines, events, by=ALEX, done_on=TODAY, note=None, today=TODAY, tz=TZ
        )

        assert updated.due_on == TODAY + timedelta(days=35)

    def test_future_completion_is_refused(self, store):
        routines, events = store
        routine = routines.put(make_routine())

        with pytest.raises(FutureCompletion):
            service.complete(
                routine,
                routines,
                events,
                by=ALEX,
                done_on=TODAY + timedelta(days=1),
                note=None,
                today=TODAY,
                tz=TZ,
            )

        assert routines.list_completions(HOUSEHOLD, "rtn_1") == []

    def test_third_completion_switches_to_observed(self, store):
        routines, events = store
        routine = routines.put(make_routine())
        for day in (date(2026, 7, 1), date(2026, 7, 31)):
            routine, _ = service.complete(
                routine, routines, events, by=ALEX, done_on=day, note=None, today=TODAY, tz=TZ
            )
        assert routine.interval_source is IntervalSource.CATALOG

        routine, _ = service.complete(
            routine,
            routines,
            events,
            by=ALEX,
            done_on=date(2026, 9, 3),
            note=None,
            today=TODAY,
            tz=TZ,
        )

        # Gaps 30 and 34 -> median 32.
        assert (routine.interval_days, routine.interval_source) == (32, IntervalSource.OBSERVED)
        assert routine.due_on == date(2026, 9, 3) + timedelta(days=32)

    def test_observed_never_overrides_human(self, store):
        routines, events = store
        routine = routines.put(make_routine(interval_days=10, interval_source=IntervalSource.HUMAN))
        for day in (date(2026, 7, 1), date(2026, 7, 31), date(2026, 9, 3)):
            routine, _ = service.complete(
                routine, routines, events, by=ALEX, done_on=day, note=None, today=TODAY, tz=TZ
            )

        assert (routine.interval_days, routine.interval_source) == (10, IntervalSource.HUMAN)
        assert routine.due_on == date(2026, 9, 13)


class TestUndoCompletion:
    def test_rolls_back_last_done_and_due(self, store):
        routines, events = store
        routine = routines.put(make_routine())
        routine, first = service.complete(
            routine,
            routines,
            events,
            by=ALEX,
            done_on=date(2026, 8, 1),
            note=None,
            today=TODAY,
            tz=TZ,
        )
        routine, second = service.complete(
            routine,
            routines,
            events,
            by=ALEX,
            done_on=date(2026, 9, 10),
            note=None,
            today=TODAY,
            tz=TZ,
        )

        assert service.undo_completion(
            routine, routines, events, completion_id=second.completion_id, today=TODAY, tz=TZ
        )

        stored = routines.get(HOUSEHOLD, "rtn_1")
        assert stored.last_done_on == date(2026, 8, 1)
        assert stored.due_on == date(2026, 9, 5)
        assert [c.completion_id for c in routines.list_completions(HOUSEHOLD, "rtn_1")] == [
            first.completion_id
        ]
        # Sept 5 is already past, so the due event sits on today.
        assert events.get(HOUSEHOLD, stored.due_event_id).start_utc.date() == TODAY

    def test_undoing_the_only_completion(self, store):
        routines, events = store
        routine = routines.put(make_routine())
        routine, only = service.complete(
            routine,
            routines,
            events,
            by=ALEX,
            done_on=date(2026, 9, 1),
            note=None,
            today=TODAY,
            tz=TZ,
        )

        assert service.undo_completion(
            routine, routines, events, completion_id=only.completion_id, today=TODAY, tz=TZ
        )

        stored = routines.get(HOUSEHOLD, "rtn_1")
        assert stored.last_done_on is None
        assert stored.due_on == TODAY

    def test_unknown_completion_is_false_and_writes_nothing(self, store):
        routines, events = store
        routine = routines.put(
            make_routine(last_done_on=date(2026, 9, 1), due_on=date(2026, 10, 6))
        )
        stamp = routine.updated_at

        assert (
            service.undo_completion(
                routine, routines, events, completion_id="cmp_nope", today=TODAY, tz=TZ
            )
            is None
        )

        assert routines.get(HOUSEHOLD, "rtn_1").updated_at == stamp


# --- create / remove ---------------------------------------------------------


def create(store, **overrides) -> Routine:
    routines, events = store
    kwargs = {
        "household_id": HOUSEHOLD,
        "name": "Haircut",
        "owner": ALEX,
        "created_by": ALEX,
        "today": TODAY,
        "tz": TZ,
    }
    return service.create(routines, events, **{**kwargs, **overrides})


class TestCreate:
    def test_catalog_match_fills_everything(self, store):
        routines, events = store
        routine = create(store, name="Put up the Christmas lights")

        assert routine.catalog_key == "christmas_decorations_up"
        assert routine.category == "holidays_seasonal"
        assert routine.anchor is Anchor.CALENDAR
        assert (routine.interval_days, routine.interval_source) == (365, IntervalSource.CATALOG)
        assert routine.interval_note
        # Never done: due now, on the calendar today.
        assert routine.due_on == TODAY
        assert events.get(HOUSEHOLD, routine.due_event_id).start_utc.date() == TODAY
        assert routines.get(HOUSEHOLD, routine.routine_id) is not None

    def test_seeds_a_first_completion_from_last_done_on(self, store):
        routines, events = store
        routine = create(store, last_done_on=date(2026, 9, 1), note="barber on 5th", created_by=SAM)

        history = routines.list_completions(HOUSEHOLD, routine.routine_id)
        assert len(history) == 1
        assert history[0].done_on == date(2026, 9, 1)
        assert history[0].by_member_id == SAM.member_id
        assert history[0].note == "barber on 5th"
        assert routine.last_done_on == date(2026, 9, 1)
        assert routine.due_on == date(2026, 10, 6)
        assert routine.created_by == SAM.member_id
        assert routine.owner_member_id == ALEX.member_id

    def test_stated_interval_is_human(self, store):
        routine = create(store, stated_interval=21)
        assert (routine.interval_days, routine.interval_source) == (21, IntervalSource.HUMAN)
        assert routine.catalog_key == "haircut_mens"

    def test_estimate_used_only_when_ladder_is_empty(self, store):
        estimate = Resolution(
            interval_days=90,
            source=IntervalSource.ESTIMATED,
            note="model guess",
            confidence=0.4,
            category="kitchen",
        )

        guessed = create(store, name="buy milk", estimate=estimate)
        known = create(store, name="Haircut", estimate=estimate)

        assert (guessed.interval_days, guessed.interval_source) == (90, IntervalSource.ESTIMATED)
        assert guessed.interval_confidence == 0.4
        assert guessed.category == "kitchen"
        assert (known.interval_days, known.interval_source) == (35, IntervalSource.CATALOG)
        assert known.interval_confidence is None

    def test_unknown_is_unscheduled_with_no_event(self, store):
        routines, events = store
        routine = create(store, name="buy milk")

        assert routine.interval_days is None
        assert routine.due_on is None
        assert routine.due_event_id is None
        assert service.status(routine, today=TODAY) == "unscheduled"
        assert all_events(events) == []

    def test_explicit_due_on_wins(self, store):
        routines, events = store
        routine = create(store, last_done_on=date(2026, 9, 1), due_on=date(2026, 12, 24))

        assert routine.due_on == date(2026, 12, 24)
        assert events.get(HOUSEHOLD, routine.due_event_id).start_utc.date() == date(2026, 12, 24)

    def test_explicit_fields_beat_catalog_and_involves_dedupe(self, store):
        routine = create(
            store,
            category="grooming",
            anchor=Anchor.CALENDAR,
            involves=[SAM.member_id, RILEY.member_id, SAM.member_id],
            tier=Tier.HOUSEHOLD,
            visibility=Visibility.ADULTS,
        )
        assert routine.category == "grooming"
        assert routine.anchor is Anchor.CALENDAR
        assert routine.involves == [SAM.member_id, RILEY.member_id]
        assert routine.tier is Tier.HOUSEHOLD
        assert routine.visibility is Visibility.ADULTS
        assert routine.name == "Haircut"

    def test_name_is_stripped(self, store):
        assert create(store, name="  Haircut  ").name == "Haircut"


class TestRemove:
    def test_tombstones_routine_and_due_event(self, store):
        routines, events = store
        routine = create(store, last_done_on=date(2026, 9, 1))
        at = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)

        removed = service.remove(routine, routines, events, at=at)

        assert removed.deleted_at == at
        assert routines.list(HOUSEHOLD) == []
        assert routines.get(HOUSEHOLD, routine.routine_id).is_deleted
        assert events.get(HOUSEHOLD, routine.due_event_id).deleted_at == at
        assert all_events(events) == []

    def test_remove_without_event(self, store):
        routines, events = store
        routine = create(store, name="buy milk")

        assert service.remove(routine, routines, events).is_deleted
