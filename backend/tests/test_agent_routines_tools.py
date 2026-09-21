"""The routine tools: visibility, the interval ladder, and the confirmation gate.

Exercised the way the SDK's dispatcher does — `tool.call(input_dict)` — so the
schema, the closure over the actor, and the error translation are all in the
path a real turn takes. Same harness shape as `test_agent_tools.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from anthropic.lib.tools import ToolError

from airhead.agent.prompt import DATA_CLOSE, DATA_OPEN
from airhead.agent.tools import (
    Confirmation,
    ToolContext,
    build_tools,
    call_id_for,
    settle_confirmation,
)
from airhead.domain import Anchor, IntervalSource, Routine, Tier, Visibility
from airhead.routines import service
from fakes import (
    ALEX,
    HOUSEHOLD,
    RILEY,
    ROSTER,
    SAM,
    TZ,
    InMemoryEventRepo,
    InMemoryMemberRepo,
    InMemoryRoutineRepo,
)

# 2026-08-04 20:15 UTC is 16:15 in America/New_York — still the 4th.
NOW = datetime(2026, 8, 4, 20, 15, tzinfo=UTC)
TODAY = date(2026, 8, 4)

SECRET = "Marriage counsellor follow-up"


def make_routine(
    routine_id: str,
    *,
    name: str = "Haircut",
    owner: str = "mem_alex",
    interval_days: int | None = 35,
    source: IntervalSource = IntervalSource.CATALOG,
    last_done_on: date | None = date(2026, 7, 1),
    due_on: date | None = date(2026, 8, 5),
    visibility: Visibility = Visibility.ALL,
    created_by: str | None = None,
    paused: bool = False,
) -> Routine:
    return Routine(
        routine_id=routine_id,
        household_id=HOUSEHOLD,
        name=name,
        owner_member_id=owner,
        interval_days=interval_days,
        interval_source=source,
        last_done_on=last_done_on,
        due_on=due_on,
        visibility=visibility,
        created_by=created_by or owner,
        paused=paused,
    )


def adults_only() -> Routine:
    return make_routine("rtn_secret", name=SECRET, visibility=Visibility.ADULTS)


def harness(
    actor: Any = ALEX,
    *,
    routines: list[Routine] | None = None,
    confirm: Confirmation | None = None,
) -> tuple[dict[str, Any], ToolContext, InMemoryRoutineRepo, InMemoryEventRepo]:
    routine_repo = InMemoryRoutineRepo(routines or [])
    event_repo = InMemoryEventRepo([])
    # Seed the due events the way a real store would already hold them.
    service.reproject_all(routine_repo, event_repo, household_id=HOUSEHOLD, today=TODAY, tz=TZ)
    ctx = ToolContext(
        household_id=HOUSEHOLD,
        actor=actor,
        events=event_repo,
        members=InMemoryMemberRepo(ROSTER),
        routines=routine_repo,
        now=NOW,
        tz=TZ,
        confirm=confirm,
    )
    return {t.name: t for t in build_tools(ctx)}, ctx, routine_repo, event_repo


def rows(out: str) -> list[dict[str, Any]]:
    body = out.split(DATA_OPEN, 1)[1].split(DATA_CLOSE, 1)[0]
    return json.loads(body)["routines"]


def approve(pending: Any) -> Confirmation:
    """What the route builds from the stored pending call: id, verdict, tool, args."""
    return Confirmation(
        call_id=pending.call_id, approved=True, tool=pending.tool, args=dict(pending.args)
    )


def decline(pending: Any) -> Confirmation:
    return Confirmation(
        call_id=pending.call_id, approved=False, tool=pending.tool, args=dict(pending.args)
    )


# --- list_routines -----------------------------------------------------------


def test_list_routines_is_fenced_and_shaped_like_the_api() -> None:
    tools, _, _, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    out = tools["list_routines"].call({})

    assert out.startswith(DATA_OPEN)
    (row,) = rows(out)
    assert row["routineId"] == "rtn_hair"
    assert row["name"] == "Haircut"
    assert row["memberIds"] == ["mem_alex"]
    assert row["intervalDays"] == 35
    assert row["intervalSource"] == "catalog"
    assert row["lastDoneOn"] == "2026-07-01"
    assert row["dueOn"] == "2026-08-05"
    assert row["status"] == "due_soon"
    assert row["daysUntilDue"] == 1
    assert row["completionCount"] == 0
    assert row["paused"] is False
    assert row["dueEventId"].startswith("evt_")


def test_list_routines_orders_overdue_first_and_reprojects() -> None:
    late = make_routine("rtn_gutters", name="Gutters", due_on=date(2026, 7, 20))
    fine = make_routine("rtn_hair", due_on=date(2026, 9, 1))
    tools, _, _, events = harness(ALEX, routines=[fine, late])
    out = tools["list_routines"].call({})

    assert [r["routineId"] for r in rows(out)] == ["rtn_gutters", "rtn_hair"]
    assert rows(out)[0]["status"] == "overdue"
    assert rows(out)[0]["daysUntilDue"] == -15
    # Ground rule 3: the overdue due-event sits on today, not in the past.
    due = events.get(HOUSEHOLD, rows(out)[0]["dueEventId"])
    assert due.start_utc.date() == TODAY


def test_list_routines_reads_nothing_into_the_audit_log() -> None:
    tools, ctx, _, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["list_routines"].call({})
    assert ctx.outcomes == []


# --- visibility: minor never sees an adults-only routine ---------------------


def test_minor_list_excludes_adults_only_routines() -> None:
    tools, _, _, _ = harness(RILEY, routines=[make_routine("rtn_hair"), adults_only()])
    out = tools["list_routines"].call({})
    assert [r["routineId"] for r in rows(out)] == ["rtn_hair"]
    assert SECRET not in out


def test_adult_list_includes_adults_only_routines() -> None:
    tools, _, _, _ = harness(ALEX, routines=[make_routine("rtn_hair"), adults_only()])
    assert SECRET in tools["list_routines"].call({})


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("log_done", {}),
        ("update_routine", {"due_on": "2026-09-01"}),
        ("delete_routine", {}),
        ("undo_completion", {"completion_id": "cmp_x"}),
    ],
)
def test_minor_gets_not_found_for_an_adults_only_routine(tool: str, args: dict) -> None:
    """404, not 403 — the difference is itself a disclosure."""
    tools, ctx, repo, _ = harness(RILEY, routines=[adults_only()])
    with pytest.raises(ToolError) as caught:
        tools[tool].call({"routine_id": "rtn_secret", **args})
    assert "No such routine" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert repo.get(HOUSEHOLD, "rtn_secret").is_deleted is False
    assert repo.list_completions(HOUSEHOLD, "rtn_secret") == []
    assert [(o.tool, o.status, o.detail) for o in ctx.outcomes] == [(tool, "error", "not_found")]
    assert ctx.pending is None


# --- log_done ----------------------------------------------------------------


def test_log_done_records_a_completion_and_moves_the_due_date() -> None:
    tools, ctx, repo, events = harness(SAM, routines=[make_routine("rtn_hair")])
    out = tools["log_done"].call({"routine_id": "rtn_hair", "note": "Short on the sides"})

    (done,) = repo.list_completions(HOUSEHOLD, "rtn_hair")
    assert done.done_on == TODAY
    assert done.by_member_id == "mem_sam"  # any member who can see it may complete it
    assert done.note == "Short on the sides"
    assert done.completion_id in out

    stored = repo.get(HOUSEHOLD, "rtn_hair")
    assert stored.last_done_on == TODAY
    assert stored.due_on == date(2026, 9, 8)
    assert events.get(HOUSEHOLD, stored.due_event_id).start_utc.date() == date(2026, 9, 8)
    assert [(o.tool, o.status, o.event_id, o.detail) for o in ctx.outcomes] == [
        ("log_done", "ok", stored.due_event_id, "rtn_hair")
    ]


def test_log_done_accepts_an_earlier_day() -> None:
    tools, _, repo, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["log_done"].call({"routine_id": "rtn_hair", "done_on": "2026-08-01"})
    assert repo.get(HOUSEHOLD, "rtn_hair").last_done_on == date(2026, 8, 1)


def test_log_done_in_the_future_is_refused() -> None:
    tools, ctx, repo, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    with pytest.raises(ToolError):
        tools["log_done"].call({"routine_id": "rtn_hair", "done_on": "2026-08-05"})
    assert repo.list_completions(HOUSEHOLD, "rtn_hair") == []
    assert repo.get(HOUSEHOLD, "rtn_hair").last_done_on == date(2026, 7, 1)
    assert [(o.status, o.detail) for o in ctx.outcomes] == [("error", "future_completion")]


def test_log_done_on_an_unknown_routine_is_a_tool_error() -> None:
    tools, ctx, _, _ = harness(ALEX)
    with pytest.raises(ToolError) as caught:
        tools["log_done"].call({"routine_id": "rtn_nope"})
    assert "No such routine" in str(caught.value)
    assert [(o.status, o.detail) for o in ctx.outcomes] == [("error", "not_found")]


def test_log_done_with_a_bad_date_is_a_tool_error() -> None:
    tools, _, repo, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    with pytest.raises(ToolError):
        tools["log_done"].call({"routine_id": "rtn_hair", "done_on": "yesterday"})
    assert repo.list_completions(HOUSEHOLD, "rtn_hair") == []


# --- create_routine: the interval ladder -------------------------------------


def created(ctx: ToolContext, repo: InMemoryRoutineRepo) -> Routine:
    (outcome,) = ctx.outcomes
    assert outcome.tool == "create_routine" and outcome.status == "ok"
    stored = repo.get(HOUSEHOLD, outcome.detail)
    assert stored is not None
    assert outcome.event_id == stored.due_event_id
    return stored


def test_catalog_beats_the_models_estimate() -> None:
    tools, ctx, repo, _ = harness(ALEX)
    out = tools["create_routine"].call(
        {
            "name": "Haircut",
            "interval_days": 60,
            "interval_stated_by_person": False,
            "interval_note": "Guessing every two months.",
            "interval_confidence": 0.4,
        }
    )
    stored = created(ctx, repo)
    assert stored.interval_days == 35
    assert stored.interval_source is IntervalSource.CATALOG
    assert stored.catalog_key == "haircut_mens"
    assert stored.category == "personal_care"
    assert stored.interval_confidence is None
    assert "catalog" in out


def test_the_persons_number_beats_the_catalog() -> None:
    tools, ctx, repo, _ = harness(ALEX)
    tools["create_routine"].call(
        {"name": "Haircut", "interval_days": 60, "interval_stated_by_person": True}
    )
    stored = created(ctx, repo)
    assert stored.interval_days == 60
    assert stored.interval_source is IntervalSource.HUMAN
    assert stored.catalog_key == "haircut_mens"  # the match still tags it


def test_the_estimate_is_used_only_when_the_catalog_misses() -> None:
    tools, ctx, repo, _ = harness(ALEX)
    out = tools["create_routine"].call(
        {
            "name": "Re-seal the deck",
            "interval_days": 730,
            "interval_stated_by_person": False,
            "interval_note": "Deck stain is usually redone every 2-3 years.",
            "interval_confidence": 0.6,
            "last_done_on": "2026-08-04",
        }
    )
    stored = created(ctx, repo)
    assert stored.interval_days == 730
    assert stored.interval_source is IntervalSource.ESTIMATED
    assert stored.interval_note == "Deck stain is usually redone every 2-3 years."
    assert stored.interval_confidence == 0.6
    assert stored.catalog_key is None
    assert stored.due_on == date(2028, 8, 3)
    # The model is told, in the tool result, to say it is a guess.
    assert "estimate" in out


def test_no_interval_and_no_catalog_match_is_unscheduled() -> None:
    tools, ctx, repo, _ = harness(ALEX)
    out = tools["create_routine"].call({"name": "Re-seal the deck"})
    stored = created(ctx, repo)
    assert stored.interval_days is None
    assert stored.due_on is None
    assert stored.due_event_id is None
    assert service.status(stored, today=TODAY) == "unscheduled"
    assert "unscheduled" in out


def test_create_with_last_done_on_seeds_a_completion_and_a_due_event() -> None:
    tools, ctx, repo, events = harness(RILEY)
    tools["create_routine"].call({"name": "Haircut", "last_done_on": "2026-08-04"})
    stored = created(ctx, repo)
    assert stored.owner_member_id == "mem_riley"
    assert stored.created_by == "mem_riley"
    assert stored.last_done_on == TODAY
    assert stored.due_on == date(2026, 9, 8)
    (done,) = repo.list_completions(HOUSEHOLD, stored.routine_id)
    assert done.by_member_id == "mem_riley"
    due = events.get(HOUSEHOLD, stored.due_event_id)
    assert due.routine_id == stored.routine_id
    assert due.all_day is True
    assert due.owner_member_id == "mem_riley"


def test_create_with_a_calendar_anchor_and_explicit_due_date() -> None:
    tools, ctx, repo, _ = harness(ALEX)
    tools["create_routine"].call(
        {
            "name": "Holiday lights up",
            "interval_days": 365,
            "interval_stated_by_person": True,
            "anchor": "calendar",
            "last_done_on": "2025-11-28",
            "tier": "T1",
            "involves": ["mem_sam"],
        }
    )
    stored = created(ctx, repo)
    assert stored.anchor is Anchor.CALENDAR
    assert stored.due_on == date(2026, 11, 28)
    assert stored.tier is Tier.HOUSEHOLD
    assert stored.involves == ["mem_sam"]


def test_create_with_a_future_last_done_on_writes_nothing() -> None:
    tools, ctx, repo, _ = harness(ALEX)
    with pytest.raises(ToolError):
        tools["create_routine"].call({"name": "Haircut", "last_done_on": "2026-08-05"})
    assert repo.list(HOUSEHOLD) == []
    assert [(o.status, o.detail) for o in ctx.outcomes] == [("error", "future_completion")]


def test_minor_cannot_create_a_routine_for_someone_else() -> None:
    tools, ctx, repo, _ = harness(RILEY)
    with pytest.raises(ToolError):
        tools["create_routine"].call({"name": "Haircut", "owner_member_id": "mem_alex"})
    assert repo.list(HOUSEHOLD) == []
    assert [(o.status, o.detail) for o in ctx.outcomes] == [("error", "forbidden")]


def test_minor_cannot_create_an_adults_only_routine() -> None:
    tools, _, repo, _ = harness(RILEY)
    with pytest.raises(ToolError):
        tools["create_routine"].call({"name": "Haircut", "visibility": "adults"})
    assert repo.list(HOUSEHOLD) == []


def test_adult_can_create_for_another_member() -> None:
    tools, ctx, repo, _ = harness(ALEX)
    tools["create_routine"].call({"name": "Haircut", "owner_member_id": "mem_riley"})
    assert created(ctx, repo).owner_member_id == "mem_riley"


def test_create_with_an_unknown_owner_is_refused() -> None:
    tools, _, repo, _ = harness(ALEX)
    with pytest.raises(ToolError):
        tools["create_routine"].call({"name": "Haircut", "owner_member_id": "mem_ghost"})
    assert repo.list(HOUSEHOLD) == []


# --- update_routine ----------------------------------------------------------


def test_owner_snoozes_without_a_gate() -> None:
    tools, ctx, repo, events = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["update_routine"].call({"routine_id": "rtn_hair", "due_on": "2026-09-01"})

    stored = repo.get(HOUSEHOLD, "rtn_hair")
    assert stored.due_on == date(2026, 9, 1)
    assert events.get(HOUSEHOLD, stored.due_event_id).start_utc.date() == date(2026, 9, 1)
    assert ctx.pending is None
    assert [(o.tool, o.status, o.event_id, o.detail) for o in ctx.outcomes] == [
        ("update_routine", "ok", stored.due_event_id, "rtn_hair")
    ]


def test_interval_through_update_is_human_and_recomputes_due() -> None:
    tools, _, repo, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["update_routine"].call({"routine_id": "rtn_hair", "interval_days": 28})

    stored = repo.get(HOUSEHOLD, "rtn_hair")
    assert stored.interval_days == 28
    assert stored.interval_source is IntervalSource.HUMAN
    assert stored.due_on == date(2026, 7, 29)  # last done + 28, not the old snooze


def test_explicit_due_on_wins_over_the_recompute() -> None:
    tools, _, repo, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["update_routine"].call(
        {"routine_id": "rtn_hair", "interval_days": 28, "due_on": "2026-10-01"}
    )
    assert repo.get(HOUSEHOLD, "rtn_hair").due_on == date(2026, 10, 1)


def test_a_human_interval_survives_the_next_completion() -> None:
    tools, _, repo, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["update_routine"].call({"routine_id": "rtn_hair", "interval_days": 28})
    tools["log_done"].call({"routine_id": "rtn_hair"})
    stored = repo.get(HOUSEHOLD, "rtn_hair")
    assert stored.interval_days == 28
    assert stored.interval_source is IntervalSource.HUMAN
    assert stored.due_on == date(2026, 9, 1)


def test_pausing_removes_the_due_event_and_resuming_restores_it() -> None:
    tools, _, repo, events = harness(ALEX, routines=[make_routine("rtn_hair")])
    first_event = repo.get(HOUSEHOLD, "rtn_hair").due_event_id

    tools["update_routine"].call({"routine_id": "rtn_hair", "paused": True})
    assert repo.get(HOUSEHOLD, "rtn_hair").due_event_id is None
    assert events.get(HOUSEHOLD, first_event).is_deleted is True

    tools["update_routine"].call({"routine_id": "rtn_hair", "paused": False})
    stored = repo.get(HOUSEHOLD, "rtn_hair")
    assert stored.due_event_id is not None
    assert events.get(HOUSEHOLD, stored.due_event_id).is_deleted is False


def test_update_with_nothing_to_change_is_refused() -> None:
    tools, _, _, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    with pytest.raises(ToolError):
        tools["update_routine"].call({"routine_id": "rtn_hair"})


def test_minor_cannot_edit_another_members_routine() -> None:
    tools, ctx, repo, _ = harness(RILEY, routines=[make_routine("rtn_hair")])
    with pytest.raises(ToolError):
        tools["update_routine"].call({"routine_id": "rtn_hair", "name": "Nope"})
    assert repo.get(HOUSEHOLD, "rtn_hair").name == "Haircut"
    assert ctx.pending is None
    assert [(o.status, o.detail) for o in ctx.outcomes] == [("error", "forbidden")]


def test_minor_edits_their_own_routine_without_a_gate() -> None:
    tools, ctx, repo, _ = harness(RILEY, routines=[make_routine("rtn_hair", owner="mem_riley")])
    tools["update_routine"].call({"routine_id": "rtn_hair", "name": "Trim"})
    assert repo.get(HOUSEHOLD, "rtn_hair").name == "Trim"
    assert ctx.pending is None


def test_editing_another_members_routine_is_gated() -> None:
    tools, ctx, repo, _ = harness(SAM, routines=[make_routine("rtn_hair")])
    out = tools["update_routine"].call({"routine_id": "rtn_hair", "due_on": "2026-09-01"})

    assert "NOT DONE" in out
    assert repo.get(HOUSEHOLD, "rtn_hair").due_on == date(2026, 8, 5)
    assert ctx.pending is not None
    assert ctx.pending.tool == "update_routine"
    assert ctx.pending.args == {"routine_id": "rtn_hair", "due_on": "2026-09-01"}
    assert ctx.pending.event_id == repo.get(HOUSEHOLD, "rtn_hair").due_event_id
    assert "Haircut" in ctx.pending.summary
    assert [o.status for o in ctx.outcomes] == ["pending_confirmation"]


def test_an_approved_update_is_replayed_by_the_harness() -> None:
    tools, ctx, _, _ = harness(SAM, routines=[make_routine("rtn_hair")])
    tools["update_routine"].call({"routine_id": "rtn_hair", "due_on": "2026-09-01"})

    tools2, ctx2, repo2, _ = harness(
        SAM, routines=[make_routine("rtn_hair")], confirm=approve(ctx.pending)
    )
    settled = settle_confirmation(ctx2)

    assert settled is not None and settled.status == "ok"
    assert repo2.get(HOUSEHOLD, "rtn_hair").due_on == date(2026, 9, 1)
    assert ctx2.pending is None
    # A model re-issue of the same call is "already handled", not a second write.
    out = tools2["update_routine"].call({"routine_id": "rtn_hair", "due_on": "2026-09-01"})
    assert "Already handled" in out
    assert [(o.tool, o.status) for o in ctx2.outcomes] == [("update_routine", "ok")]


def test_an_approval_does_not_carry_over_to_a_different_routine_patch() -> None:
    approved = Confirmation(
        call_id=call_id_for("update_routine", "rtn_hair", '{"due_on": "2026-09-01"}'),
        approved=True,
    )
    tools, ctx, repo, _ = harness(SAM, routines=[make_routine("rtn_hair")], confirm=approved)
    tools["update_routine"].call({"routine_id": "rtn_hair", "name": "Something else"})
    assert repo.get(HOUSEHOLD, "rtn_hair").name == "Haircut"
    assert ctx.pending is not None


def test_a_declined_update_is_recorded_and_not_applied() -> None:
    tools, ctx, _, _ = harness(SAM, routines=[make_routine("rtn_hair")])
    tools["update_routine"].call({"routine_id": "rtn_hair", "due_on": "2026-09-01"})

    tools2, ctx2, repo2, _ = harness(
        SAM, routines=[make_routine("rtn_hair")], confirm=decline(ctx.pending)
    )
    settled = settle_confirmation(ctx2)
    assert (settled.status, settled.detail) == ("error", "declined")
    assert repo2.get(HOUSEHOLD, "rtn_hair").due_on == date(2026, 8, 5)

    out = tools2["update_routine"].call({"routine_id": "rtn_hair", "due_on": "2026-09-01"})
    assert "declined" in out
    assert [(o.tool, o.status) for o in ctx2.outcomes] == [("update_routine", "error")]


# --- delete_routine ----------------------------------------------------------


def test_delete_returns_pending_and_writes_nothing() -> None:
    tools, ctx, repo, events = harness(ALEX, routines=[make_routine("rtn_hair")])
    out = tools["delete_routine"].call({"routine_id": "rtn_hair"})

    assert "NOT DONE" in out
    stored = repo.get(HOUSEHOLD, "rtn_hair")
    assert stored.is_deleted is False
    assert events.get(HOUSEHOLD, stored.due_event_id).is_deleted is False
    assert ctx.pending is not None
    assert ctx.pending.tool == "delete_routine"
    assert ctx.pending.args == {"routine_id": "rtn_hair"}
    assert ctx.pending.event_id == stored.due_event_id
    assert "Haircut" in ctx.pending.summary
    assert [o.status for o in ctx.outcomes] == ["pending_confirmation"]


def test_an_approved_delete_is_replayed_and_removes_the_due_event() -> None:
    tools, ctx, _, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["delete_routine"].call({"routine_id": "rtn_hair"})

    tools2, ctx2, repo2, events2 = harness(
        ALEX, routines=[make_routine("rtn_hair")], confirm=approve(ctx.pending)
    )
    due_event = repo2.get(HOUSEHOLD, "rtn_hair").due_event_id
    settled = settle_confirmation(ctx2)

    assert settled is not None and settled.status == "ok"
    assert repo2.get(HOUSEHOLD, "rtn_hair").is_deleted is True
    assert events2.get(HOUSEHOLD, due_event).is_deleted is True
    assert [(o.tool, o.status, o.event_id, o.detail) for o in ctx2.outcomes] == [
        ("delete_routine", "ok", due_event, "rtn_hair")
    ]

    out = tools2["delete_routine"].call({"routine_id": "rtn_hair"})
    assert "Already handled" in out
    assert len(ctx2.outcomes) == 1


def test_a_declined_delete_performs_no_write() -> None:
    refusal = Confirmation(call_id=call_id_for("delete_routine", "rtn_hair"), approved=False)
    tools, ctx, repo, _ = harness(ALEX, routines=[make_routine("rtn_hair")], confirm=refusal)
    out = tools["delete_routine"].call({"routine_id": "rtn_hair"})

    assert "NOT DONE" in out
    assert repo.get(HOUSEHOLD, "rtn_hair").is_deleted is False
    assert ctx.pending is None
    assert [o.status for o in ctx.outcomes] == ["error"]


def test_minor_cannot_delete_a_routine_they_did_not_create() -> None:
    tools, ctx, repo, _ = harness(RILEY, routines=[make_routine("rtn_hair")])
    with pytest.raises(ToolError):
        tools["delete_routine"].call({"routine_id": "rtn_hair"})
    assert repo.get(HOUSEHOLD, "rtn_hair").is_deleted is False
    assert ctx.pending is None


def test_a_deleted_routine_is_gone_for_everyone() -> None:
    tools, _, _, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["delete_routine"].call({"routine_id": "rtn_hair"})


# --- undo_completion ---------------------------------------------------------


def test_undo_rolls_the_due_date_back() -> None:
    tools, ctx, repo, events = harness(ALEX, routines=[make_routine("rtn_hair")])
    tools["log_done"].call({"routine_id": "rtn_hair"})
    (done,) = repo.list_completions(HOUSEHOLD, "rtn_hair")
    assert repo.get(HOUSEHOLD, "rtn_hair").last_done_on == TODAY

    tools["undo_completion"].call({"routine_id": "rtn_hair", "completion_id": done.completion_id})

    stored = repo.get(HOUSEHOLD, "rtn_hair")
    assert repo.list_completions(HOUSEHOLD, "rtn_hair") == []
    assert stored.last_done_on is None
    assert stored.due_on == TODAY  # an interval with no history is due now
    assert events.get(HOUSEHOLD, stored.due_event_id).start_utc.date() == TODAY
    assert [(o.tool, o.status, o.detail) for o in ctx.outcomes] == [
        ("log_done", "ok", "rtn_hair"),
        ("undo_completion", "ok", "rtn_hair"),
    ]


def test_undo_of_an_unknown_completion_is_a_tool_error() -> None:
    tools, ctx, _, _ = harness(ALEX, routines=[make_routine("rtn_hair")])
    with pytest.raises(ToolError) as caught:
        tools["undo_completion"].call({"routine_id": "rtn_hair", "completion_id": "cmp_nope"})
    assert "No such completion" in str(caught.value)
    assert [(o.status, o.detail) for o in ctx.outcomes] == [("error", "not_found")]


# --- schema ------------------------------------------------------------------


def test_routine_tools_are_present_and_expose_no_actor() -> None:
    tools, _, _, _ = harness(ALEX)
    names = {
        "list_routines",
        "log_done",
        "create_routine",
        "update_routine",
        "delete_routine",
        "undo_completion",
    }
    assert names <= tools.keys()
    for name in names:
        schema = tools[name].to_dict()["input_schema"]
        assert "actor_member_id" not in schema["properties"]
        assert "household_id" not in schema["properties"]
