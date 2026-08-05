from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from airhead.domain import Event, EventSource, SourceKind
from airhead.recurrence import MAX_OCCURRENCES, expand, occurrence_id

NY = ZoneInfo("America/New_York")
SYDNEY = ZoneInfo("Australia/Sydney")


def make_event(**overrides) -> Event:
    base = {
        "event_id": "evt_1",
        "household_id": "hh_1",
        "title": "Soccer practice",
        "start_utc": datetime(2026, 10, 22, 20, 0, tzinfo=UTC),  # 16:00 EDT, a Thursday
        "end_utc": datetime(2026, 10, 22, 21, 30, tzinfo=UTC),
        "tz": "America/New_York",
        "owner_member_id": "mem_riley",
        "source": EventSource(kind=SourceKind.GOOGLE, external_id="ext_1"),
    }
    return Event(**{**base, **overrides})


def window(start: datetime, days: int) -> tuple[datetime, datetime]:
    return start, start + timedelta(days=days)


def local_times(instances: list[Event], zone: ZoneInfo = NY) -> list[datetime]:
    return [i.start_utc.astimezone(zone).replace(tzinfo=None) for i in instances]


class TestNonRecurring:
    def test_returns_itself_when_it_overlaps(self):
        event = make_event()
        result = expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 7))
        assert result == [event]
        assert result[0] is event

    def test_returns_nothing_outside_the_window(self):
        assert expand(make_event(), *window(datetime(2026, 11, 20, tzinfo=UTC), 7)) == []

    def test_includes_an_event_that_started_before_the_window(self):
        overnight = make_event(
            start_utc=datetime(2026, 10, 22, 3, 0, tzinfo=UTC),
            end_utc=datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
        )
        assert expand(
            overnight, datetime(2026, 10, 22, 8, 0, tzinfo=UTC), datetime(2026, 10, 23, tzinfo=UTC)
        ) == [overnight]

    def test_zero_length_marker_is_not_swallowed(self):
        marker = make_event(
            start_utc=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
            end_utc=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
        )
        assert len(expand(marker, *window(datetime(2026, 10, 22, tzinfo=UTC), 1))) == 1

    def test_empty_window_yields_nothing(self):
        at = datetime(2026, 10, 22, tzinfo=UTC)
        assert expand(make_event(), at, at) == []


class TestDst:
    """The trap the PRD calls out: 16:00 local must stay 16:00 local."""

    def test_weekly_stays_at_16_local_across_the_november_fall_back(self):
        # DST ends 2026-11-01 in America/New_York.
        event = make_event(rrule="FREQ=WEEKLY;BYDAY=TH")
        instances = expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 30))

        assert local_times(instances) == [
            datetime(2026, 10, 22, 16, 0),
            datetime(2026, 10, 29, 16, 0),
            datetime(2026, 11, 5, 16, 0),
            datetime(2026, 11, 12, 16, 0),
        ]
        # The UTC instant is what shifts, which is the whole point.
        assert [i.start_utc.hour for i in instances] == [20, 20, 21, 21]

    def test_weekly_stays_at_16_local_across_the_march_spring_forward(self):
        # DST starts 2026-03-08 in America/New_York.
        event = make_event(
            start_utc=datetime(2026, 2, 26, 21, 0, tzinfo=UTC),  # 16:00 EST, a Thursday
            end_utc=datetime(2026, 2, 26, 22, 30, tzinfo=UTC),
            rrule="FREQ=WEEKLY;BYDAY=TH",
        )
        instances = expand(event, *window(datetime(2026, 2, 24, tzinfo=UTC), 26))

        assert local_times(instances) == [
            datetime(2026, 2, 26, 16, 0),
            datetime(2026, 3, 5, 16, 0),
            datetime(2026, 3, 12, 16, 0),
            datetime(2026, 3, 19, 16, 0),
        ]
        assert [i.start_utc.hour for i in instances] == [21, 21, 20, 20]

    def test_southern_hemisphere_transition_runs_the_other_way(self):
        # Sydney leaves DST on 2026-04-05; the local hour must still not move.
        event = make_event(
            tz="Australia/Sydney",
            start_utc=datetime(2026, 3, 26, 5, 0, tzinfo=UTC),  # 16:00 AEDT Thursday
            end_utc=datetime(2026, 3, 26, 6, 30, tzinfo=UTC),
            rrule="FREQ=WEEKLY;BYDAY=TH",
        )
        instances = expand(event, *window(datetime(2026, 3, 24, tzinfo=UTC), 21))

        assert local_times(instances, SYDNEY) == [
            datetime(2026, 3, 26, 16, 0),
            datetime(2026, 4, 2, 16, 0),
            datetime(2026, 4, 9, 16, 0),
        ]
        assert [i.start_utc.hour for i in instances] == [5, 5, 6]

    def test_wall_clock_duration_is_preserved_across_the_transition(self):
        event = make_event(rrule="FREQ=WEEKLY;BYDAY=TH")
        for inst in expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 30)):
            start = inst.start_utc.astimezone(NY).replace(tzinfo=None)
            end = inst.end_utc.astimezone(NY).replace(tzinfo=None)
            assert end - start == timedelta(minutes=90)

    def test_daily_across_the_transition_keeps_a_single_local_hour(self):
        event = make_event(rrule="FREQ=DAILY")
        instances = expand(event, *window(datetime(2026, 10, 29, tzinfo=UTC), 6))
        assert {t.hour for t in local_times(instances)} == {16}
        # And no 24h-in-UTC drift: two distinct UTC hours across the boundary.
        assert {i.start_utc.hour for i in instances} == {20, 21}


class TestExdates:
    def test_exdate_removes_that_instance(self):
        event = make_event(
            rrule="FREQ=WEEKLY;BYDAY=TH",
            exdates=[datetime(2026, 10, 29, 20, 0, tzinfo=UTC)],
        )
        instances = expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 30))
        assert datetime(2026, 10, 29, 16, 0) not in local_times(instances)
        assert len(instances) == 3

    def test_exdate_after_the_transition_matches_the_shifted_instant(self):
        event = make_event(
            rrule="FREQ=WEEKLY;BYDAY=TH",
            exdates=[datetime(2026, 11, 5, 21, 0, tzinfo=UTC)],  # 16:00 EST
        )
        assert datetime(2026, 11, 5, 16, 0) not in local_times(
            expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 30))
        )

    def test_exdate_that_matches_nothing_is_harmless(self):
        event = make_event(
            rrule="FREQ=WEEKLY;BYDAY=TH",
            exdates=[datetime(2026, 10, 29, 3, 0, tzinfo=UTC)],
        )
        assert len(expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 30))) == 4


class TestAllDay:
    def _all_day(self, **overrides) -> Event:
        base = {
            "title": "Spring break",
            "all_day": True,
            "start_utc": datetime(2026, 10, 22, 0, 0),  # floating date
            "end_utc": datetime(2026, 10, 23, 0, 0),
            "rrule": "FREQ=WEEKLY;BYDAY=TH",
        }
        return make_event(**{**base, **overrides})

    def test_dates_do_not_shift_a_day(self):
        instances = expand(self._all_day(), *window(datetime(2026, 10, 20, tzinfo=UTC), 30))
        assert [i.start_utc.date() for i in instances] == [
            datetime(2026, 10, 22).date(),
            datetime(2026, 10, 29).date(),
            datetime(2026, 11, 5).date(),
            datetime(2026, 11, 12).date(),
        ]
        assert {i.start_utc.time() for i in instances} == {datetime.min.time()}

    def test_span_is_preserved(self):
        multi = self._all_day(end_utc=datetime(2026, 10, 25, 0, 0))
        for inst in expand(multi, *window(datetime(2026, 10, 20, tzinfo=UTC), 30)):
            assert inst.end_utc - inst.start_utc == timedelta(days=3)

    def test_exdate_by_date_removes_the_instance(self):
        event = self._all_day(exdates=[datetime(2026, 11, 5, 0, 0)])
        dates = [
            i.start_utc.date()
            for i in expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 30))
        ]
        assert datetime(2026, 11, 5).date() not in dates

    def test_a_midnight_utc_instant_still_lands_on_its_own_date(self):
        """An adapter that stored a UTC midnight must not slide back a day."""
        event = self._all_day(
            start_utc=datetime(2026, 10, 22, 0, 0, tzinfo=UTC),
            end_utc=datetime(2026, 10, 23, 0, 0, tzinfo=UTC),
        )
        instances = expand(event, *window(datetime(2026, 10, 22, 4, 0, tzinfo=UTC), 1))
        assert instances[0].start_utc.date() == datetime(2026, 10, 22).date()

    def test_non_recurring_all_day_overlaps_its_own_day(self):
        event = self._all_day(rrule=None)
        assert (
            len(
                expand(
                    event,
                    datetime(2026, 10, 22, 4, 0, tzinfo=UTC),
                    datetime(2026, 10, 23, 4, 0, tzinfo=UTC),
                )
            )
            == 1
        )

    def test_all_day_with_a_z_until_is_not_rejected(self):
        """A `Z` UNTIL against a floating DATE start is a real feed shape."""
        event = self._all_day(rrule="FREQ=WEEKLY;BYDAY=TH;UNTIL=20261030T000000Z")
        assert len(expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 30))) == 2


class TestBoundsAndRuleShapes:
    def test_count_is_honored(self):
        event = make_event(rrule="FREQ=WEEKLY;BYDAY=TH;COUNT=2")
        assert len(expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 60))) == 2

    def test_naive_until_is_read_as_local_wall_time(self):
        event = make_event(rrule="FREQ=WEEKLY;BYDAY=TH;UNTIL=20261105T170000")
        assert len(expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 60))) == 3

    def test_a_dtstart_inside_the_rule_text_does_not_win(self):
        event = make_event(rrule="DTSTART:20200101T000000\nRRULE:FREQ=WEEKLY;BYDAY=TH")
        instances = expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 14))
        assert local_times(instances) == [
            datetime(2026, 10, 22, 16, 0),
            datetime(2026, 10, 29, 16, 0),
        ]

    def test_expansion_is_capped(self):
        event = make_event(rrule="FREQ=MINUTELY")
        instances = expand(event, *window(datetime(2026, 10, 22, tzinfo=UTC), 31))
        assert len(instances) == MAX_OCCURRENCES

    def test_a_daily_rule_over_31_days_is_well_within_the_cap(self):
        event = make_event(rrule="FREQ=DAILY")
        assert len(expand(event, *window(datetime(2026, 10, 22, tzinfo=UTC), 31))) == 31

    def test_a_malformed_rule_degrades_instead_of_dropping_the_event(self):
        """R6: never make something vanish, and never take the agenda down."""
        event = make_event(rrule="FREQ=NONSENSE;BYDAY=??")
        assert expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 7)) == [event]


class TestInstanceShape:
    @pytest.fixture
    def instances(self) -> list[Event]:
        event = make_event(rrule="FREQ=WEEKLY;BYDAY=TH", location="Field 3", involves=["mem_sam"])
        return expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 14))

    def test_carries_the_parent_fields(self, instances):
        for inst in instances:
            assert inst.event_id == "evt_1"
            assert inst.title == "Soccer practice"
            assert inst.location == "Field 3"
            assert inst.involves == ["mem_sam"]
            assert inst.owner_member_id == "mem_riley"

    def test_links_back_to_the_master(self, instances):
        for inst in instances:
            assert inst.recurrence_parent_id == "evt_1"
        assert instances[1].recurrence_id == "2026-10-29T20:00:00Z"

    def test_instances_are_not_themselves_recurring(self, instances):
        assert all(i.rrule is None for i in instances)

    def test_master_is_left_alone(self):
        event = make_event(rrule="FREQ=WEEKLY;BYDAY=TH", involves=["mem_sam"])
        instances = expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 14))
        instances[0].involves.append("mem_alex")

        assert event.rrule == "FREQ=WEEKLY;BYDAY=TH"
        assert event.involves == ["mem_sam"]
        assert event.start_utc == datetime(2026, 10, 22, 20, 0, tzinfo=UTC)


class TestOccurrenceId:
    def test_is_stable_and_uses_the_wire_format(self):
        oid = occurrence_id("evt_1", datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
        assert oid == "evt_1@2026-08-04T20:00:00Z"
        assert oid == occurrence_id("evt_1", datetime(2026, 8, 4, 20, 0, tzinfo=UTC))

    def test_normalizes_to_utc_so_the_key_does_not_depend_on_the_caller(self):
        assert occurrence_id("evt_1", datetime(2026, 8, 4, 16, 0, tzinfo=NY)) == occurrence_id(
            "evt_1", datetime(2026, 8, 4, 20, 0, tzinfo=UTC)
        )

    def test_distinct_per_instance(self):
        event = make_event(rrule="FREQ=WEEKLY;BYDAY=TH")
        instances = expand(event, *window(datetime(2026, 10, 20, tzinfo=UTC), 30))
        ids = {occurrence_id(i.event_id, i.start_utc) for i in instances}
        assert len(ids) == len(instances)
