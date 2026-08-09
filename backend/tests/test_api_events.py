from __future__ import annotations

from datetime import UTC, datetime

import pytest

from airhead.domain import EventStatus, Tier, TierSource, Visibility
from fakes import HOUSEHOLD, as_member, build_client, make_event


@pytest.fixture
def seeded():
    event = make_event(
        "evt_soccer",
        start=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
        end=datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
        owner="mem_riley",
        involves=["mem_riley", "mem_sam"],
        location="Riverside Park Field 3",
    )
    client, events, members = build_client([event])
    yield client, events, members
    client.app.dependency_overrides.clear()


class TestAuthShim:
    def test_missing_header_is_401(self, seeded):
        client, _, _ = seeded
        r = client.get("/api/members")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"

    def test_unknown_member_is_401(self, seeded):
        client, _, _ = seeded
        r = client.get("/api/members", headers=as_member("mem_nobody"))
        assert r.status_code == 401

    def test_healthz_needs_no_auth(self, seeded):
        client, _, _ = seeded
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_members_roster(self, seeded):
        client, _, _ = seeded
        body = client.get("/api/members", headers=as_member("mem_alex")).json()
        assert [m["memberId"] for m in body["members"]] == ["mem_alex", "mem_riley", "mem_sam"]
        assert body["members"][0]["displayName"] == "Alex"


class TestGetEvent:
    def test_camelcase_row_shape(self, seeded):
        client, _, _ = seeded
        r = client.get("/api/events/evt_soccer", headers=as_member("mem_alex"))
        assert r.status_code == 200
        row = r.json()
        assert row["kind"] == "event"
        assert row["eventId"] == "evt_soccer"
        assert row["startLocal"] == "2026-08-06T16:00:00"  # 20:00Z in America/New_York
        assert row["endLocal"] == "2026-08-06T17:30:00"
        assert row["startUtc"] == "2026-08-06T20:00:00Z"
        assert row["allDay"] is False
        assert row["ownerMemberId"] == "mem_riley"
        assert row["memberIds"] == ["mem_riley", "mem_sam"]
        assert row["tierSource"] == "auto"
        assert row["isFamily"] is True  # >1 member and T1

    def test_unknown_event_is_404(self, seeded):
        client, _, _ = seeded
        r = client.get("/api/events/evt_nope", headers=as_member("mem_alex"))
        assert r.status_code == 404
        assert r.json() == {"error": {"code": "not_found", "message": "Event not found."}}


class TestCreate:
    def test_creates_and_returns_201(self, seeded):
        client, events, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_alex"),
            json={
                "title": "Dentist",
                "startLocal": "2026-08-07T09:00:00",
                "endLocal": "2026-08-07T10:00:00",
                "involves": ["mem_alex", "mem_riley"],
                "tier": "T1",
            },
        )
        assert r.status_code == 201
        row = r.json()
        assert row["ownerMemberId"] == "mem_alex"
        assert row["startUtc"] == "2026-08-07T13:00:00Z"
        assert row["memberIds"] == ["mem_alex", "mem_riley"]
        stored = events.get(HOUSEHOLD, row["eventId"])
        assert stored.created_by == "mem_alex"
        assert stored.visibility is Visibility.ALL

    def test_all_day_carries_plain_dates(self, seeded):
        client, _, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_alex"),
            json={"title": "Camp", "date": "2026-08-10", "allDay": True},
        )
        assert r.status_code == 201
        row = r.json()
        assert row["allDay"] is True
        assert row["startLocal"] == "2026-08-10"
        assert row["endLocal"] == "2026-08-10"  # inclusive, not the exclusive next midnight

    def test_explicit_tier_is_a_human_tier(self, seeded):
        client, events, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_alex"),
            json={
                "title": "Flight",
                "startLocal": "2026-08-09T06:00:00",
                "endLocal": "2026-08-09T09:00:00",
                "tier": "T1",
            },
        )
        assert events.get(HOUSEHOLD, r.json()["eventId"]).tier_source is TierSource.HUMAN

    def test_minor_creating_their_own_event_is_confirmed(self, seeded):
        client, events, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_riley"),
            json={
                "title": "Band practice",
                "startLocal": "2026-08-07T15:00:00",
                "endLocal": "2026-08-07T16:00:00",
            },
        )
        assert r.status_code == 201
        assert r.json()["status"] == "confirmed"
        assert events.get(HOUSEHOLD, r.json()["eventId"]).status is EventStatus.CONFIRMED

    def test_adult_creating_for_anyone_is_confirmed(self, seeded):
        client, events, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_alex"),
            json={
                "title": "Soccer pickup",
                "startLocal": "2026-08-07T18:00:00",
                "endLocal": "2026-08-07T18:30:00",
                "ownerMemberId": "mem_riley",
            },
        )
        assert r.status_code == 201
        assert r.json()["status"] == "confirmed"
        assert events.get(HOUSEHOLD, r.json()["eventId"]).status is EventStatus.CONFIRMED

    def test_end_before_start_is_422(self, seeded):
        client, _, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_alex"),
            json={
                "title": "Backwards",
                "startLocal": "2026-08-07T10:00:00",
                "endLocal": "2026-08-07T09:00:00",
            },
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_error"

    def test_invalid_tz_is_422(self, seeded):
        client, _, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_alex"),
            json={
                "title": "Nowhere",
                "startLocal": "2026-08-07T10:00:00",
                "endLocal": "2026-08-07T11:00:00",
                "tz": "Mars/Olympus_Mons",
            },
        )
        assert r.status_code == 422

    def test_unknown_involves_is_422(self, seeded):
        client, _, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_alex"),
            json={
                "title": "Ghost",
                "startLocal": "2026-08-07T10:00:00",
                "endLocal": "2026-08-07T11:00:00",
                "involves": ["mem_ghost"],
            },
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_error"

    def test_error_body_never_echoes_the_title(self, seeded):
        client, _, _ = seeded
        secret = "Divorce lawyer consultation"
        r = client.post(
            "/api/events",
            headers=as_member("mem_alex"),
            json={"title": secret, "startLocal": "2026-08-07T10:00:00"},
        )
        assert r.status_code == 422
        assert secret not in r.text


class TestPatch:
    def test_tier_patch_stamps_human(self, seeded):
        client, events, _ = seeded
        r = client.patch(
            "/api/events/evt_soccer", headers=as_member("mem_alex"), json={"tier": "T3"}
        )
        assert r.status_code == 200
        assert r.json()["tierSource"] == "human"
        stored = events.get(HOUSEHOLD, "evt_soccer")
        assert stored.tier is Tier.BUSY
        assert stored.tier_source is TierSource.HUMAN

    def test_partial_patch_leaves_other_fields(self, seeded):
        client, events, _ = seeded
        r = client.patch(
            "/api/events/evt_soccer",
            headers=as_member("mem_alex"),
            json={"title": "Soccer game"},
        )
        assert r.json()["title"] == "Soccer game"
        assert events.get(HOUSEHOLD, "evt_soccer").tier is Tier.HOUSEHOLD

    def test_patch_times_reconverts_to_utc(self, seeded):
        client, _, _ = seeded
        r = client.patch(
            "/api/events/evt_soccer",
            headers=as_member("mem_alex"),
            json={"startLocal": "2026-08-06T18:00:00", "endLocal": "2026-08-06T19:00:00"},
        )
        assert r.json()["startUtc"] == "2026-08-06T22:00:00Z"

    def test_patch_end_before_start_is_422(self, seeded):
        client, _, _ = seeded
        r = client.patch(
            "/api/events/evt_soccer",
            headers=as_member("mem_alex"),
            json={"startLocal": "2026-08-06T18:00:00", "endLocal": "2026-08-06T17:00:00"},
        )
        assert r.status_code == 422

    def test_patch_unknown_event_is_404(self, seeded):
        client, _, _ = seeded
        r = client.patch("/api/events/evt_nope", headers=as_member("mem_alex"), json={"tier": "T1"})
        assert r.status_code == 404

    def test_unknown_field_is_rejected(self, seeded):
        client, _, _ = seeded
        r = client.patch(
            "/api/events/evt_soccer", headers=as_member("mem_alex"), json={"tierSource": "auto"}
        )
        assert r.status_code == 422


class TestDelete:
    def test_soft_deletes_and_then_404s(self, seeded):
        client, events, _ = seeded
        assert (
            client.delete("/api/events/evt_soccer", headers=as_member("mem_alex")).status_code
            == 204
        )
        assert events.get(HOUSEHOLD, "evt_soccer").is_deleted
        assert (
            client.get("/api/events/evt_soccer", headers=as_member("mem_alex")).status_code == 404
        )

    def test_delete_unknown_is_404(self, seeded):
        client, _, _ = seeded
        assert (
            client.delete("/api/events/evt_nope", headers=as_member("mem_alex")).status_code == 404
        )


class TestPreflight:
    """API Gateway's $default route hands the browser's OPTIONS preflight to the
    app. If it 405s, every cross-origin request from the kitchen display fails -
    and it fails only in a browser, so nothing else in this suite would catch it."""

    def test_preflight_is_not_rejected(self, seeded):
        client, _, _ = seeded
        r = client.options(
            "/api/agenda",
            headers={
                "Origin": "https://airhead.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-airhead-member",
            },
        )
        assert r.status_code == 204

    def test_preflight_needs_no_actor_header(self, seeded):
        """A preflight carries no credentials, so requiring the shim would reject
        every cross-origin call before the real request was ever made."""
        client, _, _ = seeded
        assert client.options("/api/events/evt_soccer").status_code == 204


class TestMinorProposal:
    """Issue #4: a minor creating an event for another member lands a proposal.

    PRD §6.2 keeps the binding moves adult-only; a proposal that only an adult
    can turn into a confirmed event is the ask without the authority.
    """

    PICKUP = {
        "title": "Pick me up at 6",
        "startLocal": "2026-08-07T18:00:00",
        "endLocal": "2026-08-07T18:15:00",
        "ownerMemberId": "mem_alex",
    }

    def _propose(self, client):
        r = client.post("/api/events", headers=as_member("mem_riley"), json=self.PICKUP)
        assert r.status_code == 201
        return r.json()

    def test_minor_creating_for_a_parent_is_a_pending_proposal(self, seeded):
        client, events, _ = seeded
        row = self._propose(client)
        assert row["status"] == "proposed"
        assert row["ownerMemberId"] == "mem_alex"
        stored = events.get(HOUSEHOLD, row["eventId"])
        assert stored.status is EventStatus.PROPOSED
        assert stored.created_by == "mem_riley"

    def test_proposal_is_not_shown_as_confirmed_until_an_adult_approves(self, seeded):
        client, _, _ = seeded
        row = self._propose(client)
        fetched = client.get(f"/api/events/{row['eventId']}", headers=as_member("mem_alex")).json()
        assert fetched["status"] == "proposed"

    def test_adult_confirms_the_proposal(self, seeded):
        client, events, _ = seeded
        row = self._propose(client)
        r = client.post(f"/api/events/{row['eventId']}/confirm", headers=as_member("mem_alex"))
        assert r.status_code == 200
        assert r.json()["status"] == "confirmed"
        assert events.get(HOUSEHOLD, row["eventId"]).status is EventStatus.CONFIRMED

    def test_minor_cannot_confirm(self, seeded):
        client, events, _ = seeded
        row = self._propose(client)
        r = client.post(f"/api/events/{row['eventId']}/confirm", headers=as_member("mem_riley"))
        assert r.status_code == 403
        assert events.get(HOUSEHOLD, row["eventId"]).status is EventStatus.PROPOSED

    def test_confirming_a_confirmed_event_is_409(self, seeded):
        client, _, _ = seeded
        r = client.post("/api/events/evt_soccer/confirm", headers=as_member("mem_alex"))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_proposed"

    def test_minor_can_delete_their_own_proposal(self, seeded):
        # `created_by` is the delete rule, so a minor can withdraw the ask.
        client, _, _ = seeded
        row = self._propose(client)
        r = client.delete(f"/api/events/{row['eventId']}", headers=as_member("mem_riley"))
        assert r.status_code == 204
