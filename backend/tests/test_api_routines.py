"""HTTP contract tests for /api/routines (docs/ROUTINES-CONTRACT.md).

The clock is pinned by monkeypatching `airhead.api.routines.household_today`, which is
the one place both the routines router and `GET /api/agenda` read "today" from.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from airhead.api import deps
from airhead.api import routines as routines_api
from airhead.domain import IntervalSource, Visibility
from airhead.routines import estimate as estimate_mod
from airhead.routines import service
from fakes import HOUSEHOLD, InMemoryRoutineRepo, as_member, build_client, make_event

TODAY = date(2026, 9, 20)
ADULT = as_member("mem_alex")
MINOR = as_member("mem_riley")


class FakeEstimator:
    """The `deps.get_estimator` seam: records calls, serves one canned resolution."""

    def __init__(self, resolution: Any = None) -> None:
        self.resolution = resolution
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, name: str, context: str | None) -> Any:
        self.calls.append((name, context))
        if self.resolution is None:
            raise AssertionError("the model must not be called on a catalog hit")
        return self.resolution


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(routines_api, "household_today", lambda tz: TODAY)
    client, events, members = build_client([])
    estimator = FakeEstimator()
    client.app.dependency_overrides[deps.get_estimator] = lambda: estimator
    routines: InMemoryRoutineRepo = client.app.dependency_overrides[deps.get_routine_repo]()
    yield SimpleNamespace(
        client=client, events=events, members=members, routines=routines, estimator=estimator
    )
    client.app.dependency_overrides.clear()


def create(api, headers=ADULT, **body):
    body.setdefault("name", "Haircut")
    return api.client.post("/api/routines", headers=headers, json=body)


def listing(api, headers=ADULT) -> list[dict]:
    r = api.client.get("/api/routines", headers=headers)
    assert r.status_code == 200
    return r.json()["routines"]


def agenda(api, headers=ADULT, start="2026-09-14", end="2026-09-27") -> list[dict]:
    r = api.client.get("/api/agenda", params={"start": start, "end": end}, headers=headers)
    assert r.status_code == 200
    return [row for day in r.json()["days"] for row in day["rows"]]


# --- create ------------------------------------------------------------------


class TestCreate:
    def test_catalog_match_fills_interval_and_projects_due_event(self, api):
        r = create(api, name="Cabin air filter 2023 Kia EV6", lastDoneOn="2026-09-20")
        assert r.status_code == 201
        row = r.json()
        assert row["routineId"].startswith("rtn_")
        assert row["ownerMemberId"] == "mem_alex"
        assert row["memberIds"] == ["mem_alex"]
        assert row["intervalDays"] == 365
        assert row["intervalSource"] == "catalog"
        assert row["catalogKey"] == "ev6_cabin_air_filter"
        assert row["category"] == "vehicle"
        assert row["intervalNote"]
        assert row["intervalConfidence"] is None
        assert row["anchor"] == "elapsed"
        assert row["lastDoneOn"] == "2026-09-20"
        assert row["dueOn"] == "2027-09-20"
        assert row["status"] == "ok"
        assert row["daysUntilDue"] == 365
        assert row["completionCount"] == 1
        assert row["paused"] is False
        assert row["tier"] == "T2"
        assert row["visibility"] == "all"
        # The due date is a real all-day event owned by the routine.
        event = api.events.get(HOUSEHOLD, row["dueEventId"])
        assert event is not None and event.all_day
        assert event.routine_id == row["routineId"]
        assert event.start_utc == datetime(2027, 9, 20, tzinfo=UTC)
        assert api.routines.get(HOUSEHOLD, row["routineId"]).created_by == "mem_alex"

    def test_interval_days_is_human_and_beats_the_catalog(self, api):
        r = create(api, name="Haircut", intervalDays=21, intervalNote="Short sides")
        assert r.status_code == 201
        row = r.json()
        assert row["intervalDays"] == 21
        assert row["intervalSource"] == "human"
        assert row["intervalNote"] == "Short sides"
        assert row["catalogKey"]  # the catalog still labels it
        assert row["lastDoneOn"] is None
        assert row["dueOn"] == TODAY.isoformat()  # an interval with no history is due now
        assert row["status"] == "due_soon"
        assert row["completionCount"] == 0

    def test_unknown_name_without_interval_is_unscheduled_and_calls_no_model(self, api):
        r = create(api, name="Wax the surfboard")
        assert r.status_code == 201
        row = r.json()
        assert row["intervalDays"] is None
        assert row["intervalSource"] is None
        assert row["status"] == "unscheduled"
        assert row["dueOn"] is None
        assert row["daysUntilDue"] is None
        assert row["dueEventId"] is None
        assert api.estimator.calls == []

    def test_estimate_passthrough_is_stored_as_estimated(self, api):
        r = create(
            api,
            name="Wax the surfboard",
            intervalDays=60,
            intervalSource="estimated",
            intervalNote="Manufacturer suggests every 2 months.",
            intervalConfidence=0.6,
        )
        assert r.status_code == 201
        row = r.json()
        assert row["intervalSource"] == "estimated"
        assert row["intervalDays"] == 60
        assert row["intervalConfidence"] == 0.6
        assert row["intervalNote"] == "Manufacturer suggests every 2 months."

    def test_explicit_due_on_and_calendar_anchor(self, api):
        r = create(
            api,
            name="Holiday lights up",
            anchor="calendar",
            intervalDays=365,
            dueOn="2026-11-27",
            tier="T1",
            involves=["mem_sam", "mem_riley"],
        )
        assert r.status_code == 201
        row = r.json()
        assert row["anchor"] == "calendar"
        assert row["dueOn"] == "2026-11-27"
        assert row["memberIds"] == ["mem_alex", "mem_sam", "mem_riley"]
        assert row["tier"] == "T1"
        event = api.events.get(HOUSEHOLD, row["dueEventId"])
        assert event.involves == ["mem_sam", "mem_riley"]
        assert event.tier.value == "T1"

    def test_future_last_done_on_is_400_and_leaves_nothing_behind(self, api):
        r = create(api, name="Haircut", lastDoneOn="2026-09-21")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "future_completion"
        assert api.routines.list(HOUSEHOLD, include_deleted=True) == []

    def test_validation(self, api):
        assert create(api, name="").status_code == 422
        assert create(api, name="   ").status_code == 422
        assert create(api, intervalDays=0).status_code == 422
        assert create(api, name="x", bogus=1).status_code == 422
        r = create(api, involves=["mem_nobody"])
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_error"
        assert create(api, ownerMemberId="mem_nobody").status_code == 422

    def test_minor_may_create_their_own(self, api):
        r = create(api, headers=MINOR, name="Clean hamster cage")
        assert r.status_code == 201
        assert r.json()["ownerMemberId"] == "mem_riley"

    def test_minor_creating_for_someone_else_is_403(self, api):
        r = create(api, headers=MINOR, name="Haircut", ownerMemberId="mem_alex")
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"

    def test_minor_may_not_set_visibility(self, api):
        r = create(api, headers=MINOR, name="Haircut", visibility="adults")
        assert r.status_code == 403
        assert create(api, headers=MINOR, name="Haircut", visibility="all").status_code == 403

    def test_adult_may_create_for_another_member(self, api):
        r = create(api, name="Haircut", ownerMemberId="mem_riley", visibility="adults")
        assert r.status_code == 201
        assert r.json()["ownerMemberId"] == "mem_riley"
        assert r.json()["visibility"] == "adults"

    def test_requires_auth(self, api):
        assert api.client.post("/api/routines", json={"name": "x"}).status_code == 401


# --- list --------------------------------------------------------------------


class TestList:
    def test_order_and_reproject(self, api):
        overdue = create(api, name="Overdue", intervalDays=10, lastDoneOn="2026-08-01").json()
        very_overdue = create(api, name="Very", intervalDays=10, lastDoneOn="2026-07-01").json()
        soon = create(api, name="Soon", intervalDays=10, lastDoneOn="2026-09-15").json()
        ok = create(api, name="Ok", intervalDays=100, lastDoneOn="2026-09-01").json()
        later = create(api, name="Later", intervalDays=300, lastDoneOn="2026-09-01").json()
        unsched = create(api, name="Mystery thing nobody knows").json()
        paused = create(api, name="Paused", intervalDays=10).json()
        api.client.patch(
            f"/api/routines/{paused['routineId']}", headers=ADULT, json={"paused": True}
        )

        rows = listing(api)
        assert [r["name"] for r in rows] == [
            "Very",
            "Overdue",
            "Soon",
            "Ok",
            "Later",
            "Mystery thing nobody knows",
            "Paused",
        ]
        assert [r["status"] for r in rows] == [
            "overdue",
            "overdue",
            "due_soon",
            "ok",
            "ok",
            "unscheduled",
            "paused",
        ]
        assert rows[0]["daysUntilDue"] == (date(2026, 7, 11) - TODAY).days
        assert rows[1]["dueOn"] == "2026-08-11"  # dueOn is the truth ...
        # ... while the due *event* has been rolled forward onto today (ground rule 3).
        event = api.events.get(HOUSEHOLD, rows[1]["dueEventId"])
        assert event.start_utc.date() == TODAY
        assert {overdue["routineId"], very_overdue["routineId"]} == {
            rows[0]["routineId"],
            rows[1]["routineId"],
        }
        assert soon["dueOn"] == "2026-09-25" and ok["dueOn"] and later["dueOn"]
        assert unsched["dueEventId"] is None
        # Pausing soft-deleted the due event.
        assert api.events.get(HOUSEHOLD, paused["dueEventId"]).is_deleted
        assert rows[-1]["dueEventId"] is None

    def test_minor_never_sees_an_adults_routine(self, api):
        create(api, name="Marriage counselling", intervalDays=30, visibility="adults")
        mine = create(api, headers=MINOR, name="Clean hamster cage", intervalDays=7).json()
        assert [r["routineId"] for r in listing(api, MINOR)] == [mine["routineId"]]
        assert len(listing(api)) == 2

    def test_deleted_routines_are_gone(self, api):
        row = create(api, name="Haircut").json()
        api.client.delete(f"/api/routines/{row['routineId']}", headers=ADULT)
        assert listing(api) == []


# --- agenda integration ------------------------------------------------------


class TestAgenda:
    def test_due_event_row_carries_routine_id(self, api):
        row = create(api, name="Haircut", intervalDays=35, lastDoneOn="2026-08-20").json()
        assert row["dueOn"] == "2026-09-24"
        rows = agenda(api)
        due = [r for r in rows if r.get("routineId")]
        assert len(due) == 1
        assert due[0]["routineId"] == row["routineId"]
        assert due[0]["eventId"] == row["dueEventId"]
        assert due[0]["kind"] == "event"
        assert due[0]["allDay"] is True
        assert due[0]["startLocal"] == "2026-09-24"
        assert due[0]["title"] == "Haircut"
        # Ordinary events have no routineId.
        assert all("routineId" in r for r in rows if r["kind"] == "event")

    def test_overdue_rolls_forward_onto_today(self, api):
        row = create(api, name="Haircut", intervalDays=35, lastDoneOn="2026-07-01").json()
        assert row["dueOn"] == "2026-08-05"
        # Not in the window on its nominal date ...
        assert agenda(api, start="2026-08-01", end="2026-08-10") == []
        # ... but on today, every day, until it is done.
        rows = agenda(api, start=TODAY.isoformat(), end=TODAY.isoformat())
        assert [r["routineId"] for r in rows] == [row["routineId"]]
        assert rows[0]["startLocal"] == TODAY.isoformat()
        # And the read is idempotent: a second pass writes nothing new.
        before = api.events.get(HOUSEHOLD, row["dueEventId"]).updated_at
        agenda(api, start=TODAY.isoformat(), end=TODAY.isoformat())
        assert api.events.get(HOUSEHOLD, row["dueEventId"]).updated_at == before

    def test_minor_agenda_hides_adults_due_event(self, api):
        row = create(api, name="Marriage counselling", intervalDays=30, visibility="adults").json()
        assert row["dueOn"] == TODAY.isoformat()
        assert [r["eventId"] for r in agenda(api)] == [row["dueEventId"]]
        assert agenda(api, MINOR) == []

    def test_plain_events_still_have_no_routine_id(self, api, monkeypatch):
        event = make_event(
            "evt_soccer",
            start=datetime(2026, 9, 20, 20, 0, tzinfo=UTC),
            end=datetime(2026, 9, 20, 21, 0, tzinfo=UTC),
        )
        api.events.put(event)
        rows = agenda(api)
        assert rows[0]["eventId"] == "evt_soccer"
        assert rows[0]["routineId"] is None
        single = api.client.get("/api/events/evt_soccer", headers=ADULT).json()
        assert single["routineId"] is None


# --- estimate ----------------------------------------------------------------


class TestEstimate:
    def test_catalog_hit_makes_no_model_call(self, api):
        r = api.client.post(
            "/api/routines/estimate",
            headers=MINOR,
            json={"name": "Cabin air filter 2023 Kia EV6", "context": "we drive ~10k a year"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "catalog"
        assert body["intervalDays"] == 365
        assert body["rangeDays"] == [270, 365]
        assert body["usageDependent"] is True
        assert body["catalogKey"] == "ev6_cabin_air_filter"
        assert body["category"] == "vehicle"
        assert body["anchor"] == "elapsed"
        assert body["followUpQuestion"] is None
        assert body["confidence"] is None
        assert body["rationale"]
        assert api.estimator.calls == []
        assert api.routines.list(HOUSEHOLD) == []  # writes nothing

    def test_miss_calls_the_estimator(self, api):
        api.estimator.resolution = estimate_mod.Estimate(
            interval_days=90,
            source=IntervalSource.ESTIMATED,
            note="Most makers suggest quarterly.",
            confidence=0.55,
            category="appliances",
            anchor=None,
            range_days=(60, 120),
            usage_dependent=True,
            follow_up_question="How many cups a day?",
        )
        r = api.client.post(
            "/api/routines/estimate",
            headers=ADULT,
            json={"name": "Wax the surfboard"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "intervalDays": 90,
            "rangeDays": [60, 120],
            "confidence": 0.55,
            "source": "estimated",
            "rationale": "Most makers suggest quarterly.",
            "usageDependent": True,
            "followUpQuestion": "How many cups a day?",
            "catalogKey": None,
            "category": "appliances",
            "anchor": None,
        }
        assert api.estimator.calls == [("Wax the surfboard", None)]

    def test_unknown_is_nulls_not_a_guess(self, api):
        api.estimator.resolution = service.UNKNOWN
        r = api.client.post(
            "/api/routines/estimate", headers=ADULT, json={"name": "Frobnicate the zorb"}
        )
        assert r.status_code == 200
        assert r.json()["intervalDays"] is None
        assert r.json()["source"] is None

    def test_validation(self, api):
        r = api.client.post("/api/routines/estimate", headers=ADULT, json={})
        assert r.status_code == 422


# --- get / patch / delete ----------------------------------------------------


class TestSingle:
    def test_get(self, api):
        row = create(api, name="Haircut", intervalDays=35).json()
        r = api.client.get(f"/api/routines/{row['routineId']}", headers=MINOR)
        assert r.status_code == 200
        assert r.json() == row

    def test_unknown_tombstoned_and_invisible_are_all_404(self, api):
        gone = create(api, name="Haircut").json()["routineId"]
        api.client.delete(f"/api/routines/{gone}", headers=ADULT)
        hidden = create(api, name="Therapy", visibility="adults").json()["routineId"]
        for rid, headers in [("rtn_nope", ADULT), (gone, ADULT), (hidden, MINOR)]:
            assert api.client.get(f"/api/routines/{rid}", headers=headers).status_code == 404
            r = api.client.patch(f"/api/routines/{rid}", headers=headers, json={"name": "x"})
            assert r.status_code == 404
            assert api.client.delete(f"/api/routines/{rid}", headers=headers).status_code == 404
            r = api.client.post(f"/api/routines/{rid}/complete", headers=headers, json={})
            assert r.status_code == 404
            r = api.client.get(f"/api/routines/{rid}/completions", headers=headers)
            assert r.status_code == 404
            r = api.client.delete(f"/api/routines/{rid}/completions/cmp_x", headers=headers)
            assert r.status_code == 404
        assert api.client.get(f"/api/routines/{hidden}", headers=ADULT).status_code == 200

    def test_patch_interval_is_human_and_recomputes_due(self, api):
        row = create(api, name="Cabin air filter", lastDoneOn="2026-09-01").json()
        assert row["intervalSource"] == "catalog"
        r = api.client.patch(
            f"/api/routines/{row['routineId']}", headers=ADULT, json={"intervalDays": 30}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["intervalDays"] == 30
        assert body["intervalSource"] == "human"
        assert body["intervalNote"] is None  # the catalog's "why" no longer applies
        assert body["dueOn"] == "2026-10-01"
        assert body["status"] == "ok"
        assert api.events.get(HOUSEHOLD, body["dueEventId"]).start_utc.date() == date(2026, 10, 1)
        # Completing later never overwrites the human interval.
        r = api.client.post(f"/api/routines/{row['routineId']}/complete", headers=ADULT, json={})
        assert r.json()["intervalSource"] == "human"
        assert r.json()["dueOn"] == "2026-10-20"

    def test_patch_due_on_is_a_snooze_cleared_by_completion(self, api):
        row = create(api, name="Haircut", intervalDays=35, lastDoneOn="2026-09-01").json()
        rid = row["routineId"]
        r = api.client.patch(f"/api/routines/{rid}", headers=ADULT, json={"dueOn": "2026-10-15"})
        assert r.json()["dueOn"] == "2026-10-15"
        assert api.events.get(HOUSEHOLD, r.json()["dueEventId"]).start_utc.date() == date(
            2026, 10, 15
        )
        r = api.client.post(f"/api/routines/{rid}/complete", headers=ADULT, json={})
        assert r.json()["dueOn"] == "2026-10-25"

    def test_patch_fields_project_onto_the_due_event(self, api):
        row = create(api, name="Haircut", intervalDays=35).json()
        rid = row["routineId"]
        r = api.client.patch(
            f"/api/routines/{rid}",
            headers=ADULT,
            json={
                "name": "Haircut + beard",
                "category": "grooming",
                "tier": "T1",
                "involves": ["mem_riley"],
                "visibility": "adults",
                "intervalNote": "Barber says monthly.",
                "anchor": "elapsed",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Haircut + beard"
        assert body["category"] == "grooming"
        assert body["memberIds"] == ["mem_alex", "mem_riley"]
        assert body["intervalNote"] == "Barber says monthly."
        assert body["visibility"] == "adults"
        event = api.events.get(HOUSEHOLD, body["dueEventId"])
        assert event.title == "Haircut + beard"
        assert event.involves == ["mem_riley"]
        assert event.visibility is Visibility.ADULTS
        assert event.tier.value == "T1"

    def test_patch_pause_and_resume(self, api):
        row = create(api, name="Haircut", intervalDays=35, lastDoneOn="2026-09-01").json()
        rid = row["routineId"]
        r = api.client.patch(f"/api/routines/{rid}", headers=ADULT, json={"paused": True})
        assert r.json()["status"] == "paused"
        assert r.json()["dueEventId"] is None
        assert api.events.get(HOUSEHOLD, row["dueEventId"]).is_deleted
        r = api.client.patch(f"/api/routines/{rid}", headers=ADULT, json={"paused": False})
        assert r.json()["status"] == "ok"
        assert r.json()["dueOn"] == "2026-10-06"
        assert r.json()["dueEventId"] not in (None, row["dueEventId"])

    def test_patch_clearing_interval_makes_it_unscheduled(self, api):
        row = create(api, name="Haircut", intervalDays=35).json()
        r = api.client.patch(
            f"/api/routines/{row['routineId']}", headers=ADULT, json={"intervalDays": None}
        )
        assert r.json()["status"] == "unscheduled"
        assert r.json()["intervalSource"] is None
        assert r.json()["dueEventId"] is None

    def test_patch_validation(self, api):
        rid = create(api, name="Haircut").json()["routineId"]
        r = api.client.patch(f"/api/routines/{rid}", headers=ADULT, json={"intervalDays": 0})
        assert r.status_code == 422
        r = api.client.patch(f"/api/routines/{rid}", headers=ADULT, json={"ownerMemberId": "x"})
        assert r.status_code == 422  # not a patchable field
        r = api.client.patch(
            f"/api/routines/{rid}", headers=ADULT, json={"involves": ["mem_nobody"]}
        )
        assert r.status_code == 422

    def test_minor_patch_and_delete_rules(self, api):
        theirs = create(api, name="Haircut", intervalDays=35).json()["routineId"]
        mine = create(api, headers=MINOR, name="Hamster cage", intervalDays=7).json()["routineId"]
        r = api.client.patch(f"/api/routines/{theirs}", headers=MINOR, json={"name": "x"})
        assert r.status_code == 403
        assert api.client.delete(f"/api/routines/{theirs}", headers=MINOR).status_code == 403
        r = api.client.patch(f"/api/routines/{mine}", headers=MINOR, json={"intervalDays": 5})
        assert r.status_code == 200
        r = api.client.patch(f"/api/routines/{mine}", headers=MINOR, json={"visibility": "all"})
        assert r.status_code == 403
        assert api.client.delete(f"/api/routines/{mine}", headers=MINOR).status_code == 204

    def test_delete_soft_deletes_routine_and_due_event(self, api):
        row = create(api, name="Haircut", intervalDays=35).json()
        r = api.client.delete(f"/api/routines/{row['routineId']}", headers=ADULT)
        assert r.status_code == 204
        assert api.routines.get(HOUSEHOLD, row["routineId"]).is_deleted
        assert api.events.get(HOUSEHOLD, row["dueEventId"]).is_deleted
        assert agenda(api) == []
        assert (
            api.client.delete(f"/api/routines/{row['routineId']}", headers=ADULT).status_code == 404
        )


# --- complete / completions / undo ------------------------------------------


class TestComplete:
    def test_complete_defaults_to_today_and_moves_the_due_event(self, api):
        row = create(api, name="Haircut", intervalDays=35, lastDoneOn="2026-07-01").json()
        rid = row["routineId"]
        assert row["status"] == "overdue"
        r = api.client.post(
            f"/api/routines/{rid}/complete", headers=ADULT, json={"note": "Short sides"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["lastDoneOn"] == TODAY.isoformat()
        assert body["dueOn"] == "2026-10-25"
        assert body["status"] == "ok"
        assert body["completionCount"] == 2
        assert body["dueEventId"] == row["dueEventId"]  # same event, moved
        assert api.events.get(HOUSEHOLD, row["dueEventId"]).start_utc.date() == date(2026, 10, 25)
        assert agenda(api, start=TODAY.isoformat(), end=TODAY.isoformat()) == []

        history = api.client.get(f"/api/routines/{rid}/completions", headers=ADULT).json()
        assert [c["doneOn"] for c in history["completions"]] == ["2026-07-01", TODAY.isoformat()]
        assert history["completions"][1]["byMemberId"] == "mem_alex"
        assert history["completions"][1]["note"] == "Short sides"
        assert history["completions"][0]["note"] is None
        assert history["completions"][1]["completionId"].startswith("cmp_")

    def test_explicit_done_on(self, api):
        rid = create(api, name="Haircut", intervalDays=35).json()["routineId"]
        r = api.client.post(
            f"/api/routines/{rid}/complete", headers=ADULT, json={"doneOn": "2026-09-10"}
        )
        assert r.json()["lastDoneOn"] == "2026-09-10"
        assert r.json()["dueOn"] == "2026-10-15"

    def test_future_completion_is_400(self, api):
        rid = create(api, name="Haircut", intervalDays=35).json()["routineId"]
        r = api.client.post(
            f"/api/routines/{rid}/complete", headers=ADULT, json={"doneOn": "2026-09-21"}
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "future_completion"
        assert api.routines.list_completions(HOUSEHOLD, rid) == []

    def test_minor_may_complete_anything_visible(self, api):
        rid = create(api, name="Haircut", intervalDays=35).json()["routineId"]
        r = api.client.post(f"/api/routines/{rid}/complete", headers=MINOR, json={})
        assert r.status_code == 200
        history = api.client.get(f"/api/routines/{rid}/completions", headers=MINOR).json()
        assert history["completions"][0]["byMemberId"] == "mem_riley"

    def test_observed_interval_after_three_completions(self, api):
        rid = create(api, name="Wax the surfboard").json()["routineId"]
        for day in ["2026-06-01", "2026-07-01", "2026-08-01"]:
            r = api.client.post(
                f"/api/routines/{rid}/complete", headers=ADULT, json={"doneOn": day}
            )
            assert r.status_code == 200
        body = r.json()
        assert body["intervalSource"] == "observed"
        assert body["intervalDays"] == 30  # median of the 30- and 31-day gaps
        assert body["dueOn"] == "2026-08-31"
        assert body["status"] == "overdue"

    def test_undo_recomputes_state(self, api):
        row = create(api, name="Haircut", intervalDays=35, lastDoneOn="2026-08-01").json()
        rid = row["routineId"]
        done = api.client.post(f"/api/routines/{rid}/complete", headers=ADULT, json={}).json()
        assert done["dueOn"] == "2026-10-25"
        cid = [
            c["completionId"]
            for c in api.client.get(f"/api/routines/{rid}/completions", headers=ADULT).json()[
                "completions"
            ]
            if c["doneOn"] == TODAY.isoformat()
        ][0]
        r = api.client.delete(f"/api/routines/{rid}/completions/{cid}", headers=ADULT)
        assert r.status_code == 204
        after = api.client.get(f"/api/routines/{rid}", headers=ADULT).json()
        assert after["lastDoneOn"] == "2026-08-01"
        assert after["dueOn"] == "2026-09-05"
        assert after["status"] == "overdue"
        assert after["completionCount"] == 1
        # The due event is back on today (overdue roll-forward), not on Oct 25.
        assert api.events.get(HOUSEHOLD, after["dueEventId"]).start_utc.date() == TODAY
        r = api.client.delete(f"/api/routines/{rid}/completions/{cid}", headers=ADULT)
        assert r.status_code == 404

    def test_undo_everything_returns_to_never_done(self, api):
        row = create(api, name="Haircut", intervalDays=35, lastDoneOn="2026-08-01").json()
        rid = row["routineId"]
        cid = api.client.get(f"/api/routines/{rid}/completions", headers=ADULT).json()[
            "completions"
        ][0]["completionId"]
        api.client.delete(f"/api/routines/{rid}/completions/{cid}", headers=ADULT)
        after = api.client.get(f"/api/routines/{rid}", headers=ADULT).json()
        assert after["lastDoneOn"] is None
        assert after["completionCount"] == 0
        assert after["dueOn"] == TODAY.isoformat()

    def test_minor_undo_rules(self, api):
        theirs = create(api, name="Haircut", intervalDays=35, lastDoneOn="2026-09-01").json()
        rid = theirs["routineId"]
        adult_cid = api.client.get(f"/api/routines/{rid}/completions", headers=MINOR).json()[
            "completions"
        ][0]["completionId"]
        r = api.client.delete(f"/api/routines/{rid}/completions/{adult_cid}", headers=MINOR)
        assert r.status_code == 403
        # Their own tap on someone else's routine is theirs to take back.
        api.client.post(f"/api/routines/{rid}/complete", headers=MINOR, json={})
        own_cid = [
            c["completionId"]
            for c in api.client.get(f"/api/routines/{rid}/completions", headers=MINOR).json()[
                "completions"
            ]
            if c["byMemberId"] == "mem_riley"
        ][0]
        r = api.client.delete(f"/api/routines/{rid}/completions/{own_cid}", headers=MINOR)
        assert r.status_code == 204


# --- estimate.py parsing -----------------------------------------------------


class _Block:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class FakeModelClient:
    def __init__(self, *texts: str, raise_exc: Exception | None = None) -> None:
        self.texts = list(texts)
        self.raise_exc = raise_exc
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        blocks = [_Block("thinking")] + [_Block("text", t) for t in self.texts]
        return SimpleNamespace(content=blocks)


GOOD = {
    "interval_days": 90,
    "range_days": [60, 120],
    "confidence": 0.7,
    "rationale": "Most manufacturers say quarterly.",
    "usage_dependent": True,
    "follow_up_question": "Hard water?",
    "anchor": "elapsed",
    "category": "appliances",
}


class TestEstimateParsing:
    def test_fenced_json_with_prose(self):
        import json

        client = FakeModelClient("Sure! ```json\n" + json.dumps(GOOD) + "\n```")
        res = estimate_mod.estimate_interval(
            client, model="m", name="Descale espresso machine", context="daily use"
        )
        assert res.interval_days == 90
        assert res.source is IntervalSource.ESTIMATED
        assert res.note == "Most manufacturers say quarterly."
        assert res.confidence == 0.7
        assert res.range_days == (60, 120)
        assert res.usage_dependent is True
        assert res.follow_up_question == "Hard water?"
        assert res.anchor.value == "elapsed"
        assert res.category == "appliances"
        req = client.requests[0]
        assert req["model"] == "m"
        assert req["max_tokens"] == estimate_mod.MAX_TOKENS
        assert req["output_config"] == {"effort": "low"}
        assert "daily use" in req["messages"][0]["content"]
        assert "Descale espresso machine" in req["messages"][0]["content"]

    def test_garbage_is_unknown(self, caplog):
        client = FakeModelClient("I think every few months or so?")
        with caplog.at_level("WARNING", logger="airhead.routines.estimate"):
            res = estimate_mod.estimate_interval(client, model="m", name="x", context=None)
        assert res == service.UNKNOWN
        assert any(r.msg == "estimate_parse_failed" for r in caplog.records)
        assert "x" not in " ".join(r.getMessage() for r in caplog.records)

    def test_client_error_is_unknown_never_raises(self, caplog):
        client = FakeModelClient(raise_exc=RuntimeError("boom"))
        with caplog.at_level("WARNING", logger="airhead.routines.estimate"):
            res = estimate_mod.estimate_interval(client, model="m", name="x", context=None)
        assert res == service.UNKNOWN
        assert any(r.msg == "estimate_call_failed" for r in caplog.records)

    def test_bad_shapes_are_sanitised(self):
        import json

        text = json.dumps(
            {
                "interval_days": "45",
                "range_days": [100, 50],
                "confidence": 3,
                "rationale": "",
                "anchor": "sometimes",
                "category": "made_up",
                "usage_dependent": "yes",
            }
        )
        res = estimate_mod.parse_estimate(text)
        assert res.interval_days == 45
        assert res.range_days is None
        assert res.confidence == 1.0
        assert res.note is None
        assert res.anchor is None
        assert res.category is None
        assert res.usage_dependent is True
        assert res.follow_up_question is None

    def test_model_declining_keeps_only_the_question(self):
        res = estimate_mod.parse_estimate(
            '{"interval_days": null, "follow_up_question": "Which model?", "confidence": 0.9}'
        )
        assert res.interval_days is None
        assert res.source is None
        assert res.confidence is None
        assert res.follow_up_question == "Which model?"
        assert isinstance(res, service.Resolution)

    def test_absurd_interval_is_unknown(self):
        res = estimate_mod.parse_estimate('{"interval_days": 99999}')
        assert res.interval_days is None
        assert estimate_mod.parse_estimate('{"interval_days": 0}').interval_days is None
        assert estimate_mod.parse_estimate("[1, 2]").interval_days is None
