"""The tool layer: visibility, authorization, and the confirmation gate.

These are exercised by calling the tools the way the SDK's dispatcher does —
`tool.call(input_dict)` — so the schema, the closure over the actor, and the
error translation are all in the path a real turn takes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from anthropic.lib.tools import ToolError

from airhead.agent.tools import (
    Confirmation,
    ToolContext,
    build_tools,
    call_id_for,
)
from airhead.domain import Event, EventStatus, Tier, TierSource, Visibility
from fakes import (
    ALEX,
    HOUSEHOLD,
    RILEY,
    ROSTER,
    SAM,
    TZ,
    InMemoryEventRepo,
    InMemoryMemberRepo,
    make_event,
)

NOW = datetime(2026, 8, 4, 20, 15, tzinfo=UTC)
# 2026-08-06 is a Thursday. 16:00 America/New_York == 20:00 UTC.
THU_4PM = datetime(2026, 8, 6, 20, 0, tzinfo=UTC)
THU_5PM = datetime(2026, 8, 6, 21, 0, tzinfo=UTC)

SECRET = "Divorce lawyer consultation"


def soccer(**kw: Any) -> Event:
    kw.setdefault("owner", "mem_riley")
    return make_event("evt_soccer", start=THU_4PM, end=THU_5PM, **kw)


def adults_only() -> Event:
    return make_event(
        "evt_secret",
        title=SECRET,
        start=THU_4PM,
        end=THU_5PM,
        owner="mem_alex",
        visibility=Visibility.ADULTS,
    )


def harness(
    actor: Any = ALEX,
    *,
    events: list[Event] | None = None,
    confirm: Confirmation | None = None,
) -> tuple[dict[str, Any], ToolContext, InMemoryEventRepo]:
    repo = InMemoryEventRepo(events or [])
    ctx = ToolContext(
        household_id=HOUSEHOLD,
        actor=actor,
        events=repo,
        members=InMemoryMemberRepo(ROSTER),
        now=NOW,
        tz=TZ,
        confirm=confirm,
    )
    return {t.name: t for t in build_tools(ctx)}, ctx, repo


# --- S5: a minor never receives an adults-only event -------------------------


def test_minor_agenda_excludes_adults_only_events() -> None:
    tools, _, _ = harness(RILEY, events=[soccer(), adults_only()])
    out = tools["get_agenda"].call({"start": "2026-08-06", "end": "2026-08-06"})
    assert "Soccer practice" in out
    assert SECRET not in out


def test_adult_agenda_includes_adults_only_events() -> None:
    tools, _, _ = harness(ALEX, events=[soccer(), adults_only()])
    out = tools["get_agenda"].call({"start": "2026-08-06", "end": "2026-08-06"})
    assert SECRET in out


def test_minor_cannot_reach_an_adults_only_event_by_asking_for_its_owner() -> None:
    """Narrowing to the owning adult must not widen the scope."""
    tools, _, _ = harness(RILEY, events=[adults_only()])
    out = tools["get_agenda"].call(
        {"start": "2026-08-06", "end": "2026-08-06", "member": "mem_alex"}
    )
    assert SECRET not in out


def test_minor_conflicts_exclude_adults_only_events() -> None:
    tools, _, _ = harness(RILEY, events=[soccer(involves=["mem_alex"]), adults_only()])
    out = tools["find_conflicts"].call({"start": "2026-08-06", "end": "2026-08-06"})
    assert SECRET not in out


def test_adult_conflicts_find_the_double_booking() -> None:
    tools, _, _ = harness(ALEX, events=[soccer(involves=["mem_alex"]), adults_only()])
    out = tools["find_conflicts"].call({"start": "2026-08-06", "end": "2026-08-06"})
    assert "evt_soccer" in out
    assert "evt_secret" in out


def test_minor_gets_not_found_for_an_adults_only_event_id() -> None:
    """404, not 403 — the difference is itself a disclosure."""
    tools, _, repo = harness(RILEY, events=[adults_only()])
    with pytest.raises(ToolError) as caught:
        tools["set_tier"].call({"event_id": "evt_secret", "tier": "T1"})
    assert SECRET not in str(caught.value)
    assert repo.get(HOUSEHOLD, "evt_secret").tier is Tier.HOUSEHOLD


# --- authorization is in the tool, not the prompt ----------------------------


def test_minor_cannot_set_visibility() -> None:
    tools, ctx, repo = harness(RILEY, events=[soccer()])
    with pytest.raises(ToolError):
        tools["set_visibility"].call({"event_id": "evt_soccer", "visibility": "adults"})
    assert repo.get(HOUSEHOLD, "evt_soccer").visibility is Visibility.ALL
    assert [o.status for o in ctx.outcomes] == ["error"]


def test_minor_cannot_edit_another_members_event() -> None:
    event = make_event("evt_gym", title="Gym", start=THU_4PM, end=THU_5PM, owner="mem_sam")
    tools, ctx, repo = harness(RILEY, events=[event])
    with pytest.raises(ToolError):
        tools["update_event"].call({"event_id": "evt_gym", "title": "Nope"})
    assert repo.get(HOUSEHOLD, "evt_gym").title == "Gym"
    assert ctx.pending is None


def test_minor_creating_for_someone_else_lands_a_proposal() -> None:
    # Issue #4: allowed, but stored `proposed` — an adult confirms via the REST
    # confirm route, not the turn gate, which the minor could answer themselves.
    tools, ctx, repo = harness(RILEY)
    result = tools["create_event"].call(
        {"title": "Pickup", "start": "2026-08-06T18:00", "owner_member_id": "mem_alex"}
    )
    assert "adult must confirm" in result
    assert [o.status for o in ctx.outcomes] == ["ok"]
    stored = repo.get(HOUSEHOLD, ctx.outcomes[0].event_id)
    assert stored.status is EventStatus.PROPOSED
    assert stored.owner_member_id == "mem_alex"
    assert stored.created_by == "mem_riley"
    assert ctx.pending is None  # not the conversational gate


def test_minor_creating_their_own_event_is_confirmed_at_birth() -> None:
    tools, ctx, repo = harness(RILEY)
    tools["create_event"].call({"title": "Practice", "start": "2026-08-06T18:00"})
    assert repo.get(HOUSEHOLD, ctx.outcomes[0].event_id).status is EventStatus.CONFIRMED


def test_minor_may_edit_their_own_event_without_a_gate() -> None:
    tools, ctx, repo = harness(RILEY, events=[soccer()])
    tools["update_event"].call({"event_id": "evt_soccer", "location": "Field 3"})
    assert repo.get(HOUSEHOLD, "evt_soccer").location == "Field 3"
    assert ctx.pending is None


# --- the confirmation gate ---------------------------------------------------


def test_delete_returns_pending_and_writes_nothing() -> None:
    tools, ctx, repo = harness(ALEX, events=[soccer()])
    out = tools["delete_event"].call({"event_id": "evt_soccer"})

    assert "NOT DONE" in out
    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False
    assert ctx.pending is not None
    assert ctx.pending.tool == "delete_event"
    assert ctx.pending.event_id == "evt_soccer"
    assert "Soccer practice" in ctx.pending.summary
    assert [o.status for o in ctx.outcomes] == ["pending_confirmation"]


def test_delete_happens_only_after_an_approving_confirmation() -> None:
    approval = Confirmation(call_id=call_id_for("delete_event", "evt_soccer"), approved=True)
    tools, ctx, repo = harness(ALEX, events=[soccer()], confirm=approval)
    tools["delete_event"].call({"event_id": "evt_soccer"})

    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is True
    assert ctx.pending is None
    assert [(o.tool, o.status) for o in ctx.outcomes] == [("delete_event", "ok")]


def test_a_declining_confirmation_performs_no_write() -> None:
    refusal = Confirmation(call_id=call_id_for("delete_event", "evt_soccer"), approved=False)
    tools, ctx, repo = harness(ALEX, events=[soccer()], confirm=refusal)
    out = tools["delete_event"].call({"event_id": "evt_soccer"})

    assert "NOT DONE" in out
    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False
    assert ctx.pending is None
    assert [o.status for o in ctx.outcomes] == ["error"]


def test_a_confirmation_for_another_call_does_not_authorize_this_one() -> None:
    other = Confirmation(call_id=call_id_for("delete_event", "evt_other"), approved=True)
    tools, ctx, repo = harness(ALEX, events=[soccer()], confirm=other)
    tools["delete_event"].call({"event_id": "evt_soccer"})

    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False
    assert ctx.pending is not None
    assert ctx.pending.call_id != other.call_id


def test_editing_another_members_event_is_gated_for_an_adult_too() -> None:
    tools, ctx, repo = harness(SAM, events=[soccer()])
    tools["update_event"].call({"event_id": "evt_soccer", "start": "2026-08-06T17:00"})

    assert repo.get(HOUSEHOLD, "evt_soccer").start_utc == THU_4PM
    assert ctx.pending is not None
    assert ctx.pending.tool == "update_event"

    approved = Confirmation(call_id=ctx.pending.call_id, approved=True)
    tools2, ctx2, repo2 = harness(SAM, events=[soccer()], confirm=approved)
    tools2["update_event"].call({"event_id": "evt_soccer", "start": "2026-08-06T17:00"})
    assert repo2.get(HOUSEHOLD, "evt_soccer").start_utc == datetime(2026, 8, 6, 21, 0, tzinfo=UTC)
    assert ctx2.pending is None


def test_an_approval_does_not_carry_over_to_a_different_patch() -> None:
    """The patch is part of the call id, so 'yes' means yes to that change."""
    approved_move = Confirmation(
        call_id=call_id_for("update_event", "evt_soccer", '{"start": "2026-08-06T17:00"}'),
        approved=True,
    )
    tools, ctx, repo = harness(SAM, events=[soccer()], confirm=approved_move)
    tools["update_event"].call({"event_id": "evt_soccer", "title": "Something else"})

    assert repo.get(HOUSEHOLD, "evt_soccer").title == "Soccer practice"
    assert ctx.pending is not None


def test_owner_edits_their_own_event_without_a_gate() -> None:
    tools, ctx, repo = harness(ALEX, events=[soccer(owner="mem_alex")])
    tools["update_event"].call({"event_id": "evt_soccer", "title": "Soccer game"})
    assert repo.get(HOUSEHOLD, "evt_soccer").title == "Soccer game"
    assert ctx.pending is None


# --- writes ------------------------------------------------------------------


def test_create_event_defaults_the_owner_to_the_actor() -> None:
    tools, ctx, repo = harness(RILEY)
    tools["create_event"].call(
        {
            "title": "Soccer practice",
            "start": "2026-08-06T16:00",
            "tier": "T1",
            "involves": ["mem_sam"],
        }
    )
    (outcome,) = ctx.outcomes
    stored = repo.get(HOUSEHOLD, outcome.event_id)
    assert stored.owner_member_id == "mem_riley"
    assert stored.involves == ["mem_sam"]
    assert stored.start_utc == THU_4PM
    assert stored.end_utc == THU_5PM  # one-hour default
    assert stored.tier is Tier.HOUSEHOLD
    assert stored.tier_source is TierSource.HUMAN


def test_set_tier_stamps_a_human_source() -> None:
    tools, _, repo = harness(ALEX, events=[soccer(tier=Tier.BUSY, tier_source=TierSource.AUTO)])
    tools["set_tier"].call({"event_id": "evt_soccer", "tier": "T1"})
    stored = repo.get(HOUSEHOLD, "evt_soccer")
    assert stored.tier is Tier.HOUSEHOLD
    assert stored.tier_source is TierSource.HUMAN


def test_adult_can_set_visibility() -> None:
    tools, _, repo = harness(ALEX, events=[soccer(owner="mem_alex")])
    tools["set_visibility"].call({"event_id": "evt_soccer", "visibility": "adults"})
    assert repo.get(HOUSEHOLD, "evt_soccer").visibility is Visibility.ADULTS


def test_merge_and_unmerge_round_trip() -> None:
    twin = make_event("evt_twin", start=THU_4PM, end=THU_5PM, owner="mem_alex")
    tools, ctx, repo = harness(ALEX, events=[soccer(owner="mem_alex"), twin])
    tools["merge_events"].call({"event_ids": ["evt_soccer", "evt_twin"]})

    group = repo.get(HOUSEHOLD, "evt_soccer").merge_group_id
    assert group is not None
    assert repo.get(HOUSEHOLD, "evt_twin").merge_group_id == group

    tools["unmerge"].call({"group_id": group})
    assert repo.get(HOUSEHOLD, "evt_soccer").merge_group_id is None
    assert repo.get(HOUSEHOLD, "evt_twin").merge_group_id is None
    assert {o.tool for o in ctx.outcomes} == {"merge_events", "unmerge"}


def test_list_members_returns_the_roster() -> None:
    tools, ctx, _ = harness(RILEY)
    out = tools["list_members"].call({})
    for member in ROSTER:
        assert member.member_id in out
    assert ctx.outcomes == []  # reads are not actions


# --- input handling ----------------------------------------------------------


def test_unknown_event_is_a_tool_error_not_a_crash() -> None:
    tools, ctx, _ = harness(ALEX)
    with pytest.raises(ToolError):
        tools["delete_event"].call({"event_id": "evt_nope"})
    assert ctx.pending is None


def test_a_bad_date_is_a_tool_error() -> None:
    tools, ctx, _ = harness(ALEX)
    with pytest.raises(ToolError):
        tools["get_agenda"].call({"start": "thursday", "end": "thursday"})
    assert ctx.outcomes == []


def test_an_overlong_window_is_refused() -> None:
    tools, _, _ = harness(ALEX)
    with pytest.raises(ToolError):
        tools["get_agenda"].call({"start": "2026-01-01", "end": "2026-12-31"})


def test_an_unknown_member_is_refused() -> None:
    tools, _, _ = harness(ALEX)
    with pytest.raises(ToolError):
        tools["create_event"].call(
            {"title": "x", "start": "2026-08-06T16:00", "involves": ["mem_ghost"]}
        )


def test_no_tool_exposes_the_actor_in_its_schema() -> None:
    """The one field the model must never be able to name."""
    tools, _, _ = harness(ALEX)
    for tool in tools.values():
        schema = tool.to_dict()["input_schema"]
        assert "actor_member_id" not in schema["properties"]
        assert "household_id" not in schema["properties"]
