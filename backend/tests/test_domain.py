from datetime import UTC, datetime

import pytest

from airhead.domain import (
    Event,
    EventSource,
    SourceKind,
    Tier,
    TierSource,
    Visibility,
    apply_remote_update,
)


def make_event(**overrides) -> Event:
    base = {
        "event_id": "evt_1",
        "household_id": "hh_1",
        "title": "Soccer practice",
        "start_utc": datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
        "tz": "America/New_York",
        "owner_member_id": "mem_riley",
        "source": EventSource(kind=SourceKind.GOOGLE, external_id="ext_1"),
    }
    return Event(**{**base, **overrides})


class TestContentHash:
    def test_is_stable_across_identical_events(self):
        assert make_event().content_hash() == make_event().content_hash()

    def test_ignores_locally_owned_fields(self):
        """Tier and visibility are ours, not the remote source's."""
        local = make_event(tier=Tier.HOUSEHOLD, visibility=Visibility.ADULTS)
        assert local.content_hash() == make_event().content_hash()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("title", "Soccer game"),
            ("location", "Riverside Park"),
            ("rrule", "FREQ=WEEKLY;BYDAY=TH"),
            ("all_day", True),
        ],
    )
    def test_changes_when_remote_field_changes(self, field, value):
        assert make_event(**{field: value}).content_hash() != make_event().content_hash()

    def test_ignores_surrounding_whitespace(self):
        assert make_event(title="  Soccer practice  ").content_hash() == make_event().content_hash()


class TestApplyRemoteUpdate:
    def test_human_tier_override_survives_resync(self):
        existing = make_event(tier=Tier.HOUSEHOLD, tier_source=TierSource.HUMAN)
        incoming = make_event(tier=Tier.BUSY, tier_source=TierSource.AUTO)

        result = apply_remote_update(existing, incoming)

        assert result.tier is Tier.HOUSEHOLD
        assert result.tier_source is TierSource.HUMAN

    def test_auto_tier_is_replaced_by_resync(self):
        existing = make_event(tier=Tier.PERSONAL, tier_source=TierSource.AUTO)
        incoming = make_event(tier=Tier.BUSY, tier_source=TierSource.AUTO)

        assert apply_remote_update(existing, incoming).tier is Tier.BUSY

    def test_local_only_fields_are_carried_over(self):
        existing = make_event(
            visibility=Visibility.ADULTS,
            merge_group_id="mg_1",
            created_by="mem_alex",
        )
        incoming = make_event(visibility=Visibility.ALL)

        result = apply_remote_update(existing, incoming)

        assert result.visibility is Visibility.ADULTS
        assert result.merge_group_id == "mg_1"
        assert result.created_by == "mem_alex"
        assert result.event_id == "evt_1"

    def test_remote_fields_do_update(self):
        existing = make_event(title="Soccer practice")
        incoming = make_event(title="Soccer game", location="Field 3")

        result = apply_remote_update(existing, incoming)

        assert result.title == "Soccer game"
        assert result.location == "Field 3"
