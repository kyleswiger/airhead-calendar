"""Agenda assembly - the tier logic the kitchen display renders.

Pure: no I/O, no clock. The window, the roster and the household zone are all
injected, so the same call replays identically in a test, in the API Lambda and
in the agent's prompt builder.

The T3 collapse here is structural, not cosmetic (M1 contract, PRD R6). Every
T3 event on a day lands in exactly one band per member it constrains, with a
truthful count. There is no path through this module on which a T3 event is
dropped instead of collapsed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from airhead.domain import Event, Member, Tier, TierSource, Visibility
from airhead.recurrence import expand, occurrence_id, to_floating
from airhead.repo.base import TIER_ORDER

_MIDNIGHT = time(0, 0)


@dataclass(frozen=True, slots=True)
class EventRow:
    event_id: str
    title: str
    start_local: datetime  # naive/floating, already in household tz
    end_local: datetime
    start_utc: datetime
    all_day: bool
    tier: Tier
    tier_source: TierSource
    owner_member_id: str
    member_ids: tuple[str, ...]
    location: str | None
    visibility: Visibility
    is_family: bool
    occurrence_id: str | None


@dataclass(frozen=True, slots=True)
class BusyRow:
    member_id: str
    start_local: datetime
    end_local: datetime
    count: int
    event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgendaDay:
    date: date
    busy: tuple[BusyRow, ...]
    events: tuple[EventRow, ...]


@dataclass(frozen=True, slots=True)
class AgendaView:
    start: date
    end: date
    tz: str
    members: tuple[Member, ...]
    days: tuple[AgendaDay, ...]


def build_agenda(
    *,
    events: Sequence[Event],
    members: Sequence[Member],
    tz: str,
    start: date,
    end: date,
    min_tier: Tier = Tier.BUSY,
) -> AgendaView:
    if end < start:
        raise ValueError("end must not precede start")

    zone = ZoneInfo(tz)
    roster = tuple(members)
    rank = {m.member_id: i for i, m in enumerate(roster)}
    window_start = datetime.combine(start, _MIDNIGHT, zone).astimezone(UTC)
    window_end = datetime.combine(end + timedelta(days=1), _MIDNIGHT, zone).astimezone(UTC)

    # A stored override replaces one generated instance. Keyed off the input
    # events only - anything we generate below carries the same two fields.
    overridden = {
        (e.recurrence_parent_id, _recurrence_key(e.recurrence_id))
        for e in events
        if e.recurrence_parent_id and e.recurrence_id and not e.rrule
    }

    day_events: dict[date, list[EventRow]] = defaultdict(list)
    busy_parts: dict[tuple[date, str], list[tuple[datetime, datetime, str, str]]] = defaultdict(
        list
    )

    for event in events:
        if event.is_deleted or TIER_ORDER[event.tier] > TIER_ORDER[min_tier]:
            continue
        instances = expand(event, window_start, window_end)
        if event.rrule:
            instances = [
                i
                for i in instances
                if (i.recurrence_parent_id, _recurrence_key(i.recurrence_id)) not in overridden
            ]
        for inst in instances:
            _place(inst, zone, rank, start, end, day_events, busy_parts)

    days = []
    for day in _dates(start, end):
        busy = tuple(
            _band(day, member_id, busy_parts[(day, member_id)])
            for member_id in sorted(
                {m for d, m in busy_parts if d == day},
                key=lambda m: (rank.get(m, len(roster)), m),
            )
        )
        rows = tuple(sorted(day_events[day], key=_row_sort_key))
        days.append(AgendaDay(date=day, busy=busy, events=rows))

    return AgendaView(start=start, end=end, tz=tz, members=roster, days=tuple(days))


def _place(
    inst: Event,
    zone: ZoneInfo,
    rank: dict[str, int],
    start: date,
    end: date,
    day_events: dict[date, list[EventRow]],
    busy_parts: dict[tuple[date, str], list[tuple[datetime, datetime, str, str]]],
) -> None:
    start_local, end_local = _local_span(inst, zone)
    member_ids = _member_ids(inst, rank)

    for day in _dates(*_covered(inst, start_local, end_local, start, end)):
        if inst.tier is Tier.BUSY:
            # Every member it constrains gets it - a shared work block must not
            # be invisible to the other person's band.
            for member_id in member_ids:
                busy_parts[(day, member_id)].append(
                    (start_local, end_local, inst.title, inst.event_id)
                )
            continue
        day_events[day].append(
            EventRow(
                event_id=inst.event_id,
                title=inst.title,
                start_local=start_local,
                end_local=end_local,
                start_utc=inst.start_utc,
                all_day=inst.all_day,
                tier=inst.tier,
                tier_source=inst.tier_source,
                owner_member_id=inst.owner_member_id,
                member_ids=member_ids,
                location=inst.location,
                visibility=inst.visibility,
                is_family=inst.tier is Tier.HOUSEHOLD and len(member_ids) > 1,
                occurrence_id=(
                    occurrence_id(inst.event_id, inst.start_utc)
                    if inst.recurrence_parent_id
                    else None
                ),
            )
        )


def _band(day: date, member_id: str, parts: list[tuple[datetime, datetime, str, str]]) -> BusyRow:
    day_start = datetime.combine(day, _MIDNIGHT)
    day_end = day_start + timedelta(days=1)
    # Clipped to the day: a band is a statement about *this* day's availability,
    # and an overnight block whose band started yesterday renders as nonsense.
    ordered = sorted(parts, key=lambda p: (p[0], p[2], p[3]))
    return BusyRow(
        member_id=member_id,
        start_local=min(max(p[0], day_start) for p in ordered),
        end_local=max(min(p[1], day_end) for p in ordered),
        count=len(ordered),
        event_ids=tuple(p[3] for p in ordered),
    )


def _local_span(inst: Event, zone: ZoneInfo) -> tuple[datetime, datetime]:
    if inst.all_day:
        # Floating dates. Running one through a zone is how an all-day event
        # slides onto the wrong day.
        return to_floating(inst.start_utc), to_floating(inst.end_utc)
    return (
        _utc(inst.start_utc).astimezone(zone).replace(tzinfo=None),
        _utc(inst.end_utc).astimezone(zone).replace(tzinfo=None),
    )


def _covered(
    inst: Event, start_local: datetime, end_local: datetime, start: date, end: date
) -> tuple[date, date]:
    first = start_local.date()
    if inst.all_day:
        # Tolerates both DTEND conventions (exclusive next midnight, or the same
        # date for a one-day event).
        last = max(first, end_local.date() - timedelta(days=1))
    else:
        last = end_local.date()
        if last > first and end_local.time() == _MIDNIGHT:
            last -= timedelta(days=1)  # ending exactly at midnight does not touch that day
    return max(first, start), min(last, end)


def _member_ids(inst: Event, rank: dict[str, int]) -> tuple[str, ...]:
    ids = {inst.owner_member_id, *inst.involves}
    # Roster order, unknown members last but still present. An unstable order
    # kills the agent's prompt cache and makes snapshot tests flap.
    return tuple(sorted(ids, key=lambda m: (rank.get(m, len(rank)), m)))


def _row_sort_key(row: EventRow) -> tuple[bool, datetime, str, str]:
    return (not row.all_day, row.start_local, row.title, row.occurrence_id or row.event_id)


def _dates(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def _recurrence_key(recurrence_id: str | None) -> str:
    """Normalize an override's recurrence-id so `Z` and `+00:00` match."""
    if not recurrence_id:
        return ""
    try:
        parsed = datetime.fromisoformat(recurrence_id.replace("Z", "+00:00"))
    except ValueError:
        return recurrence_id
    return _utc(parsed).astimezone(UTC).isoformat()


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
