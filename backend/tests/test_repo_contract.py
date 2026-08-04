"""One suite, every backend. SQLite and DynamoDB must answer identically."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

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
from airhead.repo.base import AgendaQuery
from airhead.repo.seed import ALEX, HOUSEHOLD_ID, RILEY, SAM, seed_household

HH = "hh_1"
WINDOW_START = datetime(2026, 8, 3, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 10, tzinfo=UTC)


def make_event(**overrides) -> Event:
    base = {
        "event_id": "evt_1",
        "household_id": HH,
        "title": "Soccer practice",
        "start_utc": datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
        "tz": "America/New_York",
        "owner_member_id": "mem_riley",
        "source": EventSource(kind=SourceKind.GOOGLE, source_id="src_1", external_id="ext_1"),
    }
    return Event(**{**base, **overrides})


def query(**overrides) -> AgendaQuery:
    base = {"household_id": HH, "start_utc": WINDOW_START, "end_utc": WINDOW_END}
    return AgendaQuery(**{**base, **overrides})


def ids(page) -> list[str]:
    return [e.event_id for e in page.events]


class TestRoundTrip:
    def test_put_get_preserves_every_field(self, repos):
        event = make_event(
            all_day=True,
            rrule="FREQ=WEEKLY;BYDAY=TH",
            exdates=[datetime(2026, 8, 13, 20, 0, tzinfo=UTC)],
            involves=["mem_riley", "mem_sam"],
            location="Riverside Park Field 3",
            tier=Tier.HOUSEHOLD,
            tier_source=TierSource.HUMAN,
            visibility=Visibility.ADULTS,
            merge_group_id="mg_1",
            recurrence_parent_id="evt_parent",
            recurrence_id="2026-08-06T20:00:00Z",
            created_by="mem_alex",
            source=EventSource(
                kind=SourceKind.CALDAV,
                source_id="src_1",
                external_id="ext_1",
                etag='W/"abc"',
            ),
        )
        stored = repos.events.put(event)

        fetched = repos.events.get(HH, "evt_1")

        assert asdict(fetched) == asdict(stored)

    def test_datetimes_come_back_utc_aware(self, repos):
        repos.events.put(make_event())

        fetched = repos.events.get(HH, "evt_1")

        assert fetched.start_utc.tzinfo is not None
        assert fetched.start_utc.utcoffset() == timedelta(0)
        assert fetched.start_utc == datetime(2026, 8, 6, 20, 0, tzinfo=UTC)

    def test_a_non_utc_input_is_normalised_not_shifted(self, repos):
        eastern = datetime(2026, 8, 6, 16, 0, tzinfo=ZoneInfo("America/New_York"))
        repos.events.put(make_event(start_utc=eastern))

        stored = repos.events.get(HH, "evt_1").start_utc

        assert stored == eastern
        assert stored == datetime(2026, 8, 6, 20, 0, tzinfo=UTC)

    def test_put_stamps_updated_at(self, repos):
        stored = repos.events.put(make_event(updated_at=None))

        assert stored.updated_at is not None
        assert repos.events.get(HH, "evt_1").updated_at == stored.updated_at

    def test_put_replaces_rather_than_duplicates(self, repos):
        repos.events.put(make_event(title="Soccer practice"))
        repos.events.put(make_event(title="Soccer game"))

        assert repos.events.get(HH, "evt_1").title == "Soccer game"
        assert ids(repos.events.list_range(query())) == ["evt_1"]

    def test_get_unknown_is_none(self, repos):
        assert repos.events.get(HH, "nope") is None

    def test_get_is_household_scoped(self, repos):
        repos.events.put(make_event())

        assert repos.events.get("hh_other", "evt_1") is None


class TestSoftDelete:
    def test_stamps_deleted_at(self, repos):
        repos.events.put(make_event())
        at = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

        deleted = repos.events.delete(HH, "evt_1", at=at)

        assert deleted.deleted_at == at
        assert deleted.is_deleted

    def test_tombstone_is_still_readable_by_id(self, repos):
        repos.events.put(make_event())
        repos.events.delete(HH, "evt_1", at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC))

        assert repos.events.get(HH, "evt_1").is_deleted

    def test_disappears_from_list_range(self, repos):
        repos.events.put(make_event())
        repos.events.delete(HH, "evt_1", at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC))

        assert ids(repos.events.list_range(query())) == []

    def test_include_deleted_brings_it_back(self, repos):
        repos.events.put(make_event())
        repos.events.delete(HH, "evt_1", at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC))

        assert ids(repos.events.list_range(query(include_deleted=True))) == ["evt_1"]

    def test_delete_unknown_returns_none(self, repos):
        assert repos.events.delete(HH, "nope", at=datetime(2026, 8, 5, tzinfo=UTC)) is None


class TestExternalId:
    def test_finds_by_source_and_external_id(self, repos):
        repos.events.put(make_event())

        assert repos.events.get_by_external_id("src_1", "ext_1").event_id == "evt_1"

    def test_unknown_external_id_is_none(self, repos):
        repos.events.put(make_event())

        assert repos.events.get_by_external_id("src_1", "ext_missing") is None

    def test_same_external_id_on_another_source_does_not_match(self, repos):
        repos.events.put(make_event())

        assert repos.events.get_by_external_id("src_2", "ext_1") is None


class TestRange:
    def test_inside_the_window_is_returned(self, repos):
        repos.events.put(make_event())

        assert ids(repos.events.list_range(query())) == ["evt_1"]

    def test_after_the_window_is_excluded(self, repos):
        repos.events.put(
            make_event(
                start_utc=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 20, 21, 0, tzinfo=UTC),
            )
        )

        assert ids(repos.events.list_range(query())) == []

    def test_before_the_window_is_excluded(self, repos):
        repos.events.put(
            make_event(
                start_utc=datetime(2026, 7, 30, 20, 0, tzinfo=UTC),
                end_utc=datetime(2026, 7, 30, 21, 0, tzinfo=UTC),
            )
        )

        assert ids(repos.events.list_range(query())) == []

    def test_event_straddling_the_window_start_is_returned(self, repos):
        repos.events.put(
            make_event(
                start_utc=datetime(2026, 8, 2, 22, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 3, 2, 0, tzinfo=UTC),
            )
        )

        assert ids(repos.events.list_range(query())) == ["evt_1"]

    def test_recurring_master_before_the_window_is_never_range_excluded(self, repos):
        """Expansion happens above this layer, so the master must survive the query."""
        repos.events.put(
            make_event(
                start_utc=datetime(2024, 1, 4, 20, 0, tzinfo=UTC),
                end_utc=datetime(2024, 1, 4, 21, 0, tzinfo=UTC),
                rrule="FREQ=WEEKLY;BYDAY=TH",
            )
        )

        assert ids(repos.events.list_range(query())) == ["evt_1"]

    def test_another_household_is_never_returned(self, repos):
        repos.events.put(make_event(household_id="hh_other"))

        assert ids(repos.events.list_range(query())) == []

    def test_results_are_ordered_by_start(self, repos):
        repos.events.put(
            make_event(
                event_id="evt_late",
                start_utc=datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
                source=EventSource(kind=SourceKind.NATIVE),
            )
        )
        repos.events.put(
            make_event(
                event_id="evt_early",
                start_utc=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
                source=EventSource(kind=SourceKind.NATIVE),
            )
        )

        assert ids(repos.events.list_range(query())) == ["evt_early", "evt_late"]


class TestTierFilter:
    @pytest.mark.parametrize(
        "min_tier,expected",
        [
            (Tier.HOUSEHOLD, ["evt_t1"]),
            (Tier.PERSONAL, ["evt_t1", "evt_t2"]),
            (Tier.BUSY, ["evt_t1", "evt_t2", "evt_t3"]),
        ],
    )
    def test_min_tier_narrows_to_more_relevant_events(self, repos, min_tier, expected):
        for index, tier in enumerate([Tier.HOUSEHOLD, Tier.PERSONAL, Tier.BUSY], start=1):
            repos.events.put(
                make_event(
                    event_id=f"evt_{tier.value.lower()}",
                    tier=tier,
                    start_utc=datetime(2026, 8, 4, index, 0, tzinfo=UTC),
                    end_utc=datetime(2026, 8, 4, index, 30, tzinfo=UTC),
                    source=EventSource(kind=SourceKind.NATIVE),
                )
            )

        assert sorted(ids(repos.events.list_range(query(min_tier=min_tier)))) == expected


class TestMemberFilter:
    def test_matches_the_owner(self, repos):
        repos.events.put(make_event(owner_member_id="mem_riley", involves=[]))

        assert ids(repos.events.list_range(query(member_ids=("mem_riley",)))) == ["evt_1"]

    def test_matches_someone_the_event_involves(self, repos):
        repos.events.put(make_event(owner_member_id="mem_riley", involves=["mem_sam"]))

        assert ids(repos.events.list_range(query(member_ids=("mem_sam",)))) == ["evt_1"]

    def test_excludes_an_uninvolved_member(self, repos):
        repos.events.put(make_event(owner_member_id="mem_riley", involves=["mem_sam"]))

        assert ids(repos.events.list_range(query(member_ids=("mem_alex",)))) == []

    def test_none_means_every_member(self, repos):
        repos.events.put(make_event())

        assert ids(repos.events.list_range(query(member_ids=None))) == ["evt_1"]


class TestVisibility:
    """S5: a minor's session never receives an `adults` event. The one bug here
    that would actually matter, so it is asserted from several directions."""

    def test_all_scope_never_returns_an_adults_event(self, repos):
        repos.events.put(make_event(visibility=Visibility.ADULTS))

        page = repos.events.list_range(query(visibility_scope=Visibility.ALL))

        assert page.events == []

    def test_default_constructed_query_is_the_safe_one(self, repos):
        repos.events.put(make_event(visibility=Visibility.ADULTS))

        # Nothing passed for visibility at all - forgetting it must under-share.
        default = AgendaQuery(household_id=HH, start_utc=WINDOW_START, end_utc=WINDOW_END)

        assert default.visibility_scope is Visibility.ALL
        assert repos.events.list_range(default).events == []

    def test_adults_scope_does_return_it(self, repos):
        repos.events.put(make_event(visibility=Visibility.ADULTS))

        assert ids(repos.events.list_range(query(visibility_scope=Visibility.ADULTS))) == ["evt_1"]

    @pytest.mark.parametrize("include_deleted", [False, True])
    def test_no_other_flag_can_reveal_an_adults_event(self, repos, include_deleted):
        repos.events.put(make_event(visibility=Visibility.ADULTS))

        page = repos.events.list_range(
            query(
                member_ids=("mem_riley",),
                min_tier=Tier.BUSY,
                include_deleted=include_deleted,
            )
        )

        assert page.events == []

    def test_a_minors_own_scope_hides_an_adults_event_about_them(self, repos):
        minor = Member(
            member_id="mem_riley",
            household_id=HH,
            display_name="Riley",
            role=MemberRole.MINOR,
            color="#e0af68",
        )
        repos.events.put(
            make_event(
                title="Riley birthday surprise planning",
                owner_member_id="mem_alex",
                involves=["mem_riley"],
                visibility=Visibility.ADULTS,
            )
        )

        page = repos.events.list_range(query(visibility_scope=minor.visibility_scope()))

        assert page.events == []


class TestPagination:
    def test_cursor_walks_the_whole_window_without_repeats(self, repos):
        repo = repos.new_event_repo(page_size=2)
        for hour in range(5):
            repo.put(
                make_event(
                    event_id=f"evt_{hour}",
                    start_utc=datetime(2026, 8, 4, 9 + hour, tzinfo=UTC),
                    end_utc=datetime(2026, 8, 4, 9 + hour, 30, tzinfo=UTC),
                    source=EventSource(kind=SourceKind.NATIVE),
                )
            )

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = repo.list_range(query(), cursor=cursor)
            seen += ids(page)
            cursor = page.cursor
            if cursor is None:
                break

        assert cursor is None
        assert sorted(seen) == ["evt_0", "evt_1", "evt_2", "evt_3", "evt_4"]

    def test_exhausted_set_reports_no_cursor(self, repos):
        repos.events.put(make_event())

        assert repos.events.list_range(query()).cursor is None


class TestMemberRepo:
    def test_round_trip(self, repos):
        member = Member(
            member_id="mem_alex",
            household_id=HH,
            display_name="Alex",
            role=MemberRole.ADULT,
            color="#7aa2f7",
            cognito_sub="sub-123",
        )
        repos.members.put(member)

        assert asdict(repos.members.get(HH, "mem_alex")) == asdict(member)

    def test_list_is_ordered_by_member_id(self, repos):
        for member_id in ["mem_sam", "mem_alex", "mem_riley"]:
            repos.members.put(
                Member(
                    member_id=member_id,
                    household_id=HH,
                    display_name=member_id,
                    role=MemberRole.ADULT,
                    color="#fff",
                )
            )

        assert [m.member_id for m in repos.members.list(HH)] == [
            "mem_alex",
            "mem_riley",
            "mem_sam",
        ]

    def test_list_is_household_scoped(self, repos):
        repos.members.put(
            Member(
                member_id="mem_alex",
                household_id="hh_other",
                display_name="Alex",
                role=MemberRole.ADULT,
                color="#fff",
            )
        )

        assert repos.members.list(HH) == []

    def test_unknown_member_is_none(self, repos):
        assert repos.members.get(HH, "mem_nobody") is None


class TestSourceRepo:
    def test_round_trip(self, repos):
        source = Source(
            source_id="src_1",
            household_id=HH,
            kind=SourceKind.GOOGLE,
            owner_member_id="mem_alex",
            label="Work",
            default_tier=Tier.BUSY,
            cursor="sync-token-abc",
            last_sync_at=datetime(2026, 8, 1, 18, 22, 3, tzinfo=UTC),
            enabled=False,
        )
        repos.sources.put(source)

        assert asdict(repos.sources.get(HH, "src_1")) == asdict(source)

    def test_list_is_household_scoped(self, repos):
        repos.sources.put(
            Source(
                source_id="src_1",
                household_id=HH,
                kind=SourceKind.ICS,
                owner_member_id="mem_alex",
                label="School",
            )
        )

        assert [s.source_id for s in repos.sources.list(HH)] == ["src_1"]
        assert repos.sources.list("hh_other") == []


class TestSeed:
    """The M1 exit criterion, run against whichever backend is under test."""

    TODAY = date(2026, 8, 3)

    def week(self, **overrides) -> AgendaQuery:
        base = {
            "household_id": HOUSEHOLD_ID,
            "start_utc": datetime(2026, 8, 3, tzinfo=UTC),
            "end_utc": datetime(2026, 8, 11, tzinfo=UTC),
        }
        return AgendaQuery(**{**base, **overrides})

    @pytest.fixture
    def seeded(self, repos):
        seed_household(repos.events, repos.members, today=self.TODAY)
        return repos

    def test_roster_is_two_adults_and_a_minor(self, seeded):
        roster = seeded.members.list(HOUSEHOLD_ID)

        assert [m.member_id for m in roster] == [ALEX, RILEY, SAM]
        assert [m.role for m in roster if m.is_adult] == [MemberRole.ADULT] * 2
        assert seeded.members.get(HOUSEHOLD_ID, RILEY).role is MemberRole.MINOR

    def test_a_minor_never_sees_the_adults_only_event(self, seeded):
        minor = seeded.members.get(HOUSEHOLD_ID, RILEY)

        page = seeded.events.list_range(self.week(visibility_scope=minor.visibility_scope()))

        assert page.events
        assert all(e.visibility is Visibility.ALL for e in page.events)
        assert not any("surprise" in e.title for e in page.events)

    def test_an_adult_does_see_it(self, seeded):
        adult = seeded.members.get(HOUSEHOLD_ID, ALEX)

        page = seeded.events.list_range(self.week(visibility_scope=adult.visibility_scope()))

        assert any(e.visibility is Visibility.ADULTS for e in page.events)

    def test_one_adult_has_a_collapsible_stack_of_work_meetings(self, seeded):
        page = seeded.events.list_range(self.week(visibility_scope=Visibility.ADULTS))

        first_day_busy = [
            e
            for e in page.events
            if e.tier is Tier.BUSY
            and e.owner_member_id == ALEX
            and e.start_utc.date() == self.TODAY
        ]

        assert len(first_day_busy) > 1

    def test_the_week_exercises_every_tier(self, seeded):
        page = seeded.events.list_range(self.week(visibility_scope=Visibility.ADULTS))

        assert {e.tier for e in page.events} == {Tier.HOUSEHOLD, Tier.PERSONAL, Tier.BUSY}
        assert any(e.all_day for e in page.events)
        assert any(e.rrule for e in page.events)
        assert any(len({e.owner_member_id, *e.involves}) > 1 for e in page.events)

    def test_family_items_involve_more_than_one_member(self, seeded):
        page = seeded.events.list_range(
            self.week(visibility_scope=Visibility.ADULTS, member_ids=(SAM,))
        )

        assert any(e.owner_member_id != SAM for e in page.events)

    def test_reseeding_the_same_day_does_not_duplicate(self, seeded):
        before = len(seeded.events.list_range(self.week(visibility_scope=Visibility.ADULTS)).events)

        seed_household(seeded.events, seeded.members, today=self.TODAY)
        after = len(seeded.events.list_range(self.week(visibility_scope=Visibility.ADULTS)).events)

        assert after == before
