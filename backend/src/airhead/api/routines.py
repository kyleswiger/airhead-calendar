"""The routines HTTP surface (docs/ROUTINES-CONTRACT.md).

Thin wrappers over `airhead.routines.service`: every projection, interval and
completion rule lives there so the agent tools and these routes cannot drift apart.
What this module owns is the same two things `app.py` owns for events:

1. Visibility at the query layer. `service.visible_to` is the one filter, and an
   invisible or tombstoned routine is a 404 on the single-item routes - never a 403,
   because "exists but you may not see it" is itself a disclosure.
2. Authorization. Adults may do anything. A minor may create routines they own,
   complete anything they can see, and change or delete only what they own. Routines
   are commitments, so there is no proposal flow: a minor creating one for someone
   else is a 403, full stop.

"Today" is the household-local date, computed in exactly one place (`household_today`)
so the tests can pin the clock. Logging carries ids and counts only - routine names
are household text (PRD §13).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Response
from fastapi import status as http_status

from airhead.api.authz import ensure_may_set_visibility, validate_involves
from airhead.api.deps import Actor, Estimator, Events, HouseholdId, Members, Routines, Tz
from airhead.api.errors import BadRequest, Forbidden, InvalidRequest, NotFound
from airhead.api.schemas import (
    CompleteBody,
    CompletionOut,
    CompletionsResponse,
    EstimateRequest,
    EstimateResponse,
    RoutineCreate,
    RoutineOut,
    RoutinePatch,
    RoutinesResponse,
)
from airhead.domain import IntervalSource, Member, Routine, Tier, Visibility
from airhead.repo.base import MemberRepo, RoutineRepo
from airhead.routines import service

log = logging.getLogger("airhead.api.routines")

router = APIRouter(prefix="/api/routines", tags=["routines"])


# --- time --------------------------------------------------------------------


def household_today(tz: str) -> date:
    """The one clock. A routine is done "on a day" in the household's zone."""
    return datetime.now(ZoneInfo(tz)).date()


# --- authorization -----------------------------------------------------------


def ensure_may_edit_routine(actor: Member, routine: Routine) -> None:
    if not actor.is_adult and routine.owner_member_id != actor.member_id:
        raise Forbidden("Minors may only change their own routines.")


def _load_visible(
    routines: RoutineRepo, actor: Member, household_id: str, routine_id: str
) -> Routine:
    stored = routines.get(household_id, routine_id)
    # One 404 for "no such routine", "tombstoned" and "adults-only", as for events.
    if stored is None or not service.visible_to(actor, stored):
        raise NotFound("Routine not found.")
    return stored


def _resolve_owner(members: MemberRepo, household_id: str, owner_id: str) -> Member:
    owner = members.get(household_id, owner_id)
    if owner is None:
        raise InvalidRequest("Unknown ownerMemberId.")
    return owner


# --- serialization -----------------------------------------------------------


def routine_out(routine: Routine, *, completion_count: int, today: date) -> RoutineOut:
    member_ids = [routine.owner_member_id]
    member_ids += [m for m in routine.involves if m != routine.owner_member_id]
    return RoutineOut(
        routine_id=routine.routine_id,
        name=routine.name,
        category=routine.category,
        owner_member_id=routine.owner_member_id,
        member_ids=member_ids,
        tier=routine.tier,
        visibility=routine.visibility,
        interval_days=routine.interval_days,
        interval_source=routine.interval_source if routine.interval_days is not None else None,
        interval_note=routine.interval_note,
        interval_confidence=routine.interval_confidence,
        anchor=routine.anchor,
        catalog_key=routine.catalog_key,
        last_done_on=routine.last_done_on,
        due_on=routine.due_on,
        due_event_id=routine.due_event_id,
        status=service.status(routine, today=today),
        days_until_due=service.days_until_due(routine, today=today),
        completion_count=completion_count,
        paused=routine.paused,
    )


def _out(routines: RoutineRepo, routine: Routine, *, today: date) -> RoutineOut:
    count = len(routines.list_completions(routine.household_id, routine.routine_id))
    return routine_out(routine, completion_count=count, today=today)


def _estimate_out(resolution: service.Resolution) -> EstimateResponse:
    source = resolution.source
    return EstimateResponse(
        interval_days=resolution.interval_days,
        range_days=resolution.range_days,
        confidence=resolution.confidence,
        source=(
            source.value
            if source in (IntervalSource.CATALOG, IntervalSource.ESTIMATED)
            and resolution.interval_days is not None
            else None
        ),
        rationale=resolution.note,
        usage_dependent=resolution.usage_dependent,
        follow_up_question=getattr(resolution, "follow_up_question", None),
        catalog_key=resolution.catalog_key,
        category=resolution.category,
        anchor=resolution.anchor,
    )


# --- routes ------------------------------------------------------------------


@router.get("", response_model=RoutinesResponse)
def list_routines(
    actor: Actor, routines: Routines, events: Events, household_id: HouseholdId, tz: Tz
) -> RoutinesResponse:
    today = household_today(tz)
    projected = service.reproject_all(
        routines, events, household_id=household_id, today=today, tz=tz
    )
    visible = [r for r in projected if service.visible_to(actor, r)]
    visible.sort(key=lambda r: service.sort_key(r, today=today))
    return RoutinesResponse(routines=[_out(routines, r, today=today) for r in visible])


@router.post("", response_model=RoutineOut, status_code=http_status.HTTP_201_CREATED)
def create_routine(
    body: RoutineCreate,
    actor: Actor,
    routines: Routines,
    events: Events,
    members: Members,
    household_id: HouseholdId,
    tz: Tz,
) -> RoutineOut:
    owner_id = body.owner_member_id or actor.member_id
    if not actor.is_adult and owner_id != actor.member_id:
        raise Forbidden("Minors may only create routines they own.")
    if body.visibility is not None:
        ensure_may_set_visibility(actor)
    owner = _resolve_owner(members, household_id, owner_id)
    involves = validate_involves(members, household_id, body.involves)

    today = household_today(tz)
    # Checked here rather than left to `service.complete`: `service.create` stores the
    # routine before seeding the completion, and a 400 must not leave an orphan behind.
    if body.last_done_on is not None and body.last_done_on > today:
        raise BadRequest("lastDoneOn may not be in the future.", code="future_completion")

    # An `intervalDays` is the person's number unless the client says it is relaying a
    # model estimate it already fetched from /estimate - which the catalog still beats.
    stated: int | None = body.interval_days
    estimate: service.Resolution | None = None
    if body.interval_days is not None and body.interval_source is IntervalSource.ESTIMATED:
        stated = None
        estimate = service.Resolution(
            interval_days=body.interval_days,
            source=IntervalSource.ESTIMATED,
            note=body.interval_note,
            confidence=body.interval_confidence,
            anchor=body.anchor,
        )

    routine = service.create(
        routines,
        events,
        household_id=household_id,
        name=body.name,
        owner=owner,
        created_by=actor,
        today=today,
        tz=tz,
        stated_interval=stated,
        estimate=estimate,
        category=body.category.strip() if body.category else None,
        anchor=body.anchor,
        involves=involves,
        tier=body.tier if body.tier is not None else Tier.PERSONAL,
        visibility=body.visibility or Visibility.ALL,
        last_done_on=body.last_done_on,
        due_on=body.due_on,
    )
    if stated is not None and body.interval_note is not None:
        # A person's own "why" beside a person's own number.
        routine.interval_note = body.interval_note
        routine = routines.put(routine)
    log.info(
        "routine_created",
        extra={
            "routine_id": routine.routine_id,
            "actor": actor.member_id,
            "interval_source": routine.interval_source.value if routine.interval_days else None,
            "status": service.status(routine, today=today),
        },
    )
    return _out(routines, routine, today=today)


@router.post("/estimate", response_model=EstimateResponse)
def estimate_routine(body: EstimateRequest, actor: Actor, estimator: Estimator) -> EstimateResponse:
    # Deterministic first: a catalog match never costs a model call.
    resolution = service.resolve_interval(body.name)
    from_catalog = resolution.interval_days is not None
    if not from_catalog:
        resolution = estimator(body.name, body.context)
    log.info(
        "routine_estimate",
        extra={
            "actor": actor.member_id,
            "source": resolution.source.value if resolution.source else None,
            "catalog_hit": from_catalog,
        },
    )
    return _estimate_out(resolution)


@router.get("/{routine_id}", response_model=RoutineOut)
def get_routine(
    routine_id: str, actor: Actor, routines: Routines, household_id: HouseholdId, tz: Tz
) -> RoutineOut:
    stored = _load_visible(routines, actor, household_id, routine_id)
    return _out(routines, stored, today=household_today(tz))


@router.patch("/{routine_id}", response_model=RoutineOut)
def patch_routine(
    routine_id: str,
    body: RoutinePatch,
    actor: Actor,
    routines: Routines,
    events: Events,
    members: Members,
    household_id: HouseholdId,
    tz: Tz,
) -> RoutineOut:
    stored = _load_visible(routines, actor, household_id, routine_id)
    ensure_may_edit_routine(actor, stored)
    fields = body.model_fields_set
    today = household_today(tz)

    if "visibility" in fields:
        ensure_may_set_visibility(actor)
        if body.visibility is not None:
            stored.visibility = body.visibility
    if "name" in fields and body.name:
        stored.name = body.name.strip()
    if "category" in fields and body.category:
        stored.category = body.category.strip()
    if "involves" in fields and body.involves is not None:
        stored.involves = validate_involves(members, household_id, body.involves)
    if "tier" in fields and body.tier is not None:
        stored.tier = body.tier
    if "paused" in fields and body.paused is not None:
        stored.paused = body.paused
    if "anchor" in fields and body.anchor is not None:
        stored.anchor = body.anchor
    if "interval_note" in fields:
        stored.interval_note = body.interval_note

    if "interval_days" in fields:
        if body.interval_days is None:
            # "I don't know how often" - back to unscheduled, and the next completion
            # is free to resolve it again from the catalog or history.
            stored.interval_days = None
            stored.interval_confidence = None
        else:
            # A number a person typed is sticky (ground rule 1): `apply_interval`
            # refuses to overwrite it from here on.
            stored.interval_days = body.interval_days
            stored.interval_source = IntervalSource.HUMAN
            stored.interval_confidence = None
            if "interval_note" not in fields:
                stored.interval_note = None

    if "due_on" in fields and body.due_on is not None:
        stored.due_on = body.due_on  # a snooze, kept until the next completion
    elif "due_on" in fields or fields & {"interval_days", "anchor"}:
        stored.due_on = service.next_due(stored, today=today)

    saved = routines.put(stored)
    saved = service.reproject(saved, routines, events, today=today, tz=tz)
    log.info(
        "routine_patched",
        extra={"routine_id": saved.routine_id, "actor": actor.member_id, "fields": sorted(fields)},
    )
    return _out(routines, saved, today=today)


@router.delete("/{routine_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_routine(
    routine_id: str, actor: Actor, routines: Routines, events: Events, household_id: HouseholdId
) -> Response:
    stored = _load_visible(routines, actor, household_id, routine_id)
    ensure_may_edit_routine(actor, stored)
    service.remove(stored, routines, events)
    log.info("routine_deleted", extra={"routine_id": routine_id, "actor": actor.member_id})
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post("/{routine_id}/complete", response_model=RoutineOut)
def complete_routine(
    routine_id: str,
    body: CompleteBody,
    actor: Actor,
    routines: Routines,
    events: Events,
    household_id: HouseholdId,
    tz: Tz,
) -> RoutineOut:
    # Any member who can see it may complete it: "done" is a fact, not an edit.
    stored = _load_visible(routines, actor, household_id, routine_id)
    today = household_today(tz)
    try:
        routine, completion = service.complete(
            stored,
            routines,
            events,
            by=actor,
            done_on=body.done_on,
            note=body.note,
            today=today,
            tz=tz,
        )
    except service.FutureCompletion as exc:
        raise BadRequest("doneOn may not be in the future.", code="future_completion") from exc
    log.info(
        "routine_completed",
        extra={
            "routine_id": routine.routine_id,
            "completion_id": completion.completion_id,
            "actor": actor.member_id,
            "interval_source": routine.interval_source.value if routine.interval_days else None,
        },
    )
    return _out(routines, routine, today=today)


@router.get("/{routine_id}/completions", response_model=CompletionsResponse)
def list_completions(
    routine_id: str, actor: Actor, routines: Routines, household_id: HouseholdId
) -> CompletionsResponse:
    stored = _load_visible(routines, actor, household_id, routine_id)
    history = routines.list_completions(household_id, stored.routine_id)
    return CompletionsResponse(
        completions=[
            CompletionOut(
                completion_id=c.completion_id,
                done_on=c.done_on,
                by_member_id=c.by_member_id,
                note=c.note,
            )
            for c in history
        ]
    )


@router.delete(
    "/{routine_id}/completions/{completion_id}", status_code=http_status.HTTP_204_NO_CONTENT
)
def undo_completion(
    routine_id: str,
    completion_id: str,
    actor: Actor,
    routines: Routines,
    events: Events,
    household_id: HouseholdId,
    tz: Tz,
) -> Response:
    stored = _load_visible(routines, actor, household_id, routine_id)
    history = routines.list_completions(household_id, stored.routine_id)
    completion = next((c for c in history if c.completion_id == completion_id), None)
    if completion is None:
        raise NotFound("Completion not found.")
    # A minor may take back their own tap, or any tap on a routine they own.
    if (
        not actor.is_adult
        and stored.owner_member_id != actor.member_id
        and completion.by_member_id != actor.member_id
    ):
        raise Forbidden("Minors may only undo their own completions.")
    service.undo_completion(
        stored, routines, events, completion_id=completion_id, today=household_today(tz), tz=tz
    )
    log.info(
        "routine_completion_undone",
        extra={
            "routine_id": routine_id,
            "completion_id": completion_id,
            "actor": actor.member_id,
        },
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
