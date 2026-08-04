"""The M1 HTTP API.

Two rules shape this module:

1. Visibility is decided at the query layer, never at serialization. `scoped_query` is
   the only constructor of `AgendaQuery` in the package and it cannot be called without
   an actor, so there is no route that can accidentally read past a minor's scope.
2. "Exists but you may not see it" is itself a disclosure, so an invisible or tombstoned
   event is a 404 on the single-item routes. 403 is reserved for mutations, where the
   actor already knows the event exists because they can see it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi import status as http_status

from airhead.agenda import build_agenda
from airhead.api.deps import Actor, Events, HouseholdId, Members, Tz, get_actor
from airhead.api.errors import (
    BadRequest,
    Forbidden,
    InvalidRequest,
    NotFound,
    install_error_handlers,
)
from airhead.api.schemas import (
    AgendaDayOut,
    AgendaResponse,
    BusyRowOut,
    ErrorResponse,
    EventCreate,
    EventPatch,
    EventRowOut,
    MemberOut,
    MembersResponse,
    RangeOut,
    fmt_local,
)
from airhead.domain import Event, EventSource, Member, SourceKind, Tier, TierSource, Visibility
from airhead.repo.base import AgendaQuery, EventRepo, MemberRepo

MAX_SPAN_DAYS = 31

# A window wide enough that `AgendaQuery.allows` reduces to household + tombstone +
# visibility, which is exactly the single-item check.
_ALL_TIME_START = datetime.min.replace(tzinfo=UTC)
_ALL_TIME_END = datetime.max.replace(tzinfo=UTC)


# --- logging -----------------------------------------------------------------


_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _RESERVED:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("airhead")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Lambda's root handler would re-emit this unformatted.
    return logger


log = _configure_logging()


# --- authorization -----------------------------------------------------------
#
# PRD §6.2: a minor cannot change another member's events, change visibility, delete
# events they did not create, or connect/disconnect sources. All server-side, because
# the kiosk is a shared screen and the agent is not a trust boundary.


def ensure_may_edit(actor: Member, event: Event) -> None:
    if not actor.is_adult and event.owner_member_id != actor.member_id:
        raise Forbidden("Minors may only change their own events.")


def ensure_may_delete(actor: Member, event: Event) -> None:
    if not actor.is_adult and (event.created_by or event.owner_member_id) != actor.member_id:
        raise Forbidden("Minors may only delete events they created.")


def ensure_may_set_visibility(actor: Member) -> None:
    if not actor.is_adult:
        raise Forbidden("Only adults may set event visibility.")


def ensure_may_mutate_sources(actor: Member) -> None:
    """Guard for the source connect/disconnect routes landing with the adapters in M3."""
    if not actor.is_adult:
        raise Forbidden("Only adults may change calendar sources.")


def scoped_query(
    actor: Member,
    household_id: str,
    *,
    start_utc: datetime = _ALL_TIME_START,
    end_utc: datetime = _ALL_TIME_END,
    min_tier: Tier = Tier.BUSY,
    member_ids: tuple[str, ...] | None = None,
) -> AgendaQuery:
    """The only place this package builds an `AgendaQuery`.

    `actor` is positional and required so a caller cannot forget the visibility scope
    and silently fall back to a default.
    """
    return AgendaQuery(
        household_id=household_id,
        start_utc=start_utc,
        end_utc=end_utc,
        visibility_scope=actor.visibility_scope(),
        member_ids=member_ids,
        min_tier=min_tier,
    )


# --- time --------------------------------------------------------------------


def to_utc(local: datetime, tz: str) -> datetime:
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


def to_local(instant: datetime, tz: str) -> datetime:
    return instant.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def span_utc(
    start_local: datetime, end_local: datetime, tz: str, *, all_day: bool
) -> tuple[datetime, datetime]:
    """All-day events are stored *floating* — midnight UTC, not midnight-in-tz.

    Converting an all-day date through a zone is the off-by-one-day bug the contract
    warns about, and `airhead.agenda` reads them back the same way.
    """
    if all_day:
        return start_local.replace(tzinfo=UTC), end_local.replace(tzinfo=UTC)
    return to_utc(start_local, tz), to_utc(end_local, tz)


def _local_pair(start_local: datetime, end_local: datetime, *, all_day: bool) -> tuple[str, str]:
    """Format the floating local pair. All-day ends are stored exclusive (next midnight)
    and shown inclusive, which is the off-by-one-day bug the contract calls out."""
    if all_day and end_local.time() == datetime.min.time() and end_local > start_local:
        end_local = end_local - timedelta(microseconds=1)
    return fmt_local(start_local, all_day=all_day), fmt_local(end_local, all_day=all_day)


# --- serialization -----------------------------------------------------------


def member_out(member: Member) -> MemberOut:
    return MemberOut(
        member_id=member.member_id,
        display_name=member.display_name,
        role=member.role,
        color=member.color,
    )


def event_row(event: Event, tz: str) -> EventRowOut:
    """A stored event rendered as the contract's `event` row.

    Local times use the *household* timezone, not the event's originating one — the
    kiosk positions rows on a single day grid and does no conversion of its own.
    """
    member_ids = [event.owner_member_id]
    member_ids += [m for m in event.involves if m != event.owner_member_id]
    if event.all_day:
        # Stored floating (midnight UTC), matching `airhead.agenda`. Running an all-day
        # date through a zone is exactly how it slides onto the wrong day.
        raw_start, raw_end = (
            event.start_utc.replace(tzinfo=None),
            event.end_utc.replace(tzinfo=None),
        )
    else:
        raw_start, raw_end = to_local(event.start_utc, tz), to_local(event.end_utc, tz)
    start_local, end_local = _local_pair(raw_start, raw_end, all_day=event.all_day)
    return EventRowOut(
        event_id=event.event_id,
        title=event.title,
        start_local=start_local,
        end_local=end_local,
        start_utc=event.start_utc,
        all_day=event.all_day,
        tier=event.tier,
        tier_source=event.tier_source,
        owner_member_id=event.owner_member_id,
        member_ids=member_ids,
        location=event.location,
        visibility=event.visibility,
        is_family=len(member_ids) > 1 and event.tier is Tier.HOUSEHOLD,
        occurrence_id=None,
    )


# --- app ---------------------------------------------------------------------

app = FastAPI(
    title="Airhead Calendar API",
    version="0.1.0",
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
install_error_handlers(app)


@app.middleware("http")
async def access_log(request: Request, call_next: Any) -> Response:
    started = time.monotonic()
    response = await call_next(request)
    # No titles, no locations, no query values - PRD §13, event text is household PII.
    log.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        },
    )
    return response


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Authenticated, but the roster itself is not visibility-restricted.
@app.get("/api/members", response_model=MembersResponse, dependencies=[Depends(get_actor)])
def list_members(members: Members, household_id: HouseholdId) -> MembersResponse:
    return MembersResponse(members=[member_out(m) for m in members.list(household_id)])


@app.get("/api/agenda", response_model=AgendaResponse)
def get_agenda(
    actor: Actor,
    events: Events,
    members: Members,
    household_id: HouseholdId,
    tz: Tz,
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    min_tier: Annotated[Tier, Query(alias="minTier")] = Tier.BUSY,
    member_id: Annotated[list[str] | None, Query(alias="memberId")] = None,
) -> AgendaResponse:
    start_date, end_date = start, end
    if end_date < start_date:
        raise BadRequest("`end` may not precede `start`.")
    if (end_date - start_date).days + 1 > MAX_SPAN_DAYS:
        raise BadRequest("Agenda span may not exceed 31 days.", code="range_too_large")

    roster = members.list(household_id)
    requested = tuple(member_id) if member_id else None
    if requested is not None:
        known = {m.member_id for m in roster}
        unknown = sorted(set(requested) - known)
        if unknown:
            raise InvalidRequest("Unknown memberId.")
        roster = [m for m in roster if m.member_id in requested]

    window_start = to_utc(datetime.combine(start_date, datetime.min.time()), tz)
    window_end = to_utc(datetime.combine(end_date + timedelta(days=1), datetime.min.time()), tz)
    query = scoped_query(
        actor,
        household_id,
        start_utc=window_start,
        end_utc=window_end,
        min_tier=min_tier,
        member_ids=requested,
    )

    stored: list[Event] = []
    cursor: str | None = None
    while True:
        page = events.list_range(query, cursor=cursor)
        stored.extend(page.events)
        cursor = page.cursor
        if cursor is None:
            break

    view = build_agenda(
        events=stored,
        members=roster,
        tz=tz,
        start=start_date,
        end=end_date,
        min_tier=min_tier,
    )

    days: list[AgendaDayOut] = []
    for day in view.days:
        rows: list[BusyRowOut | EventRowOut] = []
        for band in day.busy:
            band_start, band_end = _local_pair(band.start_local, band.end_local, all_day=False)
            rows.append(
                BusyRowOut(
                    member_id=band.member_id,
                    start_local=band_start,
                    end_local=band_end,
                    count=band.count,
                    event_ids=list(band.event_ids),
                )
            )
        for row in day.events:
            row_start, row_end = _local_pair(row.start_local, row.end_local, all_day=row.all_day)
            rows.append(
                EventRowOut(
                    event_id=row.event_id,
                    title=row.title,
                    start_local=row_start,
                    end_local=row_end,
                    start_utc=row.start_utc,
                    all_day=row.all_day,
                    tier=row.tier,
                    tier_source=row.tier_source,
                    owner_member_id=row.owner_member_id,
                    member_ids=list(row.member_ids),
                    location=row.location,
                    visibility=row.visibility,
                    is_family=row.is_family,
                    occurrence_id=row.occurrence_id,
                )
            )
        days.append(AgendaDayOut(date=day.date, rows=rows))

    return AgendaResponse(
        range=RangeOut(start=view.start, end=view.end, tz=view.tz),
        members=[member_out(m) for m in view.members],
        days=days,
    )


def _load_visible(events: EventRepo, actor: Member, household_id: str, event_id: str) -> Event:
    stored = events.get(household_id, event_id)
    # One 404 for "no such event", "tombstoned" and "adults-only". A distinguishable
    # 403 here would let a minor enumerate the existence of adult events.
    if stored is None or not scoped_query(actor, household_id).allows(stored):
        raise NotFound("Event not found.")
    return stored


def _validate_involves(members: MemberRepo, household_id: str, involves: list[str]) -> list[str]:
    known = {m.member_id for m in members.list(household_id)}
    if set(involves) - known:
        raise InvalidRequest("Unknown member id in `involves`.")
    return list(dict.fromkeys(involves))


@app.post("/api/events", response_model=EventRowOut, status_code=http_status.HTTP_201_CREATED)
def create_event(
    body: EventCreate,
    actor: Actor,
    events: Events,
    members: Members,
    household_id: HouseholdId,
    tz: Tz,
) -> EventRowOut:
    owner = body.owner_member_id or actor.member_id
    # TODO(open-question): may a minor create an event *for* another member (e.g. Riley
    # putting "pick me up at 6" on a parent)? Pending a decision with the household
    # owner; until then a minor may only create their own events.
    if not actor.is_adult and owner != actor.member_id:
        raise Forbidden("Minors may only create their own events.")
    if body.visibility is not None:
        ensure_may_set_visibility(actor)

    event_tz = body.tz or tz
    if body.date is not None:
        start_local = datetime.combine(body.date, datetime.min.time())
        end_local = start_local + timedelta(days=1)
        all_day = True
    else:
        # EventCreate's model validator guarantees both are present here.
        start_local = body.start_local or datetime.min
        end_local = body.end_local or datetime.min
        all_day = False

    involves = _validate_involves(members, household_id, body.involves)
    if owner not in {m.member_id for m in members.list(household_id)}:
        raise InvalidRequest("Unknown ownerMemberId.")

    start_utc, end_utc = span_utc(start_local, end_local, event_tz, all_day=all_day)
    stored = events.put(
        Event(
            event_id=f"evt_{uuid.uuid4().hex}",
            household_id=household_id,
            title=body.title.strip(),
            start_utc=start_utc,
            end_utc=end_utc,
            tz=event_tz,
            owner_member_id=owner,
            source=EventSource(kind=SourceKind.NATIVE),
            all_day=all_day,
            rrule=body.rrule,
            involves=involves,
            location=body.location,
            tier=body.tier if body.tier is not None else Tier.PERSONAL,
            # A tier a person typed is a human tier, same as through PATCH — otherwise
            # the first sync would "correct" a deliberate choice back to auto.
            tier_source=TierSource.HUMAN if body.tier is not None else TierSource.AUTO,
            visibility=body.visibility or Visibility.ALL,
            created_by=actor.member_id,
        )
    )
    log.info("event_created", extra={"event_id": stored.event_id, "actor": actor.member_id})
    return event_row(stored, tz)


@app.get("/api/events/{event_id}", response_model=EventRowOut)
def get_event(
    event_id: str, actor: Actor, events: Events, household_id: HouseholdId, tz: Tz
) -> EventRowOut:
    return event_row(_load_visible(events, actor, household_id, event_id), tz)


@app.patch("/api/events/{event_id}", response_model=EventRowOut)
def patch_event(
    event_id: str,
    body: EventPatch,
    actor: Actor,
    events: Events,
    members: Members,
    household_id: HouseholdId,
    tz: Tz,
) -> EventRowOut:
    stored = _load_visible(events, actor, household_id, event_id)
    ensure_may_edit(actor, stored)
    fields = body.model_fields_set

    if "visibility" in fields:
        ensure_may_set_visibility(actor)
        if body.visibility is not None:
            stored.visibility = body.visibility

    if "owner_member_id" in fields and body.owner_member_id:
        if not actor.is_adult:
            raise Forbidden("Minors may not reassign an event's owner.")
        if body.owner_member_id not in {m.member_id for m in members.list(household_id)}:
            raise InvalidRequest("Unknown ownerMemberId.")
        stored.owner_member_id = body.owner_member_id

    if "title" in fields and body.title:
        stored.title = body.title.strip()
    if "location" in fields:
        stored.location = body.location
    if "rrule" in fields:
        stored.rrule = body.rrule
    if "involves" in fields and body.involves is not None:
        stored.involves = _validate_involves(members, household_id, body.involves)

    if "tier" in fields and body.tier is not None:
        # PRD §6.1 rule 5. This stamp is what makes a correction on the kitchen screen
        # survive the next poll; `apply_remote_update` refuses to overwrite it.
        stored.tier = body.tier
        stored.tier_source = TierSource.HUMAN

    if fields & {"start_local", "end_local", "date", "all_day", "tz"}:
        event_tz = body.tz if ("tz" in fields and body.tz) else stored.tz
        if "date" in fields and body.date is not None:
            start_local = datetime.combine(body.date, datetime.min.time())
            end_local = start_local + timedelta(days=1)
            stored.all_day = True
        elif stored.all_day:
            start_local = body.start_local or stored.start_utc.replace(tzinfo=None)
            end_local = body.end_local or stored.end_utc.replace(tzinfo=None)
            if body.all_day is not None:
                stored.all_day = body.all_day
        else:
            start_local = body.start_local or to_local(stored.start_utc, event_tz)
            end_local = body.end_local or to_local(stored.end_utc, event_tz)
            if body.all_day is not None:
                stored.all_day = body.all_day
        if end_local < start_local:
            raise InvalidRequest("endLocal precedes startLocal.")
        stored.tz = event_tz
        stored.start_utc, stored.end_utc = span_utc(
            start_local, end_local, event_tz, all_day=stored.all_day
        )

    saved = events.put(stored)
    log.info(
        "event_patched",
        extra={
            "event_id": saved.event_id,
            "actor": actor.member_id,
            "fields": sorted(fields),
        },
    )
    return event_row(saved, tz)


@app.delete("/api/events/{event_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_event(
    event_id: str, actor: Actor, events: Events, household_id: HouseholdId
) -> Response:
    stored = _load_visible(events, actor, household_id, event_id)
    ensure_may_delete(actor, stored)
    events.delete(household_id, event_id, at=datetime.now(UTC))
    log.info("event_deleted", extra={"event_id": event_id, "actor": actor.member_id})
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
