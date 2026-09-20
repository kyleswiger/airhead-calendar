"""The agent's tool surface (PRD §10.1, M2 contract).

Three properties of this module are load-bearing, and all three are enforced
here rather than in the prompt:

1. `actor_member_id` is server-injected. The tools are closures over a
   `ToolContext` that already holds the actor, so it appears in no tool's
   schema and the model has no way to name a different member.
2. Reads go through `scoped_query` — the same constructor the HTTP API uses —
   so an adults-only event is filtered out at the query layer and never reaches
   the model at all. There is no later redaction step to talk past.
3. Writes that need confirmation return a pending result *instead of* writing.
   The gate is harness state, not an instruction; a model that ignores it
   simply produces a pending result again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from airhead.agenda import build_agenda
from airhead.agent.prompt import calendar_data
from airhead.api.app import (
    MAX_SPAN_DAYS,
    ensure_may_delete,
    ensure_may_edit,
    ensure_may_set_visibility,
    scoped_query,
    span_utc,
    to_local,
)
from airhead.api.errors import ApiError, Conflict, Forbidden, InvalidRequest, NotFound
from airhead.domain import (
    Anchor,
    Event,
    EventSource,
    EventStatus,
    IntervalSource,
    Member,
    Routine,
    SourceKind,
    Tier,
    TierSource,
    Visibility,
)
from airhead.repo.base import EventRepo, MemberRepo, RoutineRepo
from airhead.routines import service

TierName = Literal["T1", "T2", "T3"]
VisibilityName = Literal["all", "adults"]
AnchorName = Literal["elapsed", "calendar"]

DEFAULT_DURATION = timedelta(hours=1)


# --- seam types --------------------------------------------------------------
#
# Defined here because they are tool-layer concepts, and re-exported from
# `airhead.agent.runner`, which is where the rest of the codebase imports them.


@dataclass(frozen=True, slots=True)
class Confirmation:
    """A human's answer to a gate, plus what the gate was about.

    `tool` and `args` come from the stored pending call, never from the client:
    they are what lets the harness replay the approved write itself instead of
    hoping the model re-issues the call on its next turn.
    """

    call_id: str
    approved: bool
    tool: str | None = None
    args: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    tool: str
    status: str  # "ok" | "error" | "pending_confirmation"
    event_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    call_id: str
    tool: str
    summary: str  # human-readable, shown on the kitchen screen
    event_id: str | None = None
    # The original arguments of the gated call, verbatim. Persisted with the
    # turn so an approval can be replayed by the harness with exactly the
    # arguments that were approved — never re-derived, never the model's.
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolContext:
    """Everything the tools are closed over, plus what they record.

    `outcomes` and `pending` are the channel back to the runner: a tool result
    is a string the model reads, but the harness needs the structured truth.
    """

    household_id: str
    actor: Member
    events: EventRepo
    members: MemberRepo
    routines: RoutineRepo
    now: datetime
    tz: str
    confirm: Confirmation | None = None
    outcomes: list[ToolOutcome] = field(default_factory=list)
    pending: PendingConfirmation | None = None
    # Call ids whose gate answer has already been acted on this turn, mapped to
    # the outcome that was recorded — the double-execution guard. A model that
    # re-issues a settled call gets a tool result describing what actually
    # happened (applied, declined, or failed) instead of a second write.
    settled: dict[str, ToolOutcome | None] = field(default_factory=dict)


# --- confirmation gate -------------------------------------------------------


def call_id_for(tool: str, *parts: str) -> str:
    """A stable id for one specific proposed write.

    Derived from the tool and its arguments rather than from the model's
    `tool_use` id, so the id survives the round trip through the display and a
    later turn, and so an approval for one call cannot authorize a different
    one — approving a delete of event A never approves a delete of event B.
    """
    blob = "|".join((tool, *parts))
    return "call_" + hashlib.sha256(blob.encode()).hexdigest()[:24]


class _Declined(Exception):
    """The person answered the gate with 'no'."""


class _AlreadySettled(Exception):
    """The gate's answer was already acted on this turn; nothing more to do."""

    def __init__(self, outcome: ToolOutcome | None = None) -> None:
        super().__init__()
        self.outcome = outcome


class _Pending(Exception):
    """The gate has not been answered yet, so the write must not happen."""

    def __init__(self, pending: PendingConfirmation) -> None:
        super().__init__(pending.call_id)
        self.pending = pending


def _gate(
    ctx: ToolContext,
    *,
    tool: str,
    call_id: str,
    summary: str,
    event_id: str | None,
    args: dict[str, Any],
) -> None:
    """Return normally only when this specific write has been approved.

    Raises on every other path, so the write below the call site is unreachable
    until a matching approval exists. An approval for a different call id is not
    an approval for this one, and an approval that was already applied — the
    harness replays approvals itself — never applies twice.
    """
    if call_id in ctx.settled:
        raise _AlreadySettled(ctx.settled[call_id])
    confirm = ctx.confirm
    if confirm is not None and confirm.call_id == call_id:
        if confirm.approved:
            return
        raise _Declined
    raise _Pending(
        PendingConfirmation(
            call_id=call_id, tool=tool, summary=summary, event_id=event_id, args=dict(args)
        )
    )


# --- helpers -----------------------------------------------------------------


def _visible(ctx: ToolContext, event_id: str) -> Event:
    """Load an event the actor is allowed to see, or 404.

    Same rule as the HTTP API: "exists but is adults-only" and "does not exist"
    are indistinguishable, because the difference is itself a disclosure.
    """
    stored = ctx.events.get(ctx.household_id, event_id)
    if stored is None or not scoped_query(ctx.actor, ctx.household_id).allows(stored):
        raise NotFound("No such event.")
    return stored


def _load_range(
    ctx: ToolContext,
    *,
    start_utc: datetime,
    end_utc: datetime,
    min_tier: Tier,
    member_ids: tuple[str, ...] | None,
) -> list[Event]:
    query = scoped_query(
        ctx.actor,
        ctx.household_id,
        start_utc=start_utc,
        end_utc=end_utc,
        min_tier=min_tier,
        member_ids=member_ids,
    )
    stored: list[Event] = []
    cursor: str | None = None
    while True:
        page = ctx.events.list_range(query, cursor=cursor)
        stored.extend(page.events)
        cursor = page.cursor
        if cursor is None:
            return stored


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise InvalidRequest(f"`{label}` must be a date like 2026-08-06.") from exc


def _parse_local(value: str, label: str) -> datetime:
    """Parse a floating local datetime; a bare date means midnight."""
    text = value.strip().replace("Z", "").split("+")[0]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidRequest(f"`{label}` must look like 2026-08-06T16:00.") from exc
    return parsed.replace(tzinfo=None)


def _window(ctx: ToolContext, start: str, end: str) -> tuple[date, date, datetime, datetime]:
    start_date, end_date = _parse_date(start, "start"), _parse_date(end, "end")
    if end_date < start_date:
        raise InvalidRequest("`end` may not precede `start`.")
    if (end_date - start_date).days + 1 > MAX_SPAN_DAYS:
        raise InvalidRequest(f"Ask for at most {MAX_SPAN_DAYS} days at a time.")
    midnight = datetime.min.time()
    start_utc, _ = span_utc(
        datetime.combine(start_date, midnight),
        datetime.combine(start_date, midnight),
        ctx.tz,
        all_day=False,
    )
    end_utc, _ = span_utc(
        datetime.combine(end_date + timedelta(days=1), midnight),
        datetime.combine(end_date + timedelta(days=1), midnight),
        ctx.tz,
        all_day=False,
    )
    return start_date, end_date, start_utc, end_utc


def _tier(name: str | None, default: Tier) -> Tier:
    return Tier(name) if name else default


def _known_members(ctx: ToolContext) -> dict[str, Member]:
    return {m.member_id: m for m in ctx.members.list(ctx.household_id)}


def _check_members(ctx: ToolContext, ids: list[str], label: str) -> list[str]:
    known = _known_members(ctx)
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise InvalidRequest(f"Unknown member id in `{label}`.")
    return list(dict.fromkeys(ids))


def _when(ctx: ToolContext, event: Event) -> str:
    """A human phrase for a confirmation summary, in the household timezone."""
    if event.all_day:
        return event.start_utc.replace(tzinfo=None).strftime("%A, %b %-d")
    return to_local(event.start_utc, ctx.tz).strftime("%A at %-I:%M %p")


def _row(ctx: ToolContext, event: Event) -> dict[str, Any]:
    # All-day events are stored floating; running one through a zone is how it
    # slides onto the wrong day.
    if event.all_day:
        start = event.start_utc.replace(tzinfo=None)
        end = event.end_utc.replace(tzinfo=None)
    else:
        start = to_local(event.start_utc, ctx.tz)
        end = to_local(event.end_utc, ctx.tz)
    return {
        "eventId": event.event_id,
        "title": event.title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "allDay": event.all_day,
        "tier": event.tier.value,
        "tierSource": event.tier_source.value,
        "ownerMemberId": event.owner_member_id,
        "memberIds": sorted({event.owner_member_id, *event.involves}),
        "location": event.location,
        "visibility": event.visibility.value,
        "status": event.status.value,
        "mergeGroupId": event.merge_group_id,
    }


def _saved(ctx: ToolContext, event: Event, tool: str, note: str) -> str:
    stored = ctx.events.put(event)
    ctx.outcomes.append(ToolOutcome(tool=tool, status="ok", event_id=stored.event_id))
    return f"{note}\n{calendar_data(_row(ctx, stored))}"


# --- routine helpers ---------------------------------------------------------


def _today(ctx: ToolContext) -> date:
    """The household-local date: a routine is done "on a day", never at an instant."""
    return to_local(ctx.now, ctx.tz).date()


def _visible_routine(ctx: ToolContext, routine_id: str) -> Routine:
    """Load a routine the actor may see, or 404 — the same rule as `_visible`."""
    stored = ctx.routines.get(ctx.household_id, routine_id)
    if stored is None or not service.visible_to(ctx.actor, stored):
        raise NotFound("No such routine.")
    return stored


def _routine_row(ctx: ToolContext, routine: Routine, *, today: date) -> dict[str, Any]:
    """The `RoutineOut` shape from ROUTINES-CONTRACT § HTTP, camelCase."""
    history = ctx.routines.list_completions(ctx.household_id, routine.routine_id)
    return {
        "routineId": routine.routine_id,
        "name": routine.name,
        "category": routine.category,
        "ownerMemberId": routine.owner_member_id,
        "memberIds": sorted({routine.owner_member_id, *routine.involves}),
        "tier": routine.tier.value,
        "visibility": routine.visibility.value,
        "intervalDays": routine.interval_days,
        "intervalSource": routine.interval_source.value if routine.interval_days else None,
        "intervalNote": routine.interval_note,
        "intervalConfidence": routine.interval_confidence,
        "anchor": routine.anchor.value,
        "catalogKey": routine.catalog_key,
        "lastDoneOn": routine.last_done_on.isoformat() if routine.last_done_on else None,
        "dueOn": routine.due_on.isoformat() if routine.due_on else None,
        "dueEventId": routine.due_event_id,
        "status": service.status(routine, today=today),
        "daysUntilDue": service.days_until_due(routine, today=today),
        "completionCount": len(history),
        "paused": routine.paused,
    }


def _routine_saved(ctx: ToolContext, routine: Routine, tool: str, note: str) -> str:
    """Record a routine write. `event_id` is the due event, the routine id is `detail`."""
    ctx.outcomes.append(
        ToolOutcome(
            tool=tool, status="ok", event_id=routine.due_event_id, detail=routine.routine_id
        )
    )
    return f"{note}\n{calendar_data(_routine_row(ctx, routine, today=_today(ctx)))}"


def _interval_note(routine: Routine) -> str:
    """One line telling the model how much to trust the cadence it just stored."""
    if routine.interval_days is None:
        return "No interval is known, so it is unscheduled; ask how often if it matters."
    if routine.interval_source is IntervalSource.ESTIMATED:
        return (
            f"The {routine.interval_days}-day interval is an estimate. Tell the person the "
            "cadence you assumed in plain words and that they can correct it."
        )
    if routine.interval_source is IntervalSource.CATALOG:
        return f"Interval of {routine.interval_days} days came from the catalog."
    return f"Interval is {routine.interval_days} days."


# --- implementations ---------------------------------------------------------
#
# Kept out of the decorated closures so the schema stays legible and the logic
# stays testable without going through the model.


def _get_agenda(
    ctx: ToolContext, start: str, end: str, member: str | None, min_tier: str | None
) -> str:
    start_date, end_date, start_utc, end_utc = _window(ctx, start, end)
    tier = _tier(min_tier, Tier.BUSY)
    roster = ctx.members.list(ctx.household_id)
    member_ids: tuple[str, ...] | None = None
    if member:
        _check_members(ctx, [member], "member")
        member_ids = (member,)
        roster = [m for m in roster if m.member_id == member]

    events = _load_range(
        ctx, start_utc=start_utc, end_utc=end_utc, min_tier=tier, member_ids=member_ids
    )
    view = build_agenda(
        events=events, members=roster, tz=ctx.tz, start=start_date, end=end_date, min_tier=tier
    )
    days = [
        {
            "date": day.date.isoformat(),
            "busy": [
                {
                    "memberId": band.member_id,
                    "start": band.start_local.isoformat(),
                    "end": band.end_local.isoformat(),
                    "count": band.count,
                }
                for band in day.busy
            ],
            "events": [
                {
                    "eventId": row.event_id,
                    "title": row.title,
                    "start": row.start_local.isoformat(),
                    "end": row.end_local.isoformat(),
                    "allDay": row.all_day,
                    "tier": row.tier.value,
                    "ownerMemberId": row.owner_member_id,
                    "memberIds": list(row.member_ids),
                    "location": row.location,
                }
                for row in day.events
            ],
        }
        for day in view.days
    ]
    return calendar_data({"timezone": ctx.tz, "days": days})


def _find_conflicts(ctx: ToolContext, start: str, end: str) -> str:
    start_date, end_date, start_utc, end_utc = _window(ctx, start, end)
    roster = ctx.members.list(ctx.household_id)
    events = _load_range(
        ctx, start_utc=start_utc, end_utc=end_utc, min_tier=Tier.PERSONAL, member_ids=None
    )
    view = build_agenda(
        events=events,
        members=roster,
        tz=ctx.tz,
        start=start_date,
        end=end_date,
        min_tier=Tier.PERSONAL,  # T3 only means "unavailable"; it is not a conflict.
    )

    clashes: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for day in view.days:
        for i, left in enumerate(day.events):
            for right in day.events[i + 1 :]:
                shared = sorted(set(left.member_ids) & set(right.member_ids))
                if not shared:
                    continue
                if left.start_local >= right.end_local or right.start_local >= left.end_local:
                    continue
                key = (left.event_id, right.event_id, day.date.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                clashes.append(
                    {
                        "date": day.date.isoformat(),
                        "memberIds": shared,
                        "events": [
                            {
                                "eventId": row.event_id,
                                "title": row.title,
                                "start": row.start_local.isoformat(),
                                "end": row.end_local.isoformat(),
                                "tier": row.tier.value,
                            }
                            for row in (left, right)
                        ],
                    }
                )
    return calendar_data({"timezone": ctx.tz, "conflicts": clashes})


def _list_members(ctx: ToolContext) -> str:
    roster = [
        {"memberId": m.member_id, "name": m.display_name, "role": m.role.value}
        for m in ctx.members.list(ctx.household_id)
    ]
    return json.dumps({"members": roster}, sort_keys=True)


def _create_event(
    ctx: ToolContext,
    title: str,
    start: str,
    end: str | None,
    all_day: bool,
    tier: str,
    owner_member_id: str | None,
    involves: list[str] | None,
    location: str | None,
    visibility: str | None,
) -> str:
    owner = owner_member_id or ctx.actor.member_id
    # Issue #4, resolved: same rule as POST /api/events. A minor creating for someone
    # else lands a `proposed` event; an adult confirms it (or deletes it) on screen.
    # Not routed through the turn gate on purpose - the gate is answered by whoever
    # holds the conversation, which here is the minor, and a proposal a minor can
    # approve for themselves is not adult confirmation.
    status = EventStatus.CONFIRMED
    note = "Created."
    if not ctx.actor.is_adult and owner != ctx.actor.member_id:
        status = EventStatus.PROPOSED
        note = (
            "Created as a proposal - an adult must confirm it before it is final. "
            "Tell the person that in one short sentence."
        )
    if visibility is not None:
        ensure_may_set_visibility(ctx.actor)
    _check_members(ctx, [owner], "owner_member_id")
    people = _check_members(ctx, involves or [], "involves")

    if all_day:
        start_local = datetime.combine(_parse_date(start, "start"), datetime.min.time())
        end_date = _parse_date(end, "end") if end else _parse_date(start, "start")
        end_local = datetime.combine(end_date + timedelta(days=1), datetime.min.time())
    else:
        start_local = _parse_local(start, "start")
        end_local = _parse_local(end, "end") if end else start_local + DEFAULT_DURATION
    if end_local < start_local:
        raise InvalidRequest("`end` precedes `start`.")

    start_utc, end_utc = span_utc(start_local, end_local, ctx.tz, all_day=all_day)
    event = Event(
        event_id=f"evt_{uuid.uuid4().hex}",
        household_id=ctx.household_id,
        title=title.strip(),
        start_utc=start_utc,
        end_utc=end_utc,
        tz=ctx.tz,
        owner_member_id=owner,
        source=EventSource(kind=SourceKind.NATIVE),
        all_day=all_day,
        involves=people,
        location=location,
        tier=_tier(tier, Tier.PERSONAL),
        # A tier chosen in conversation is a human decision, exactly as through
        # PATCH — otherwise the next sync "corrects" it away.
        tier_source=TierSource.HUMAN,
        visibility=Visibility(visibility) if visibility else Visibility.ALL,
        status=status,
        created_by=ctx.actor.member_id,
    )
    return _saved(ctx, event, "create_event", note)


def _confirm_event(ctx: ToolContext, event_id: str) -> str:
    """Mirror of `POST /api/events/{id}/confirm` (issue #4).

    Adult-only by role, not by gate: the turn gate is answered by whoever holds
    the conversation, and a minor holding it could approve their own proposal.
    The adult check is therefore on the actor's identity, which is
    server-injected and not in any tool schema.
    """
    if not ctx.actor.is_adult:
        raise Forbidden("Only adults may confirm a proposed event.")
    stored = _visible(ctx, event_id)
    if stored.status is not EventStatus.PROPOSED:
        raise Conflict("Event is not awaiting confirmation.", code="not_proposed")
    stored.status = EventStatus.CONFIRMED
    return _saved(ctx, stored, "confirm_event", "Confirmed.")


def _update_event(
    ctx: ToolContext,
    event_id: str,
    title: str | None,
    start: str | None,
    end: str | None,
    all_day: bool | None,
    location: str | None,
    involves: list[str] | None,
) -> str:
    stored = _visible(ctx, event_id)
    ensure_may_edit(ctx.actor, stored)

    patch: dict[str, Any] = {
        "title": title,
        "start": start,
        "end": end,
        "all_day": all_day,
        "location": location,
        "involves": involves,
    }
    fields = {k: v for k, v in patch.items() if v is not None}
    if not fields:
        raise InvalidRequest("Nothing to change.")

    # A re-issue of the exact call the harness already replayed this turn is
    # "already done", not a second write.
    same_call = call_id_for(
        "update_event", event_id, json.dumps(fields, sort_keys=True, default=str)
    )
    if same_call in ctx.settled:
        raise _AlreadySettled(ctx.settled[same_call])

    if stored.owner_member_id != ctx.actor.member_id:
        # Editing somebody else's calendar is a gate — the patch is part of the
        # id, so an approval authorizes this change and no other.
        _gate(
            ctx,
            tool="update_event",
            call_id=call_id_for(
                "update_event", event_id, json.dumps(fields, sort_keys=True, default=str)
            ),
            summary=f'Change "{stored.title}" on {_when(ctx, stored)}?',
            event_id=event_id,
            args={"event_id": event_id, **fields},
        )

    if title is not None:
        stored.title = title.strip()
    if location is not None:
        stored.location = location
    if involves is not None:
        stored.involves = _check_members(ctx, involves, "involves")
    if all_day is not None:
        stored.all_day = all_day
    if start is not None or end is not None:
        if stored.all_day:
            start_local = (
                datetime.combine(_parse_date(start, "start"), datetime.min.time())
                if start
                else stored.start_utc.replace(tzinfo=None)
            )
            end_local = (
                datetime.combine(_parse_date(end, "end") + timedelta(days=1), datetime.min.time())
                if end
                else stored.end_utc.replace(tzinfo=None)
            )
        else:
            start_local = (
                _parse_local(start, "start") if start else to_local(stored.start_utc, ctx.tz)
            )
            end_local = (
                _parse_local(end, "end")
                if end
                else (
                    start_local + (stored.end_utc - stored.start_utc)
                    if start
                    else to_local(stored.end_utc, ctx.tz)
                )
            )
        if end_local < start_local:
            raise InvalidRequest("`end` precedes `start`.")
        stored.start_utc, stored.end_utc = span_utc(
            start_local, end_local, ctx.tz, all_day=stored.all_day
        )
    return _saved(ctx, stored, "update_event", "Updated.")


def _delete_event(ctx: ToolContext, event_id: str) -> str:
    # Before the load: a replayed delete removed the event, so a model re-issue
    # would otherwise read as "no such event" instead of "already done".
    if call_id_for("delete_event", event_id) in ctx.settled:
        raise _AlreadySettled(ctx.settled[call_id_for("delete_event", event_id)])
    stored = _visible(ctx, event_id)
    ensure_may_delete(ctx.actor, stored)
    _gate(
        ctx,
        tool="delete_event",
        call_id=call_id_for("delete_event", event_id),
        summary=f'Delete "{stored.title}" on {_when(ctx, stored)}?',
        event_id=event_id,
        args={"event_id": event_id},
    )
    ctx.events.delete(ctx.household_id, event_id, at=datetime.now(UTC))
    ctx.outcomes.append(ToolOutcome(tool="delete_event", status="ok", event_id=event_id))
    return "Deleted."


def _set_tier(ctx: ToolContext, event_id: str, tier: str) -> str:
    stored = _visible(ctx, event_id)
    ensure_may_edit(ctx.actor, stored)
    stored.tier = Tier(tier)
    # PRD §6.1 rule 5: this stamp is what makes the correction survive the next poll.
    stored.tier_source = TierSource.HUMAN
    return _saved(ctx, stored, "set_tier", f"Tier set to {tier}.")


def _set_visibility(ctx: ToolContext, event_id: str, visibility: str) -> str:
    # Adults-only, checked before the load so the answer does not depend on
    # whether the minor could see the event.
    ensure_may_set_visibility(ctx.actor)
    stored = _visible(ctx, event_id)
    ensure_may_edit(ctx.actor, stored)
    stored.visibility = Visibility(visibility)
    return _saved(ctx, stored, "set_visibility", f"Visibility set to {visibility}.")


def _merge_events(ctx: ToolContext, event_ids: list[str]) -> str:
    unique = list(dict.fromkeys(event_ids))
    if len(unique) < 2:
        raise InvalidRequest("Merging needs at least two events.")
    stored = [_visible(ctx, event_id) for event_id in unique]
    for event in stored:
        ensure_may_edit(ctx.actor, event)
    group = next((e.merge_group_id for e in stored if e.merge_group_id), None)
    group = group or f"mrg_{uuid.uuid4().hex}"
    for event in stored:
        event.merge_group_id = group
        saved = ctx.events.put(event)
        ctx.outcomes.append(
            ToolOutcome(tool="merge_events", status="ok", event_id=saved.event_id, detail=group)
        )
    return f"Merged {len(stored)} events into group {group}."


def _unmerge(ctx: ToolContext, group_id: str) -> str:
    events = _load_range(
        ctx,
        start_utc=datetime.min.replace(tzinfo=UTC),
        end_utc=datetime.max.replace(tzinfo=UTC),
        min_tier=Tier.BUSY,
        member_ids=None,
    )
    members = [e for e in events if e.merge_group_id == group_id]
    if not members:
        raise NotFound("No such merge group.")
    for event in members:
        ensure_may_edit(ctx.actor, event)
    for event in members:
        event.merge_group_id = None
        saved = ctx.events.put(event)
        ctx.outcomes.append(
            ToolOutcome(tool="unmerge", status="ok", event_id=saved.event_id, detail=group_id)
        )
    return f"Unmerged {len(members)} events."


# --- routines ----------------------------------------------------------------
#
# Thin wrappers over `airhead.routines.service`, exactly as the event tools mirror
# `app.py`: the HTTP routes and these tools must behave identically.


def _list_routines(ctx: ToolContext) -> str:
    today = _today(ctx)
    # Same as GET /api/routines: re-project first so an overdue due-event has
    # already been rolled to today, then filter at the query layer.
    rows = service.reproject_all(
        ctx.routines, ctx.events, household_id=ctx.household_id, today=today, tz=ctx.tz
    )
    visible = [r for r in rows if service.visible_to(ctx.actor, r)]
    visible.sort(key=lambda r: service.sort_key(r, today=today))
    return calendar_data(
        {
            "today": today.isoformat(),
            "routines": [_routine_row(ctx, r, today=today) for r in visible],
        }
    )


def _log_done(ctx: ToolContext, routine_id: str, done_on: str | None, note: str | None) -> str:
    stored = _visible_routine(ctx, routine_id)
    today = _today(ctx)
    when = _parse_date(done_on, "done_on") if done_on else None
    try:
        routine, completion = service.complete(
            stored,
            ctx.routines,
            ctx.events,
            by=ctx.actor,
            done_on=when,
            note=note,
            today=today,
            tz=ctx.tz,
        )
    except service.FutureCompletion as exc:
        raise InvalidRequest(str(exc), code="future_completion") from exc
    ctx.outcomes.append(
        ToolOutcome(
            tool="log_done", status="ok", event_id=routine.due_event_id, detail=routine.routine_id
        )
    )
    row = _routine_row(ctx, routine, today=today)
    return (
        f"Logged as done on {completion.done_on.isoformat()}.\n"
        f"{calendar_data({'completionId': completion.completion_id, 'routine': row})}"
    )


def _create_routine(
    ctx: ToolContext,
    name: str,
    interval_days: int | None,
    interval_stated_by_person: bool,
    interval_note: str | None,
    interval_confidence: float | None,
    last_done_on: str | None,
    due_on: str | None,
    anchor: str | None,
    category: str | None,
    owner_member_id: str | None,
    involves: list[str] | None,
    tier: str,
    visibility: str | None,
) -> str:
    if not name.strip():
        raise InvalidRequest("`name` is required.")
    owner = owner_member_id or ctx.actor.member_id
    if not ctx.actor.is_adult and owner != ctx.actor.member_id:
        raise Forbidden("Minors may only create routines for themselves.")
    if visibility is not None:
        ensure_may_set_visibility(ctx.actor)
    known = _known_members(ctx)
    if owner not in known:
        raise InvalidRequest("Unknown member id in `owner_member_id`.")
    people = _check_members(ctx, involves or [], "involves")
    if interval_days is not None and interval_days < 1:
        raise InvalidRequest("`interval_days` must be at least 1.")
    today = _today(ctx)
    first_done = _parse_date(last_done_on, "last_done_on") if last_done_on else None
    if first_done is not None and first_done > today:
        # Checked here rather than left to `service.complete`, which would raise
        # only after the routine row had already been written.
        raise InvalidRequest("`last_done_on` may not be in the future.", code="future_completion")

    # Ground rule 1: the person's number is `human`; the model's guess is an
    # `estimated` resolution that `service.create` applies only when the catalog
    # misses. The tool never decides which wins — the service does.
    stated = interval_days if interval_stated_by_person else None
    estimate: service.Resolution | None = None
    if interval_days is not None and not interval_stated_by_person:
        estimate = service.Resolution(
            interval_days=interval_days,
            source=IntervalSource.ESTIMATED,
            note=(interval_note or "").strip() or None,
            confidence=interval_confidence,
        )

    routine = service.create(
        ctx.routines,
        ctx.events,
        household_id=ctx.household_id,
        name=name,
        owner=known[owner],
        created_by=ctx.actor,
        today=today,
        tz=ctx.tz,
        stated_interval=stated,
        estimate=estimate,
        category=category,
        anchor=Anchor(anchor) if anchor else None,
        involves=people,
        tier=_tier(tier, Tier.PERSONAL),
        visibility=Visibility(visibility) if visibility else Visibility.ALL,
        last_done_on=first_done,
        due_on=_parse_date(due_on, "due_on") if due_on else None,
    )
    return _routine_saved(ctx, routine, "create_routine", f"Created. {_interval_note(routine)}")


def _update_routine(
    ctx: ToolContext,
    routine_id: str,
    name: str | None,
    interval_days: int | None,
    due_on: str | None,
    paused: bool | None,
    involves: list[str] | None,
    tier: str | None,
    visibility: str | None,
    category: str | None,
) -> str:
    stored = _visible_routine(ctx, routine_id)
    if visibility is not None:
        ensure_may_set_visibility(ctx.actor)
    if not ctx.actor.is_adult and stored.owner_member_id != ctx.actor.member_id:
        raise Forbidden("Minors may only change their own routines.")

    patch: dict[str, Any] = {
        "name": name,
        "interval_days": interval_days,
        "due_on": due_on,
        "paused": paused,
        "involves": involves,
        "tier": tier,
        "visibility": visibility,
        "category": category,
    }
    fields = {k: v for k, v in patch.items() if v is not None}
    if not fields:
        raise InvalidRequest("Nothing to change.")

    call_id = call_id_for(
        "update_routine", routine_id, json.dumps(fields, sort_keys=True, default=str)
    )
    if call_id in ctx.settled:
        raise _AlreadySettled(ctx.settled[call_id])
    if stored.owner_member_id != ctx.actor.member_id:
        _gate(
            ctx,
            tool="update_routine",
            call_id=call_id,
            summary=f'Change "{stored.name}"?',
            event_id=stored.due_event_id,
            args={"routine_id": routine_id, **fields},
        )

    today = _today(ctx)
    if name is not None:
        if not name.strip():
            raise InvalidRequest("`name` may not be empty.")
        stored.name = name.strip()
    if category is not None:
        stored.category = category
    if involves is not None:
        stored.involves = _check_members(ctx, involves, "involves")
    if tier is not None:
        stored.tier = Tier(tier)
    if visibility is not None:
        stored.visibility = Visibility(visibility)
    if paused is not None:
        stored.paused = paused
    if interval_days is not None:
        if interval_days < 1:
            raise InvalidRequest("`interval_days` must be at least 1.")
        # Same as PATCH: a number given here is the person's, and sticky.
        service.apply_interval(
            stored,
            service.Resolution(interval_days=interval_days, source=IntervalSource.HUMAN),
        )
        if due_on is None:
            stored.due_on = service.next_due(stored, today=today)
    if due_on is not None:
        # A snooze: kept until the next completion.
        stored.due_on = _parse_date(due_on, "due_on")

    saved = ctx.routines.put(stored)
    saved = service.reproject(saved, ctx.routines, ctx.events, today=today, tz=ctx.tz)
    return _routine_saved(ctx, saved, "update_routine", "Updated.")


def _delete_routine(ctx: ToolContext, routine_id: str) -> str:
    call_id = call_id_for("delete_routine", routine_id)
    # Before the load, as for `_delete_event`: a replayed delete tombstoned it.
    if call_id in ctx.settled:
        raise _AlreadySettled(ctx.settled[call_id])
    stored = _visible_routine(ctx, routine_id)
    if not ctx.actor.is_adult and (stored.created_by or stored.owner_member_id) != (
        ctx.actor.member_id
    ):
        raise Forbidden("Minors may only delete routines they created.")
    _gate(
        ctx,
        tool="delete_routine",
        call_id=call_id,
        summary=f'Delete "{stored.name}" and its reminder?',
        event_id=stored.due_event_id,
        args={"routine_id": routine_id},
    )
    service.remove(stored, ctx.routines, ctx.events, at=datetime.now(UTC))
    ctx.outcomes.append(
        ToolOutcome(
            tool="delete_routine", status="ok", event_id=stored.due_event_id, detail=routine_id
        )
    )
    return "Deleted."


def _undo_completion(ctx: ToolContext, routine_id: str, completion_id: str) -> str:
    stored = _visible_routine(ctx, routine_id)
    today = _today(ctx)
    saved = service.undo_completion(
        stored, ctx.routines, ctx.events, completion_id=completion_id, today=today, tz=ctx.tz
    )
    if saved is None:
        raise NotFound("No such completion.")
    return _routine_saved(ctx, saved, "undo_completion", "Completion removed.")


# --- the tool surface --------------------------------------------------------


def _read(fn: Callable[[], str]) -> str:
    """A read that failed is a tool error, not an action — nothing to record."""
    try:
        return fn()
    except ApiError as exc:
        raise ToolError(exc.message) from exc


def _safe(ctx: ToolContext, tool: str, fn: Callable[[], str]) -> str:
    """Translate the three outcomes a write can have into a tool result.

    A gate and an authorization failure both come back as ordinary tool results
    the model can read; neither is an exception it can route around.
    """
    try:
        return fn()
    except _Pending as gate:
        ctx.pending = gate.pending
        ctx.outcomes.append(
            ToolOutcome(
                tool=tool,
                status="pending_confirmation",
                event_id=gate.pending.event_id,
                detail=gate.pending.call_id,
            )
        )
        return (
            f"NOT DONE — this needs confirmation from the person first: {gate.pending.summary} "
            "Ask them in one short sentence and stop. Do not retry this call."
        )
    except _Declined:
        ctx.outcomes.append(ToolOutcome(tool=tool, status="error", detail="declined"))
        return "NOT DONE — the person declined this change."
    except _AlreadySettled as settled:
        # No new outcome: the settled answer was already recorded, and a
        # duplicate row in the audit log would claim a second write that never
        # happened. The message reflects what actually happened, so a declined
        # or failed replay is never narrated as applied.
        outcome = settled.outcome
        if outcome is not None and outcome.status == "error":
            if outcome.detail == "declined":
                return "NOT DONE — the person declined this change. Do not repeat this call."
            return (
                "NOT DONE — the system already tried to apply this change and it "
                "failed. Do not repeat this call."
            )
        return (
            "Already handled — the person's answer to this request was applied "
            "by the system. Do not repeat this call."
        )
    except ApiError as exc:
        ctx.outcomes.append(ToolOutcome(tool=tool, status="error", detail=exc.code))
        raise ToolError(exc.message) from exc


def settle_confirmation(ctx: ToolContext) -> ToolOutcome | None:
    """Act on an answered gate before the model gets a say.

    An approval is *replayed*: the harness executes the gated write itself, with
    the original arguments stored on the pending call, exactly once. The model
    is then only narrating — a turn where it never re-issues the tool call still
    performs the write, and a turn where it does re-issue it hits the settled
    guard in `_gate` instead of writing twice. A decline is recorded the same
    way, so the audit log carries the answer whether or not the model mentions it.

    Returns the outcome that was recorded, or None when there was nothing to do
    (no answer this turn, or an answer whose gated call the route did not name —
    the legacy path, where the model's re-issue carries the write).
    """
    confirm = ctx.confirm
    if confirm is None or confirm.call_id in ctx.settled or not confirm.tool:
        return None

    before = len(ctx.outcomes)
    if not confirm.approved:
        ctx.outcomes.append(ToolOutcome(tool=confirm.tool, status="error", detail="declined"))
        ctx.settled[confirm.call_id] = ctx.outcomes[-1]
        return ctx.outcomes[-1]

    args = confirm.args or {}
    replays: dict[str, Callable[[], str]] = {
        "delete_event": lambda: _delete_event(ctx, str(args["event_id"])),
        "update_event": lambda: _update_event(
            ctx,
            str(args["event_id"]),
            args.get("title"),
            args.get("start"),
            args.get("end"),
            args.get("all_day"),
            args.get("location"),
            args.get("involves"),
        ),
        "delete_routine": lambda: _delete_routine(ctx, str(args["routine_id"])),
        "update_routine": lambda: _update_routine(
            ctx,
            str(args["routine_id"]),
            args.get("name"),
            args.get("interval_days"),
            args.get("due_on"),
            args.get("paused"),
            args.get("involves"),
            args.get("tier"),
            args.get("visibility"),
            args.get("category"),
        ),
    }
    if "event_id" not in args and "routine_id" not in args:
        # A pending row persisted before args were stored loads back with
        # args={} — nothing safe to replay. Fall back to the legacy path (do
        # not settle), so the model's re-issue can still carry the write.
        return None

    fn = replays.get(confirm.tool)
    if fn is None:
        ctx.outcomes.append(ToolOutcome(tool=confirm.tool, status="error", detail="unreplayable"))
    else:
        # `_safe` records the error outcome before raising ToolError; there is
        # no model here to read the message, so the exception is the noise.
        with contextlib.suppress(ToolError):
            _safe(ctx, confirm.tool, fn)
    outcome = ctx.outcomes[before] if len(ctx.outcomes) > before else None
    ctx.settled[confirm.call_id] = outcome
    return outcome


def build_tools(ctx: ToolContext) -> list[Any]:
    """The tools for one turn, closed over the actor and the repositories.

    Note what is *not* in any signature below: the household, the actor, the
    clock. The model cannot address a different household or act as a different
    member, because it is never asked who it is.
    """

    @beta_tool
    def get_agenda(
        start: str,
        end: str,
        member: str | None = None,
        min_tier: TierName | None = None,
    ) -> str:
        """Read the household calendar for a date range.

        Call this before answering any question about what is happening, and
        before changing or deleting an event, to find its event id. Do not
        answer from memory of an earlier turn — the calendar changes underneath
        you. Results are already limited to what the person speaking may see.

        Args:
            start: First day, as a date like 2026-08-06.
            end: Last day, inclusive, at most 31 days after start.
            member: Optional member id to narrow to one person.
            min_tier: Least relevant tier to include. T1 for only household
                events, T2 to add personal ones, T3 (the default) for everything
                including collapsed busy time.
        """
        return _read(lambda: _get_agenda(ctx, start, end, member, min_tier))

    @beta_tool
    def find_conflicts(start: str, end: str) -> str:
        """Find events that overlap in time for the same person.

        Call this before scheduling something that could collide — a pickup, a
        drive, anything where being in two places matters. Busy-tier blocks are
        not conflicts and are excluded.

        Args:
            start: First day, as a date like 2026-08-06.
            end: Last day, inclusive.
        """
        return _read(lambda: _find_conflicts(ctx, start, end))

    @beta_tool
    def list_members() -> str:
        """List the household members, their ids and whether they are adults.

        Call this only when a name in the request does not match anyone in the
        roster you were given; the roster is already in your context.
        """
        return _read(lambda: _list_members(ctx))

    @beta_tool
    def create_event(
        title: str,
        start: str,
        end: str | None = None,
        all_day: bool = False,
        tier: TierName = "T2",
        owner_member_id: str | None = None,
        involves: list[str] | None = None,
        location: str | None = None,
        visibility: VisibilityName | None = None,
    ) -> str:
        """Add a new event to the calendar.

        Call this once you know what, when and for whom. Choose the tier
        deliberately: T1 if it constrains somebody else (a pickup, a drive, a
        shared meal), T2 for a personal commitment, T3 for work or focus time.
        Owner defaults to the person speaking, so pass owner_member_id only when
        the event is really somebody else's. When a minor creates an event for
        someone else it is stored as a proposal an adult must confirm; relay
        that when it happens.

        Args:
            title: What the event is, in the household's own words.
            start: Local start, like 2026-08-06T16:00, or a date if all_day.
            end: Local end. Defaults to one hour after start.
            all_day: True for something that occupies a whole day.
            owner_member_id: Whose event it is. Defaults to the speaker.
            tier: T1, T2 or T3 — see above.
            involves: Member ids of other people this event constrains, such as
                the parent doing the driving.
            location: Where it is, if stated.
            visibility: "adults" hides it from minors. Adults only; leave unset
                unless the person explicitly asks for it to be private.
        """
        return _safe(
            ctx,
            "create_event",
            lambda: _create_event(
                ctx,
                title,
                start,
                end,
                all_day,
                tier,
                owner_member_id,
                involves,
                location,
                visibility,
            ),
        )

    @beta_tool
    def confirm_event(event_id: str) -> str:
        """Confirm an event a minor proposed for someone else.

        Adults only. Call this when an adult approves a proposed event — one
        get_agenda reports with status "proposed". An adult who disagrees
        deletes the proposal instead; there is no separate decline.

        Args:
            event_id: The id from get_agenda.
        """
        return _safe(ctx, "confirm_event", lambda: _confirm_event(ctx, event_id))

    @beta_tool
    def update_event(
        event_id: str,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        location: str | None = None,
        involves: list[str] | None = None,
    ) -> str:
        """Change an existing event. Pass only the fields that change.

        Call this to move, rename or relocate something that already exists —
        never delete and recreate. Editing an event that belongs to someone else
        needs their confirmation and will come back as pending; that is normal.
        Use set_tier for the tier and set_visibility for visibility.

        Args:
            event_id: The id from get_agenda.
            title: New title.
            start: New local start, like 2026-08-06T16:30.
            end: New local end. If only start moves, the duration is kept.
            all_day: Switch between timed and all-day.
            location: New location.
            involves: Replacement list of member ids this event constrains.
        """
        return _safe(
            ctx,
            "update_event",
            lambda: _update_event(ctx, event_id, title, start, end, all_day, location, involves),
        )

    @beta_tool
    def delete_event(event_id: str) -> str:
        """Remove an event from the calendar.

        Call this when someone says an event is cancelled or should come off the
        calendar. This always needs confirmation and will come back as pending
        the first time; relay the question and wait.

        Args:
            event_id: The id from get_agenda.
        """
        return _safe(ctx, "delete_event", lambda: _delete_event(ctx, event_id))

    @beta_tool
    def set_tier(event_id: str, tier: TierName) -> str:
        """Change how relevant an event is to the household.

        Call this when someone corrects the calendar — "that's not a family
        thing", "that one matters to all of us", "that's just work". The change
        is recorded as a human decision and survives future syncs.

        Args:
            event_id: The id from get_agenda.
            tier: T1 household, T2 personal, T3 busy.
        """
        return _safe(ctx, "set_tier", lambda: _set_tier(ctx, event_id, tier))

    @beta_tool
    def set_visibility(event_id: str, visibility: VisibilityName) -> str:
        """Hide an event from minors, or show it to everyone again.

        Adults only. Call this only on an explicit request to make something
        private or to un-hide it.

        Args:
            event_id: The id from get_agenda.
            visibility: "adults" to hide from minors, "all" to show everyone.
        """
        return _safe(ctx, "set_visibility", lambda: _set_visibility(ctx, event_id, visibility))

    @beta_tool
    def merge_events(event_ids: list[str]) -> str:
        """Mark several events as the same real-world event.

        Call this when the same thing appears more than once because it came
        from more than one calendar. Nothing is deleted and it can be undone.

        Args:
            event_ids: Two or more event ids that describe one event.
        """
        return _safe(ctx, "merge_events", lambda: _merge_events(ctx, event_ids))

    @beta_tool
    def unmerge(group_id: str) -> str:
        """Split a merge group back into separate events.

        Call this when events were joined that are actually different things.

        Args:
            group_id: The merge group id reported by get_agenda or merge_events.
        """
        return _safe(ctx, "unmerge", lambda: _unmerge(ctx, group_id))

    @beta_tool
    def list_routines() -> str:
        """List the household's routines: things done every so often, with when
        each was last done and when it is next due.

        Call this before logging that something was done, and to answer "how long
        since we…" or "when is … due". Each row has a routine id, lastDoneOn,
        dueOn, status (overdue, due_soon, ok, unscheduled, paused) and
        daysUntilDue. Results are already limited to what the speaker may see.
        """
        return _read(lambda: _list_routines(ctx))

    @beta_tool
    def log_done(routine_id: str, done_on: str | None = None, note: str | None = None) -> str:
        """Record that a routine was just done, and move its due date forward.

        Call this when someone says they did an occasional chore — "changed the
        cabin filter this morning", "got a haircut", "cleaned the gutters".
        Find the routine id with list_routines first.

        Args:
            routine_id: The id from list_routines.
            done_on: The day it was done, like 2026-08-06. Defaults to today;
                may not be in the future.
            note: Anything worth remembering, such as the part used or mileage.
        """
        return _safe(ctx, "log_done", lambda: _log_done(ctx, routine_id, done_on, note))

    @beta_tool
    def create_routine(
        name: str,
        interval_days: int | None = None,
        interval_stated_by_person: bool = False,
        interval_note: str | None = None,
        interval_confidence: float | None = None,
        last_done_on: str | None = None,
        due_on: str | None = None,
        anchor: AnchorName | None = None,
        category: str | None = None,
        owner_member_id: str | None = None,
        involves: list[str] | None = None,
        tier: TierName = "T2",
        visibility: VisibilityName | None = None,
    ) -> str:
        """Start tracking something the household does every so often.

        Call this when someone mentions an occasional task that list_routines
        does not already have. A known catalog of common items supplies the
        interval when the name matches; a number the person states beats the
        catalog; your own estimate is used only when both are missing. When you
        estimate, set interval_stated_by_person to false, give a one-line
        interval_note, and tell the person the cadence you assumed so they can
        correct it. Never present an estimate as fact.

        Args:
            name: What it is, in the household's own words.
            interval_days: How often, in days. Pass the person's number when
                they said one; otherwise your estimate, or leave unset.
            interval_stated_by_person: True only when the person said how often.
                False when interval_days is your estimate.
            interval_note: One line on where an estimated interval came from.
            interval_confidence: Your confidence in an estimate, 0 to 1.
            last_done_on: When it was last done, like 2026-08-06. Pass today
                when they are telling you they just did it.
            due_on: An explicit first due date, if the person named one.
            anchor: "elapsed" (due N days after last done) or "calendar" (same
                month and day every year, like holiday lights).
            category: Free text such as vehicle, home_maintenance, personal_care.
            owner_member_id: Whose routine it is. Defaults to the speaker.
            involves: Member ids of other people it involves.
            tier: T1 household, T2 personal (default), T3 busy.
            visibility: "adults" hides it from minors. Adults only.
        """
        return _safe(
            ctx,
            "create_routine",
            lambda: _create_routine(
                ctx,
                name,
                interval_days,
                interval_stated_by_person,
                interval_note,
                interval_confidence,
                last_done_on,
                due_on,
                anchor,
                category,
                owner_member_id,
                involves,
                tier,
                visibility,
            ),
        )

    @beta_tool
    def update_routine(
        routine_id: str,
        name: str | None = None,
        interval_days: int | None = None,
        due_on: str | None = None,
        paused: bool | None = None,
        involves: list[str] | None = None,
        tier: TierName | None = None,
        visibility: VisibilityName | None = None,
        category: str | None = None,
    ) -> str:
        """Change a routine. Pass only the fields that change.

        Use due_on to snooze ("remind me next month instead"), interval_days
        when the person corrects how often, and paused to stop reminders without
        deleting. Editing a routine that belongs to someone else needs their
        confirmation and will come back as pending; that is normal.

        Args:
            routine_id: The id from list_routines.
            name: New name.
            interval_days: New interval in days, as stated by the person.
            due_on: New due date, like 2026-09-01. Kept until it is next done.
            paused: True to stop reminders, false to resume.
            involves: Replacement list of member ids this routine involves.
            tier: T1 household, T2 personal, T3 busy.
            visibility: "adults" hides it from minors, "all" shows everyone.
                Adults only.
            category: New category.
        """
        return _safe(
            ctx,
            "update_routine",
            lambda: _update_routine(
                ctx,
                routine_id,
                name,
                interval_days,
                due_on,
                paused,
                involves,
                tier,
                visibility,
                category,
            ),
        )

    @beta_tool
    def delete_routine(routine_id: str) -> str:
        """Stop tracking a routine and remove its reminder from the calendar.

        This always needs confirmation and will come back as pending the first
        time; relay the question and wait. To stop reminders temporarily, use
        update_routine with paused instead.

        Args:
            routine_id: The id from list_routines.
        """
        return _safe(ctx, "delete_routine", lambda: _delete_routine(ctx, routine_id))

    @beta_tool
    def undo_completion(routine_id: str, completion_id: str) -> str:
        """Remove a completion that was logged by mistake ("I didn't actually
        do that"), rolling the due date back.

        Args:
            routine_id: The id from list_routines.
            completion_id: The id log_done returned.
        """
        return _safe(
            ctx, "undo_completion", lambda: _undo_completion(ctx, routine_id, completion_id)
        )

    # Stable order: the tool list renders ahead of the system prompt, so a
    # reshuffle here silently invalidates the whole cached prefix.
    return [
        confirm_event,
        create_event,
        create_routine,
        delete_event,
        delete_routine,
        find_conflicts,
        get_agenda,
        list_members,
        list_routines,
        log_done,
        merge_events,
        set_tier,
        set_visibility,
        undo_completion,
        unmerge,
        update_event,
        update_routine,
    ]
