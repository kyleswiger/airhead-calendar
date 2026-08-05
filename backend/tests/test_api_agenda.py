from __future__ import annotations

from datetime import UTC, datetime

import pytest

from airhead.domain import Tier
from fakes import as_member, build_client, make_event


def _events():
    return [
        make_event(
            "evt_soccer",
            title="Soccer practice",
            start=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),  # 16:00 local
            end=datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
            owner="mem_riley",
            involves=["mem_riley", "mem_sam"],
            tier=Tier.HOUSEHOLD,
        ),
        make_event(
            "evt_gym",
            title="Gym",
            start=datetime(2026, 8, 6, 23, 0, tzinfo=UTC),  # 19:00 local
            end=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            owner="mem_sam",
            tier=Tier.PERSONAL,
        ),
        make_event(
            "evt_standup",
            title="Standup",
            start=datetime(2026, 8, 6, 13, 0, tzinfo=UTC),  # 09:00 local
            end=datetime(2026, 8, 6, 14, 0, tzinfo=UTC),
            owner="mem_alex",
            tier=Tier.BUSY,
        ),
        make_event(
            "evt_review",
            title="Design review",
            start=datetime(2026, 8, 6, 18, 0, tzinfo=UTC),  # 14:00 local
            end=datetime(2026, 8, 6, 19, 0, tzinfo=UTC),
            owner="mem_alex",
            tier=Tier.BUSY,
        ),
    ]


@pytest.fixture
def client():
    c, _, _ = build_client(_events())
    yield c
    c.app.dependency_overrides.clear()


def _agenda(client, member="mem_alex", **params):
    query = {"start": "2026-08-04", "end": "2026-08-10", **params}
    return client.get("/api/agenda", params=query, headers=as_member(member))


def _day(body, iso):
    return next(d for d in body["days"] if d["date"] == iso)


class TestAgendaShape:
    def test_range_members_and_days(self, client):
        r = _agenda(client)
        assert r.status_code == 200
        body = r.json()
        assert body["range"] == {
            "start": "2026-08-04",
            "end": "2026-08-10",
            "tz": "America/New_York",
        }
        assert [m["memberId"] for m in body["members"]] == ["mem_alex", "mem_riley", "mem_sam"]
        assert len(body["days"]) == 7

    def test_event_row_fields(self, client):
        day = _day(_agenda(client).json(), "2026-08-06")
        row = next(r for r in day["rows"] if r.get("eventId") == "evt_soccer")
        assert row["kind"] == "event"
        assert row["startLocal"] == "2026-08-06T16:00:00"
        assert row["startUtc"] == "2026-08-06T20:00:00Z"
        assert row["memberIds"] == ["mem_riley", "mem_sam"]
        assert row["isFamily"] is True

    def test_t3_collapses_into_one_truthful_busy_band(self, client):
        day = _day(_agenda(client).json(), "2026-08-06")
        bands = [r for r in day["rows"] if r["kind"] == "busy"]
        assert len(bands) == 1
        band = bands[0]
        assert band["memberId"] == "mem_alex"
        assert band["startLocal"] == "2026-08-06T09:00:00"
        assert band["endLocal"] == "2026-08-06T15:00:00"
        assert band["count"] == 2
        assert sorted(band["eventIds"]) == ["evt_review", "evt_standup"]
        # Nothing is hidden: the T3 titles are not emitted as their own rows.
        assert "evt_standup" not in [r.get("eventId") for r in day["rows"]]

    def test_busy_bands_precede_event_rows(self, client):
        day = _day(_agenda(client).json(), "2026-08-06")
        kinds = [r["kind"] for r in day["rows"]]
        assert kinds == sorted(kinds, key=lambda k: 0 if k == "busy" else 1)

    def test_empty_day_has_no_rows(self, client):
        assert _day(_agenda(client).json(), "2026-08-05")["rows"] == []


class TestAgendaFilters:
    def test_min_tier_t1_drops_personal_and_busy(self, client):
        day = _day(_agenda(client, minTier="T1").json(), "2026-08-06")
        assert [r.get("eventId") for r in day["rows"] if r["kind"] == "event"] == ["evt_soccer"]
        assert [r for r in day["rows"] if r["kind"] == "busy"] == []

    def test_member_id_narrows_roster_and_rows(self, client):
        body = _agenda(client, memberId="mem_alex").json()
        assert [m["memberId"] for m in body["members"]] == ["mem_alex"]
        day = _day(body, "2026-08-06")
        assert [r["memberId"] for r in day["rows"] if r["kind"] == "busy"] == ["mem_alex"]
        assert [r for r in day["rows"] if r["kind"] == "event"] == []

    def test_repeated_member_id(self, client):
        r = client.get(
            "/api/agenda",
            params=[
                ("start", "2026-08-04"),
                ("end", "2026-08-10"),
                ("memberId", "mem_riley"),
                ("memberId", "mem_sam"),
            ],
            headers=as_member("mem_alex"),
        )
        assert [m["memberId"] for m in r.json()["members"]] == ["mem_riley", "mem_sam"]

    def test_unknown_member_id_is_422(self, client):
        assert _agenda(client, memberId="mem_ghost").status_code == 422


class TestAgendaValidation:
    def test_span_over_31_days_is_range_too_large(self, client):
        r = _agenda(client, start="2026-08-01", end="2026-09-05")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "range_too_large"

    def test_exactly_31_days_is_allowed(self, client):
        assert _agenda(client, start="2026-08-01", end="2026-08-31").status_code == 200

    def test_end_before_start_is_400(self, client):
        r = _agenda(client, start="2026-08-10", end="2026-08-04")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "bad_request"

    def test_missing_start_is_422(self, client):
        r = client.get("/api/agenda", params={"end": "2026-08-10"}, headers=as_member("mem_alex"))
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_error"

    def test_garbage_date_is_422(self, client):
        assert _agenda(client, start="last tuesday").status_code == 422

    def test_bad_min_tier_is_422(self, client):
        assert _agenda(client, minTier="T9").status_code == 422

    def test_agenda_requires_auth(self, client):
        r = client.get("/api/agenda", params={"start": "2026-08-04", "end": "2026-08-10"})
        assert r.status_code == 401
