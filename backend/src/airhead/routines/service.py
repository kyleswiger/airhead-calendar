"""Projection, completion and interval resolution for routines.

Pure functions over domain objects plus the two repositories; no HTTP, no
model. The HTTP routes and the agent tools are thin wrappers over this module
so both channels behave identically - the same rule that keeps `tools.py`
and `app.py` in step for events.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from airhead.domain import (
    Anchor,
    Completion,
    Event,
    EventSource,
    IntervalSource,
    Member,
    Routine,
    SourceKind,
    Tier,
    TierSource,
    Visibility,
)
from airhead.repo.base import EventRepo, RoutineRepo
from airhead.routines import catalog

Status = Literal["paused", "unscheduled", "overdue", "due_soon", "ok"]

DUE_SOON_DAYS = 7
# Three completions = two gaps; fewer than that is an anecdote, not a cadence.
MIN_COMPLETIONS_FOR_OBSERVED = 3
_STATUS_RANK: dict[str, int] = {"overdue": 0, "due_soon": 1, "ok": 2, "unscheduled": 3, "paused": 4}


# --- interval resolution -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """Where an interval came from and why. `interval_days` None = still unknown."""

    interval_days: int | None
    source: IntervalSource | None
    note: str | None = None
    confidence: float | None = None
    catalog_key: str | None = None
    category: str | None = None
    anchor: Anchor | None = None
    range_days: tuple[int, int] | None = None
    usage_dependent: bool = False


UNKNOWN = Resolution(interval_days=None, source=None)


def observed_interval(completions: Sequence[Completion]) -> int | None:
    """Median gap between completions, once there are enough to trust."""
    days = sorted({c.done_on for c in completions})
    if len(days) < MIN_COMPLETIONS_FOR_OBSERVED:
        return None
    gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False) if (b - a).days > 0]
    if len(gaps) < MIN_COMPLETIONS_FOR_OBSERVED - 1:
        return None
    return max(1, round(statistics.median(gaps)))


def resolve_interval(
    name: str,
    *,
    stated: int | None = None,
    completions: Sequence[Completion] = (),
) -> Resolution:
    """Ground rule 1 without the model step: human > observed > catalog > unknown.

    The model step belongs to the caller - the agent estimates in-loop, and the
    HTTP API exposes it as `POST /api/routines/estimate` - because a model call
    is a cost and a latency the deterministic path must never pay.
    """
    entry = catalog.lookup(name)
    if stated is not None:
        return Resolution(
            interval_days=stated,
            source=IntervalSource.HUMAN,
            catalog_key=entry.key if entry else None,
            category=entry.category if entry else None,
            anchor=entry.anchor if entry else None,
        )
    observed = observed_interval(completions)
    if observed is not None:
        return Resolution(
            interval_days=observed,
            source=IntervalSource.OBSERVED,
            note=f"Median of your last {len(completions)} completions.",
            catalog_key=entry.key if entry else None,
            category=entry.category if entry else None,
            anchor=entry.anchor if entry else None,
        )
    if entry is not None:
        return Resolution(
            interval_days=entry.interval_days,
            source=IntervalSource.CATALOG,
            note=entry.notes,
            confidence=None,
            catalog_key=entry.key,
            category=entry.category,
            anchor=entry.anchor,
            range_days=(
                (entry.interval_min_days, entry.interval_max_days)
                if entry.interval_min_days and entry.interval_max_days
                else None
            ),
            usage_dependent=entry.interval_miles is not None,
        )
    return UNKNOWN


def apply_interval(routine: Routine, resolution: Resolution) -> Routine:
    """Write a resolution onto a routine, never over a human-stated interval.

    Same invariant as `tier_source: human` on events: a person's correction
    must survive every later recomputation or every correction evaporates.
    """
    if resolution.interval_days is None or resolution.source is None:
        return routine
    if routine.interval_is_human and resolution.source is not IntervalSource.HUMAN:
        return routine
    routine.interval_days = resolution.interval_days
    routine.interval_source = resolution.source
    routine.interval_note = resolution.note
    routine.interval_confidence = resolution.confidence
    if resolution.catalog_key and not routine.catalog_key:
        routine.catalog_key = resolution.catalog_key
    if resolution.category and routine.category == "other":
        routine.category = resolution.category
    return routine


# --- projection --------------------------------------------------------------


def next_due(routine: Routine, *, today: date) -> date | None:
    """When the routine is next due, from its last completion and interval.

    ELAPSED: last + interval. CALENDAR: the same month/day next year - so
    holiday lights done on Nov 28 come due on Nov 28 regardless of interval
    arithmetic. Feb 29 falls to Feb 28. `today` is only used when there is no
    last completion at all: an interval with no history is due "now", which is
    the honest answer for something the household says it does but never has.
    """
    if routine.interval_days is None:
        return None
    if routine.last_done_on is None:
        return today
    if routine.anchor is Anchor.CALENDAR:
        return _anniversary(routine.last_done_on)
    return routine.last_done_on + timedelta(days=routine.interval_days)


def _anniversary(done: date) -> date:
    year = done.year + 1
    try:
        return done.replace(year=year)
    except ValueError:  # Feb 29
        return done.replace(year=year, day=28)


def status(routine: Routine, *, today: date) -> Status:
    if routine.paused:
        return "paused"
    if routine.due_on is None:
        return "unscheduled"
    delta = (routine.due_on - today).days
    if delta < 0:
        return "overdue"
    if delta <= DUE_SOON_DAYS:
        return "due_soon"
    return "ok"


def days_until_due(routine: Routine, *, today: date) -> int | None:
    return None if routine.due_on is None else (routine.due_on - today).days


def sort_key(routine: Routine, *, today: date) -> tuple[int, int, str]:
    """Contract order: overdue (most first), due_soon, ok (soonest), unscheduled, paused."""
    s = status(routine, today=today)
    due = days_until_due(routine, today=today)
    return (_STATUS_RANK[s], due if due is not None else 0, routine.routine_id)


def visible_to(actor: Member, routine: Routine) -> bool:
    """The same rule `AgendaQuery.allows` applies to events, at the query layer."""
    if routine.is_deleted:
        return False
    return not (
        actor.visibility_scope() is Visibility.ALL and routine.visibility is Visibility.ADULTS
    )


# --- the due event -----------------------------------------------------------


def _due_title(routine: Routine) -> str:
    return routine.name


def _day_span(day: date) -> tuple[datetime, datetime]:
    # Floating, midnight UTC - the all-day convention everywhere else in the tree.
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return start, start + timedelta(days=1)


def reproject(
    routine: Routine,
    routines: RoutineRepo,
    events: EventRepo,
    *,
    today: date,
    tz: str,
) -> Routine:
    """Make the calendar agree with the routine. Idempotent; writes only on drift.

    - paused / unscheduled / deleted: no due event (soft-deleted if one exists).
    - otherwise: exactly one all-day event on max(due_on, today). An overdue
      routine therefore sits on *today* every day until it is done, snoozed or
      paused - it never scrolls off the kitchen screen (ground rule 3).
    """
    wants_event = not routine.paused and not routine.is_deleted and routine.due_on is not None
    existing = (
        events.get(routine.household_id, routine.due_event_id) if routine.due_event_id else None
    )
    if existing is not None and existing.is_deleted:
        existing = None

    if not wants_event:
        if existing is not None:
            events.delete(routine.household_id, existing.event_id, at=datetime.now(UTC))
        if routine.due_event_id is not None:
            routine.due_event_id = None
            routine = routines.put(routine)
        return routine

    assert routine.due_on is not None
    shown_on = max(routine.due_on, today)
    start_utc, end_utc = _day_span(shown_on)
    desired = Event(
        event_id=existing.event_id if existing else f"evt_{uuid.uuid4().hex}",
        household_id=routine.household_id,
        title=_due_title(routine),
        start_utc=start_utc,
        end_utc=end_utc,
        tz=tz,
        owner_member_id=routine.owner_member_id,
        source=EventSource(kind=SourceKind.NATIVE),
        all_day=True,
        involves=list(routine.involves),
        tier=routine.tier,
        tier_source=TierSource.HUMAN,
        visibility=routine.visibility,
        routine_id=routine.routine_id,
        created_by=routine.created_by,
    )
    if existing is not None and _same_projection(existing, desired):
        return routine
    stored = events.put(desired)
    if routine.due_event_id != stored.event_id:
        routine.due_event_id = stored.event_id
        routine = routines.put(routine)
    return routine


def _same_projection(a: Event, b: Event) -> bool:
    return (
        a.title == b.title
        and a.start_utc == b.start_utc
        and a.end_utc == b.end_utc
        and a.all_day
        and a.owner_member_id == b.owner_member_id
        and list(a.involves) == list(b.involves)
        and a.tier is b.tier
        and a.visibility is b.visibility
        and a.routine_id == b.routine_id
    )


def reproject_all(
    routines: RoutineRepo, events: EventRepo, *, household_id: str, today: date, tz: str
) -> list[Routine]:
    return [reproject(r, routines, events, today=today, tz=tz) for r in routines.list(household_id)]


# --- completions -------------------------------------------------------------


def _recompute(routine: Routine, completions: Sequence[Completion], *, today: date) -> Routine:
    """Refresh the derived fields from the completion history."""
    routine.last_done_on = max((c.done_on for c in completions), default=None)
    apply_interval(
        routine,
        resolve_interval(routine.name, completions=completions)
        if not routine.interval_is_human
        else UNKNOWN,
    )
    routine.due_on = next_due(routine, today=today)
    return routine


class FutureCompletion(ValueError):
    """`done_on` is after today. A completion is a fact, and facts are not in the future."""


def complete(
    routine: Routine,
    routines: RoutineRepo,
    events: EventRepo,
    *,
    by: Member,
    done_on: date | None,
    note: str | None,
    today: date,
    tz: str,
) -> tuple[Routine, Completion]:
    """Record "we did it" and move the due date forward. Clears any snooze."""
    when = done_on or today
    if when > today:
        raise FutureCompletion("A routine cannot be completed in the future.")
    completion = routines.add_completion(
        Completion(
            completion_id=f"cmp_{uuid.uuid4().hex}",
            household_id=routine.household_id,
            routine_id=routine.routine_id,
            done_on=when,
            by_member_id=by.member_id,
            note=(note or "").strip() or None,
        )
    )
    history = routines.list_completions(routine.household_id, routine.routine_id)
    routine = routines.put(_recompute(routine, history, today=today))
    return reproject(routine, routines, events, today=today, tz=tz), completion


def undo_completion(
    routine: Routine,
    routines: RoutineRepo,
    events: EventRepo,
    *,
    completion_id: str,
    today: date,
    tz: str,
) -> Routine | None:
    """Delete one completion and roll the derived state back.

    Returns the recomputed routine, or None when the completion did not exist -
    the same shape `complete` returns, so callers never need a second `get`.
    """
    if not routines.delete_completion(routine.household_id, routine.routine_id, completion_id):
        return None
    history = routines.list_completions(routine.household_id, routine.routine_id)
    routine = routines.put(_recompute(routine, history, today=today))
    return reproject(routine, routines, events, today=today, tz=tz)


def create(
    routines: RoutineRepo,
    events: EventRepo,
    *,
    household_id: str,
    name: str,
    owner: Member,
    created_by: Member,
    today: date,
    tz: str,
    stated_interval: int | None = None,
    estimate: Resolution | None = None,
    category: str | None = None,
    anchor: Anchor | None = None,
    involves: Sequence[str] = (),
    tier: Tier = Tier.PERSONAL,
    visibility: Visibility = Visibility.ALL,
    last_done_on: date | None = None,
    due_on: date | None = None,
    note: str | None = None,
) -> Routine:
    """Create a routine, resolve its interval, seed a first completion, project it.

    `estimate` is a model resolution the caller already obtained; it is applied
    only when the deterministic ladder came up empty, so the catalog beats the
    model and the person beats both.
    """
    # Before the first write: a future completion is rejected inside `complete`,
    # and rejecting it there would leave an orphan routine row behind.
    if last_done_on is not None and last_done_on > today:
        raise FutureCompletion("A routine cannot be completed in the future.")
    resolution = resolve_interval(name, stated=stated_interval)
    if resolution.interval_days is None and estimate is not None:
        resolution = estimate
    routine = Routine(
        routine_id=f"rtn_{uuid.uuid4().hex}",
        household_id=household_id,
        name=name.strip(),
        owner_member_id=owner.member_id,
        category=category or resolution.category or "other",
        anchor=anchor or resolution.anchor or Anchor.ELAPSED,
        involves=list(dict.fromkeys(involves)),
        tier=tier,
        visibility=visibility,
        catalog_key=resolution.catalog_key,
        created_by=created_by.member_id,
    )
    apply_interval(routine, resolution)
    routine = routines.put(routine)
    if last_done_on is not None:
        routine, _ = complete(
            routine,
            routines,
            events,
            by=created_by,
            done_on=last_done_on,
            note=note,
            today=today,
            tz=tz,
        )
    if due_on is not None:
        routine.due_on = due_on
        routine = routines.put(routine)
    elif routine.due_on is None:
        routine.due_on = next_due(routine, today=today)
        routine = routines.put(routine)
    return reproject(routine, routines, events, today=today, tz=tz)


def remove(
    routine: Routine, routines: RoutineRepo, events: EventRepo, *, at: datetime | None = None
) -> Routine | None:
    """Soft-delete the routine and its due event together."""
    when = at or datetime.now(UTC)
    if routine.due_event_id:
        events.delete(routine.household_id, routine.due_event_id, at=when)
    return routines.delete(routine.household_id, routine.routine_id, at=when)
