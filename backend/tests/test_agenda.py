from datetime import UTC, date, datetime

import pytest

from airhead.agenda import build_agenda
from airhead.domain import (
    Event,
    EventSource,
    Member,
    MemberRole,
    SourceKind,
    Tier,
    Visibility,
)

TZ = "America/New_York"

ROSTER = [
    Member(
        member_id="mem_alex",
        household_id="hh_1",
        display_name="Alex",
        role=MemberRole.ADULT,
        color="#7aa2f7",
    ),
    Member(
        member_id="mem_sam",
        household_id="hh_1",
        display_name="Sam",
        role=MemberRole.ADULT,
        color="#9ece6a",
    ),
    Member(
        member_id="mem_riley",
        household_id="hh_1",
        display_name="Riley",
        role=MemberRole.MINOR,
        color="#f7768e",
    ),
]


def make_event(event_id: str, **overrides) -> Event:
    base = {
        "event_id": event_id,
        "household_id": "hh_1",
        "title": "Soccer practice",
        "start_utc": datetime(2026, 8, 4, 20, 0, tzinfo=UTC),  # 16:00 EDT
        "end_utc": datetime(2026, 8, 4, 21, 30, tzinfo=UTC),
        "tz": TZ,
        "owner_member_id": "mem_riley",
        "source": EventSource(kind=SourceKind.GOOGLE, external_id=f"ext_{event_id}"),
    }
    return Event(**{**base, **overrides})


def busy(event_id: str, member: str, hour: int, hours: int = 1, day: int = 4, **overrides) -> Event:
    """A T3 work block, given in local hours on 2026-08-{day}."""
    return make_event(
        event_id,
        title=f"Meeting {event_id}",
        owner_member_id=member,
        tier=Tier.BUSY,
        start_utc=datetime(2026, 8, day, hour + 4, 0, tzinfo=UTC),
        end_utc=datetime(2026, 8, day, hour + 4 + hours, 0, tzinfo=UTC),
        **overrides,
    )


def build(events, **overrides):
    kwargs = {
        "events": events,
        "members": ROSTER,
        "tz": TZ,
        "start": date(2026, 8, 4),
        "end": date(2026, 8, 6),
    }
    return build_agenda(**{**kwargs, **overrides})


def day_of(view, d: date):
    return next(day for day in view.days if day.date == d)


class TestWindow:
    def test_every_day_in_the_range_is_present_even_when_empty(self):
        view = build([])
        assert [d.date for d in view.days] == [date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]
        assert all(d.events == () and d.busy == () for d in view.days)

    def test_view_echoes_the_range_and_roster(self):
        view = build([])
        assert (view.start, view.end, view.tz) == (date(2026, 8, 4), date(2026, 8, 6), TZ)
        assert view.members == tuple(ROSTER)

    def test_a_single_day_range_is_one_day(self):
        view = build([make_event("evt_1")], start=date(2026, 8, 4), end=date(2026, 8, 4))
        assert len(view.days) == 1

    def test_an_inverted_range_is_rejected(self):
        with pytest.raises(ValueError):
            build([], start=date(2026, 8, 6), end=date(2026, 8, 4))

    def test_events_outside_the_window_are_not_placed(self):
        outside = make_event(
            "evt_x",
            start_utc=datetime(2026, 9, 1, 20, 0, tzinfo=UTC),
            end_utc=datetime(2026, 9, 1, 21, 0, tzinfo=UTC),
        )
        assert all(d.events == () for d in build([outside]).days)

    def test_tombstoned_events_are_excluded(self):
        gone = make_event("evt_1", deleted_at=datetime(2026, 8, 3, tzinfo=UTC))
        assert all(d.events == () for d in build([gone]).days)


class TestLocalConversion:
    def test_utc_is_converted_to_household_local(self):
        row = day_of(build([make_event("evt_1")]), date(2026, 8, 4)).events[0]
        assert row.start_local == datetime(2026, 8, 4, 16, 0)
        assert row.end_local == datetime(2026, 8, 4, 17, 30)
        assert row.start_local.tzinfo is None  # floating; the kiosk does no tz math
        assert row.start_utc == datetime(2026, 8, 4, 20, 0, tzinfo=UTC)

    def test_a_late_utc_instant_lands_on_the_previous_local_day(self):
        late = make_event(
            "evt_1",
            start_utc=datetime(2026, 8, 5, 1, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 5, 2, 0, tzinfo=UTC),
        )  # 21:00 Aug 4 local
        assert len(day_of(build([late]), date(2026, 8, 4)).events) == 1
        assert day_of(build([late]), date(2026, 8, 5)).events == ()

    def test_all_day_events_do_not_shift_a_day(self):
        holiday = make_event(
            "evt_1",
            title="Nana visits",
            all_day=True,
            start_utc=datetime(2026, 8, 5, 0, 0),
            end_utc=datetime(2026, 8, 6, 0, 0),
        )
        view = build([holiday])
        assert len(day_of(view, date(2026, 8, 5)).events) == 1
        assert day_of(view, date(2026, 8, 4)).events == ()
        assert day_of(view, date(2026, 8, 6)).events == ()

    def test_an_all_day_stored_as_a_utc_instant_also_holds_its_day(self):
        holiday = make_event(
            "evt_1",
            all_day=True,
            start_utc=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
        )
        assert len(day_of(build([holiday]), date(2026, 8, 5)).events) == 1


class TestMultiDay:
    def test_a_multi_day_event_appears_on_every_day_it_covers(self):
        trip = make_event(
            "evt_1",
            title="Trip",
            start_utc=datetime(2026, 8, 4, 20, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 6, 14, 0, tzinfo=UTC),
        )
        view = build([trip])
        assert [len(d.events) for d in view.days] == [1, 1, 1]

    def test_an_event_spanning_midnight_appears_on_both_days(self):
        overnight = make_event(
            "evt_1",
            title="Red-eye",
            start_utc=datetime(2026, 8, 5, 2, 0, tzinfo=UTC),  # 22:00 Aug 4
            end_utc=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )  # 06:00 Aug 5
        view = build([overnight])
        assert [len(d.events) for d in view.days] == [1, 1, 0]
        # Both rows carry the true span so the display can mark the continuation.
        assert day_of(view, date(2026, 8, 5)).events[0].start_local == datetime(2026, 8, 4, 22, 0)

    def test_ending_exactly_at_midnight_does_not_claim_the_next_day(self):
        event = make_event(
            "evt_1",
            start_utc=datetime(2026, 8, 4, 22, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 5, 4, 0, tzinfo=UTC),
        )  # 00:00 Aug 5 local
        assert [len(d.events) for d in build([event]).days] == [1, 0, 0]

    def test_a_multi_day_event_is_clipped_to_the_requested_window(self):
        trip = make_event(
            "evt_1",
            start_utc=datetime(2026, 8, 1, 20, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 20, 14, 0, tzinfo=UTC),
        )
        assert [len(d.events) for d in build([trip]).days] == [1, 1, 1]

    def test_a_multi_day_all_day_event_covers_its_dates_inclusively(self):
        camp = make_event(
            "evt_1",
            title="Camp",
            all_day=True,
            start_utc=datetime(2026, 8, 4, 0, 0),
            end_utc=datetime(2026, 8, 6, 0, 0),
        )  # exclusive DTEND
        assert [len(d.events) for d in build([camp]).days] == [1, 1, 0]

    def test_an_all_day_event_stored_with_an_inclusive_end_still_covers_a_day(self):
        same = make_event(
            "evt_1",
            all_day=True,
            start_utc=datetime(2026, 8, 4, 0, 0),
            end_utc=datetime(2026, 8, 4, 0, 0),
        )
        assert [len(d.events) for d in build([same]).days] == [1, 0, 0]


class TestT3Collapse:
    def test_t3_events_never_appear_as_event_rows(self):
        view = build([busy("evt_1", "mem_alex", 9), busy("evt_2", "mem_alex", 13)])
        assert day_of(view, date(2026, 8, 4)).events == ()

    def test_the_band_spans_earliest_start_to_latest_end_with_a_truthful_count(self):
        events = [
            busy("evt_1", "mem_alex", 9),
            busy("evt_2", "mem_alex", 11),
            busy("evt_3", "mem_alex", 14, hours=1),
        ]
        band = day_of(build(events), date(2026, 8, 4)).busy[0]

        assert band.member_id == "mem_alex"
        assert band.start_local == datetime(2026, 8, 4, 9, 0)
        assert band.end_local == datetime(2026, 8, 4, 15, 0)
        assert band.count == 3
        assert set(band.event_ids) == {"evt_1", "evt_2", "evt_3"}

    def test_one_band_per_member_per_day(self):
        events = [
            busy("evt_1", "mem_alex", 9),
            busy("evt_2", "mem_alex", 11),
            busy("evt_3", "mem_sam", 13),
        ]
        bands = day_of(build(events), date(2026, 8, 4)).busy
        assert [b.member_id for b in bands] == ["mem_alex", "mem_sam"]
        assert [b.count for b in bands] == [2, 1]

    def test_a_member_with_no_t3_events_gets_no_band(self):
        """Absence of a band means absence of work, which is information."""
        bands = day_of(build([busy("evt_1", "mem_alex", 9)]), date(2026, 8, 4)).busy
        assert [b.member_id for b in bands] == ["mem_alex"]

    def test_bands_are_in_roster_order(self):
        events = [
            busy("evt_1", "mem_riley", 9),
            busy("evt_2", "mem_sam", 9),
            busy("evt_3", "mem_alex", 9),
        ]
        bands = day_of(build(events), date(2026, 8, 4)).busy
        assert [b.member_id for b in bands] == ["mem_alex", "mem_sam", "mem_riley"]

    def test_a_shared_t3_block_lands_in_every_constrained_members_band(self):
        shared = busy("evt_1", "mem_alex", 9, involves=["mem_sam"])
        bands = day_of(build([shared]), date(2026, 8, 4)).busy
        assert [(b.member_id, b.count) for b in bands] == [("mem_alex", 1), ("mem_sam", 1)]

    def test_bands_are_scoped_to_their_own_day(self):
        events = [busy("evt_1", "mem_alex", 9), busy("evt_2", "mem_alex", 11, day=5)]
        view = build(events)
        assert day_of(view, date(2026, 8, 4)).busy[0].count == 1
        assert day_of(view, date(2026, 8, 5)).busy[0].count == 1
        assert day_of(view, date(2026, 8, 6)).busy == ()

    def test_an_overnight_t3_block_is_clipped_to_each_day(self):
        overnight = make_event(
            "evt_1",
            title="On call",
            owner_member_id="mem_alex",
            tier=Tier.BUSY,
            start_utc=datetime(2026, 8, 5, 2, 0, tzinfo=UTC),  # 22:00 Aug 4
            end_utc=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )  # 06:00 Aug 5
        view = build([overnight])
        first = day_of(view, date(2026, 8, 4)).busy[0]
        second = day_of(view, date(2026, 8, 5)).busy[0]

        assert (first.start_local, first.end_local) == (
            datetime(2026, 8, 4, 22, 0),
            datetime(2026, 8, 5, 0, 0),
        )
        assert (second.start_local, second.end_local) == (
            datetime(2026, 8, 5, 0, 0),
            datetime(2026, 8, 5, 6, 0),
        )

    def test_recurring_t3_blocks_are_counted_per_occurrence(self):
        standup = busy("evt_1", "mem_alex", 9, rrule="FREQ=DAILY")
        view = build([standup])
        assert [d.busy[0].count for d in view.days] == [1, 1, 1]


class TestNothingIsEverDropped:
    """PRD R6: hiding something that mattered is the product-killing failure."""

    @pytest.fixture
    def events(self) -> list[Event]:
        return [
            busy("evt_1", "mem_alex", 9),
            busy("evt_2", "mem_alex", 10),
            busy("evt_3", "mem_alex", 13, involves=["mem_sam"]),
            busy("evt_4", "mem_sam", 8),
            busy("evt_5", "mem_riley", 15),
            make_event("evt_6", tier=Tier.HOUSEHOLD),
        ]

    def test_every_t3_event_survives_the_collapse(self, events):
        day = day_of(build(events), date(2026, 8, 4))
        placed = {eid for band in day.busy for eid in band.event_ids}
        expected = {e.event_id for e in events if e.tier is Tier.BUSY}
        assert placed == expected

    def test_counts_add_up_to_every_t3_placement(self, events):
        day = day_of(build(events), date(2026, 8, 4))
        assert sum(b.count for b in day.busy) == sum(len(b.event_ids) for b in day.busy)
        assert sum(b.count for b in day.busy) == 6  # evt_3 constrains two members

    def test_a_recurring_t3_series_is_fully_accounted_for_across_the_window(self):
        standup = busy("evt_1", "mem_alex", 9, rrule="FREQ=DAILY")
        view = build([standup])
        assert sum(b.count for d in view.days for b in d.busy) == 3
        assert all(d.busy[0].event_ids == ("evt_1",) for d in view.days)


class TestOrdering:
    def test_all_day_events_sort_ahead_of_timed_ones(self):
        events = [
            make_event(
                "evt_1",
                title="Morning run",
                start_utc=datetime(2026, 8, 4, 11, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
            ),
            make_event(
                "evt_2",
                title="Nana visits",
                all_day=True,
                start_utc=datetime(2026, 8, 4, 0, 0),
                end_utc=datetime(2026, 8, 5, 0, 0),
            ),
        ]
        rows = day_of(build(events), date(2026, 8, 4)).events
        assert [r.title for r in rows] == ["Nana visits", "Morning run"]

    def test_timed_events_sort_by_local_start(self):
        events = [
            make_event(
                "evt_1",
                title="Late",
                start_utc=datetime(2026, 8, 4, 22, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 4, 23, 0, tzinfo=UTC),
            ),
            make_event(
                "evt_2",
                title="Early",
                start_utc=datetime(2026, 8, 4, 13, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 4, 14, 0, tzinfo=UTC),
            ),
        ]
        assert [r.title for r in day_of(build(events), date(2026, 8, 4)).events] == [
            "Early",
            "Late",
        ]

    def test_ties_break_on_title_regardless_of_input_order(self):
        events = [
            make_event("evt_1", title="Zoo"),
            make_event("evt_2", title="Aquarium"),
            make_event("evt_3", title="Museum"),
        ]
        forward = [r.title for r in day_of(build(events), date(2026, 8, 4)).events]
        reverse = [r.title for r in day_of(build(events[::-1]), date(2026, 8, 4)).events]
        assert forward == ["Aquarium", "Museum", "Zoo"] == reverse

    def test_identical_titles_still_order_deterministically(self):
        events = [make_event("evt_2"), make_event("evt_1")]
        assert [r.event_id for r in day_of(build(events), date(2026, 8, 4)).events] == [
            "evt_1",
            "evt_2",
        ]


class TestMemberIds:
    def test_resolved_set_is_owner_plus_involves_in_roster_order(self):
        event = make_event(
            "evt_1", owner_member_id="mem_riley", involves=["mem_sam", "mem_alex", "mem_riley"]
        )
        row = day_of(build([event]), date(2026, 8, 4)).events[0]
        assert row.member_ids == ("mem_alex", "mem_sam", "mem_riley")

    def test_a_member_outside_the_roster_is_kept_at_the_end(self):
        event = make_event("evt_1", owner_member_id="mem_alex", involves=["mem_ghost"])
        row = day_of(build([event]), date(2026, 8, 4)).events[0]
        assert row.member_ids == ("mem_alex", "mem_ghost")

    def test_is_family_needs_t1_and_more_than_one_member(self):
        shared = make_event("evt_1", tier=Tier.HOUSEHOLD, involves=["mem_sam"])
        solo = make_event("evt_2", tier=Tier.HOUSEHOLD, title="Solo")
        personal = make_event("evt_3", tier=Tier.PERSONAL, title="Gym", involves=["mem_sam"])
        rows = {
            r.event_id: r for r in day_of(build([shared, solo, personal]), date(2026, 8, 4)).events
        }

        assert rows["evt_1"].is_family is True
        assert rows["evt_2"].is_family is False
        assert rows["evt_3"].is_family is False


class TestMinTier:
    def test_default_includes_everything(self):
        events = [
            make_event("evt_1", tier=Tier.HOUSEHOLD),
            make_event("evt_2", tier=Tier.PERSONAL, title="Gym"),
            busy("evt_3", "mem_alex", 9),
        ]
        day = day_of(build(events), date(2026, 8, 4))
        assert len(day.events) == 2
        assert len(day.busy) == 1

    def test_min_tier_t2_drops_the_busy_bands(self):
        events = [make_event("evt_1", tier=Tier.PERSONAL), busy("evt_2", "mem_alex", 9)]
        day = day_of(build(events, min_tier=Tier.PERSONAL), date(2026, 8, 4))
        assert len(day.events) == 1
        assert day.busy == ()

    def test_min_tier_t1_keeps_only_household_events(self):
        events = [
            make_event("evt_1", tier=Tier.HOUSEHOLD),
            make_event("evt_2", tier=Tier.PERSONAL, title="Gym"),
            busy("evt_3", "mem_alex", 9),
        ]
        day = day_of(build(events, min_tier=Tier.HOUSEHOLD), date(2026, 8, 4))
        assert [r.event_id for r in day.events] == ["evt_1"]
        assert day.busy == ()


class TestRecurrenceIntegration:
    def test_a_weekly_master_is_expanded_into_the_window(self):
        weekly = make_event("evt_1", rrule="FREQ=DAILY")
        assert [len(d.events) for d in build([weekly]).days] == [1, 1, 1]

    def test_occurrence_id_is_set_only_on_expanded_instances(self):
        events = [make_event("evt_1", rrule="FREQ=DAILY"), make_event("evt_2", title="One-off")]
        rows = {r.event_id: r for r in day_of(build(events), date(2026, 8, 4)).events}

        assert rows["evt_1"].occurrence_id == "evt_1@2026-08-04T20:00:00Z"
        assert rows["evt_2"].occurrence_id is None

    def test_occurrence_ids_are_unique_across_the_window(self):
        view = build([make_event("evt_1", rrule="FREQ=DAILY")])
        ids = {r.occurrence_id for d in view.days for r in d.events}
        assert len(ids) == 3

    def test_an_exdate_removes_the_day_entirely(self):
        weekly = make_event(
            "evt_1", rrule="FREQ=DAILY", exdates=[datetime(2026, 8, 5, 20, 0, tzinfo=UTC)]
        )
        assert [len(d.events) for d in build([weekly]).days] == [1, 0, 1]

    def test_a_stored_override_replaces_its_generated_instance(self):
        master = make_event("evt_1", rrule="FREQ=DAILY")
        override = make_event(
            "evt_1_ovr",
            title="Practice moved",
            start_utc=datetime(2026, 8, 5, 22, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 5, 23, 0, tzinfo=UTC),
            recurrence_parent_id="evt_1",
            recurrence_id="2026-08-05T20:00:00Z",
        )
        view = build([master, override])

        assert [len(d.events) for d in view.days] == [1, 1, 1]
        assert day_of(view, date(2026, 8, 5)).events[0].title == "Practice moved"

    def test_a_dst_safe_series_keeps_its_local_hour_in_the_view(self):
        weekly = make_event(
            "evt_1",
            rrule="FREQ=WEEKLY;BYDAY=TH",
            start_utc=datetime(2026, 10, 22, 20, 0, tzinfo=UTC),  # 16:00 EDT
            end_utc=datetime(2026, 10, 22, 21, 30, tzinfo=UTC),
        )
        view = build([weekly], start=date(2026, 10, 22), end=date(2026, 11, 12))
        rows = [r for d in view.days for r in d.events]

        assert [r.start_local.time().hour for r in rows] == [16, 16, 16, 16]
        assert [r.start_utc.hour for r in rows] == [20, 20, 21, 21]

    def test_visibility_and_tier_source_ride_along_untouched(self):
        event = make_event("evt_1", visibility=Visibility.ADULTS)
        row = day_of(build([event]), date(2026, 8, 4)).events[0]
        assert row.visibility is Visibility.ADULTS
        assert row.tier_source == event.tier_source


class TestPurity:
    def test_the_input_events_are_not_mutated(self):
        master = make_event("evt_1", rrule="FREQ=DAILY", involves=["mem_sam"])
        build([master])

        assert master.start_utc == datetime(2026, 8, 4, 20, 0, tzinfo=UTC)
        assert master.rrule == "FREQ=DAILY"
        assert master.involves == ["mem_sam"]
        assert master.recurrence_parent_id is None

    def test_the_same_input_yields_the_same_view_twice(self):
        events = [
            make_event("evt_1", rrule="FREQ=DAILY"),
            busy("evt_2", "mem_alex", 9),
            make_event(
                "evt_3",
                title="Nana",
                all_day=True,
                start_utc=datetime(2026, 8, 5, 0, 0),
                end_utc=datetime(2026, 8, 6, 0, 0),
            ),
        ]
        assert build(events) == build(events)
