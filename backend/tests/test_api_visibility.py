"""PRD §6.2 / success criterion S5: a minor's session never receives an `adults` event.

These are the tests that matter most in the API. They are written from the outside — no
knowledge of how filtering is implemented — because the guarantee is about the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from airhead.domain import Tier, Visibility
from fakes import HOUSEHOLD, as_member, build_client, make_event


def _events():
    return [
        make_event(
            "evt_soccer",
            title="Soccer practice",
            start=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
            end=datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
            owner="mem_riley",
            involves=["mem_riley", "mem_sam"],
            tier=Tier.HOUSEHOLD,
            created_by="mem_sam",  # a parent put this on Riley's calendar
        ),
        make_event(
            "evt_secret",
            title="Riley birthday surprise planning",
            start=datetime(2026, 8, 6, 22, 0, tzinfo=UTC),
            end=datetime(2026, 8, 6, 23, 0, tzinfo=UTC),
            owner="mem_alex",
            involves=["mem_alex", "mem_sam"],
            tier=Tier.HOUSEHOLD,
            visibility=Visibility.ADULTS,
        ),
        make_event(
            "evt_alex_gym",
            title="Gym",
            start=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            end=datetime(2026, 8, 6, 13, 0, tzinfo=UTC),
            owner="mem_alex",
            tier=Tier.PERSONAL,
        ),
        make_event(
            "evt_riley_made",
            title="Study group",
            start=datetime(2026, 8, 7, 20, 0, tzinfo=UTC),
            end=datetime(2026, 8, 7, 21, 0, tzinfo=UTC),
            owner="mem_riley",
            tier=Tier.PERSONAL,
            created_by="mem_riley",
        ),
    ]


@pytest.fixture
def seeded():
    client, events, _ = build_client(_events())
    yield client, events
    client.app.dependency_overrides.clear()


def _agenda(client, member):
    return client.get(
        "/api/agenda",
        params={"start": "2026-08-04", "end": "2026-08-10"},
        headers=as_member(member),
    )


class TestMinorReads:
    def test_agenda_never_carries_an_adults_event(self, seeded):
        client, _ = seeded
        body = _agenda(client, "mem_riley").json()
        ids = [r.get("eventId") for d in body["days"] for r in d["rows"]]
        assert "evt_soccer" in ids
        assert "evt_secret" not in ids
        # Not just the id — no fragment of the row leaks anywhere in the payload.
        assert "surprise" not in _agenda(client, "mem_riley").text.lower()

    def test_adult_does_see_it(self, seeded):
        client, _ = seeded
        body = _agenda(client, "mem_alex").json()
        ids = [r.get("eventId") for d in body["days"] for r in d["rows"]]
        assert "evt_secret" in ids

    def test_single_get_is_404_not_403(self, seeded):
        """A 403 here would confirm the event exists. The absence of a distinguishable
        response is the control; 'exists but forbidden' is itself the leak."""
        client, _ = seeded
        forbidden = client.get("/api/events/evt_secret", headers=as_member("mem_riley"))
        missing = client.get("/api/events/evt_nope", headers=as_member("mem_riley"))
        assert forbidden.status_code == 404
        assert forbidden.json() == missing.json()

    def test_patch_of_an_invisible_event_is_404(self, seeded):
        client, _ = seeded
        r = client.patch(
            "/api/events/evt_secret", headers=as_member("mem_riley"), json={"tier": "T3"}
        )
        assert r.status_code == 404

    def test_delete_of_an_invisible_event_is_404(self, seeded):
        client, events = seeded
        r = client.delete("/api/events/evt_secret", headers=as_member("mem_riley"))
        assert r.status_code == 404
        assert not events.get(HOUSEHOLD, "evt_secret").is_deleted


class TestMinorWrites:
    def test_cannot_patch_another_members_event(self, seeded):
        client, _ = seeded
        r = client.patch(
            "/api/events/evt_alex_gym", headers=as_member("mem_riley"), json={"title": "Nap"}
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"

    def test_can_patch_own_event(self, seeded):
        client, _ = seeded
        r = client.patch(
            "/api/events/evt_riley_made", headers=as_member("mem_riley"), json={"tier": "T1"}
        )
        assert r.status_code == 200
        assert r.json()["tierSource"] == "human"

    def test_cannot_change_visibility_even_on_own_event(self, seeded):
        client, _ = seeded
        r = client.patch(
            "/api/events/evt_riley_made",
            headers=as_member("mem_riley"),
            json={"visibility": "adults"},
        )
        assert r.status_code == 403

    def test_cannot_downgrade_visibility_to_all(self, seeded):
        client, _ = seeded
        r = client.patch(
            "/api/events/evt_riley_made", headers=as_member("mem_riley"), json={"visibility": "all"}
        )
        assert r.status_code == 403

    def test_cannot_delete_an_event_they_did_not_create(self, seeded):
        client, events = seeded
        r = client.delete("/api/events/evt_soccer", headers=as_member("mem_riley"))
        assert r.status_code == 403
        assert not events.get(HOUSEHOLD, "evt_soccer").is_deleted

    def test_can_delete_own_creation(self, seeded):
        client, _ = seeded
        r = client.delete("/api/events/evt_riley_made", headers=as_member("mem_riley"))
        assert r.status_code == 204

    def test_cannot_create_for_another_member(self, seeded):
        client, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_riley"),
            json={
                "title": "Dad drives me",
                "startLocal": "2026-08-08T09:00:00",
                "endLocal": "2026-08-08T10:00:00",
                "ownerMemberId": "mem_alex",
            },
        )
        assert r.status_code == 403

    def test_cannot_create_an_adults_only_event(self, seeded):
        client, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_riley"),
            json={
                "title": "Hidden",
                "startLocal": "2026-08-08T09:00:00",
                "endLocal": "2026-08-08T10:00:00",
                "visibility": "adults",
            },
        )
        assert r.status_code == 403

    def test_can_create_own_event(self, seeded):
        client, _ = seeded
        r = client.post(
            "/api/events",
            headers=as_member("mem_riley"),
            json={
                "title": "Band practice",
                "startLocal": "2026-08-08T09:00:00",
                "endLocal": "2026-08-08T10:00:00",
            },
        )
        assert r.status_code == 201
        assert r.json()["ownerMemberId"] == "mem_riley"


class TestAdultWrites:
    def test_adult_may_set_visibility(self, seeded):
        client, _ = seeded
        r = client.patch(
            "/api/events/evt_soccer", headers=as_member("mem_alex"), json={"visibility": "adults"}
        )
        assert r.status_code == 200
        assert r.json()["visibility"] == "adults"
        # And it immediately drops out of the minor's view.
        assert (
            client.get("/api/events/evt_soccer", headers=as_member("mem_riley")).status_code == 404
        )

    def test_adult_may_delete_someone_elses_event(self, seeded):
        client, _ = seeded
        r = client.delete("/api/events/evt_riley_made", headers=as_member("mem_alex"))
        assert r.status_code == 204
