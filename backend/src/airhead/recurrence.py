"""Recurrence expansion.

The RRULE is stored verbatim and expanded at read time inside a bounded window
(PRD §7). Expansion runs in the event's *own* IANA zone and only then converts
back to UTC. Adding 7x24h in UTC is the trap: a 16:00 practice in
America/New_York silently becomes 15:00 for the four months after the November
transition, which is exactly the kind of quietly-wrong row that ends trust in
the display (PRD R6).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from airhead.domain import Event

# Hard bounds on one master's expansion. A legitimate window is at most 31 days
# (M1 contract) or 120 for a sync pass, so a COUNT-less daily rule needs ~120
# instances. Anything past these came from a malformed or hostile feed
# (FREQ=SECONDLY with no COUNT) and is truncated rather than allowed to hang a
# request. MAX_ITERATIONS also bounds the walk from a dtstart years in the past.
MAX_OCCURRENCES = 750
MAX_ITERATIONS = 20_000

# The master's start is authoritative; a DTSTART carried inside the stored rule
# text would override the dtstart we pass and could come back in a different
# zone (or naive), so it is dropped before parsing.
_DTSTART_LINE_RE = re.compile(r"^DTSTART[^\r\n]*\r?\n?", re.IGNORECASE | re.MULTILINE)
_UNTIL_RE = re.compile(r"UNTIL=(\d{8}(?:T\d{6})?Z?)", re.IGNORECASE)


def occurrence_id(event_id: str, start_utc: datetime) -> str:
    """Stable handle for one instance. The display uses it as a React key."""
    return f"{event_id}@{_iso(start_utc)}"


def to_floating(dt: datetime) -> datetime:
    """Read a datetime as wall-clock fields, discarding any zone.

    All-day events are floating dates. Converting one through a zone is the
    classic off-by-one-day bug, so every all-day path goes through here.
    """
    return dt.replace(tzinfo=None)


def expand(event: Event, window_start: datetime, window_end: datetime) -> list[Event]:
    """Instances of `event` overlapping [window_start, window_end).

    A non-recurring event yields itself (unchanged) or nothing. Generated
    instances are copies carrying `recurrence_parent_id` and `recurrence_id`.
    """
    window_start = _instant(window_start)
    window_end = _instant(window_end)
    if window_end <= window_start:
        return []
    if not event.rrule:
        return [event] if _overlaps(event, window_start, window_end) else []
    try:
        if event.all_day:
            return _expand_all_day(event, window_start, window_end)
        return _expand_timed(event, window_start, window_end)
    except Exception:
        # A junk RRULE out of an imported feed must not take the agenda down,
        # and it must not make the event vanish either (PRD R6). Degrade to the
        # master occurrence alone.
        return [event] if _overlaps(event, window_start, window_end) else []


def _expand_timed(event: Event, window_start: datetime, window_end: datetime) -> list[Event]:
    zone = ZoneInfo(event.tz)
    local_start = _instant(event.start_utc).astimezone(zone)
    # Wall-clock duration, not elapsed duration: a 16:00-17:30 practice is 90
    # minutes on the clock on both sides of a transition, even in the week
    # where those 90 minutes are really 30 or 150.
    wall_delta = to_floating(_instant(event.end_utc).astimezone(zone)) - to_floating(local_start)

    rule = _rule_set(event.rrule, local_start, zone, aware=True)
    excluded = {_instant(x) for x in event.exdates}
    win_end_local = window_end.astimezone(zone)

    out: list[Event] = []
    for i, occ in enumerate(rule):
        if i >= MAX_ITERATIONS or occ >= win_end_local:
            break
        # dateutil replaces tzinfo rather than converting, so `occ` is already
        # the right wall time and ZoneInfo resolves that date's offset for us.
        start_utc = occ.astimezone(UTC)
        end_utc = (to_floating(occ) + wall_delta).replace(tzinfo=zone).astimezone(UTC)
        # start < window_end already holds via the break above.
        in_window = end_utc > window_start or (end_utc == start_utc >= window_start)
        if not in_window or start_utc in excluded:
            continue
        out.append(_instance(event, start_utc, end_utc))
        if len(out) >= MAX_OCCURRENCES:
            break
    return out


def _expand_all_day(event: Event, window_start: datetime, window_end: datetime) -> list[Event]:
    zone = ZoneInfo(event.tz)
    dtstart = to_floating(event.start_utc)
    span = to_floating(event.end_utc) - dtstart
    tzinfo = event.start_utc.tzinfo  # preserved so instances round-trip like the master

    rule = _rule_set(event.rrule, dtstart, zone, aware=False)
    excluded = {to_floating(x).date() for x in event.exdates}
    # The window is a pair of instants but an all-day event is a floating date,
    # so an exact comparison is meaningless at the edges. Over-generating by a
    # day either way is safe - the agenda places rows by date and drops the rest.
    first = window_start.astimezone(zone).date() - timedelta(days=max(span.days, 1))
    last = window_end.astimezone(zone).date() + timedelta(days=1)

    out: list[Event] = []
    for i, occ in enumerate(rule):
        if i >= MAX_ITERATIONS or occ.date() >= last:
            break
        if occ.date() < first or occ.date() in excluded:
            continue
        start = occ.replace(tzinfo=tzinfo)
        out.append(_instance(event, start, start + span))
        if len(out) >= MAX_OCCURRENCES:
            break
    return out


def _instance(event: Event, start_utc: datetime, end_utc: datetime) -> Event:
    return replace(
        event,
        start_utc=start_utc,
        end_utc=end_utc,
        rrule=None,  # an instance is not itself recurring; re-expanding it would explode
        exdates=[],
        involves=list(event.involves),
        source=replace(event.source),
        recurrence_parent_id=event.event_id,
        recurrence_id=_iso(start_utc),
    )


def _rule_set(rrule_text: str, dtstart: datetime, zone: ZoneInfo, *, aware: bool):
    text = _DTSTART_LINE_RE.sub("", rrule_text).strip()
    return rrulestr(_normalize_until(text, zone, aware=aware), dtstart=dtstart, forceset=True)


def _normalize_until(text: str, zone: ZoneInfo, *, aware: bool) -> str:
    """Make UNTIL agree with dtstart's awareness.

    dateutil refuses to build a rule whose UNTIL and DTSTART disagree, and real
    feeds mix them constantly (a `Z` UNTIL against an all-day DATE start, a
    naive UNTIL against a timed one). Rejecting the rule would drop the event.
    """

    def sub(match: re.Match[str]) -> str:
        raw = match.group(1)
        has_z = raw.endswith("Z")
        body = raw[:-1] if has_z else raw
        if len(body) == 8:
            body += "T235959"  # a DATE-valued UNTIL includes the whole day
        if not aware:
            return f"UNTIL={body}"
        if has_z:
            return f"UNTIL={body}Z"
        local = datetime.strptime(body, "%Y%m%dT%H%M%S").replace(tzinfo=zone)
        return "UNTIL=" + local.astimezone(UTC).strftime("%Y%m%dT%H%M%S") + "Z"

    return _UNTIL_RE.sub(sub, text)


def _overlaps(event: Event, window_start: datetime, window_end: datetime) -> bool:
    if event.all_day:
        zone = ZoneInfo(event.tz)
        first = to_floating(event.start_utc).date()
        # Tolerates both DTEND conventions: an exclusive next-midnight end and
        # an adapter that stored the same date for a one-day event.
        last_exclusive = max(to_floating(event.end_utc).date(), first + timedelta(days=1))
        return (
            first < window_end.astimezone(zone).date()
            and last_exclusive > window_start.astimezone(zone).date()
        )
    start = _instant(event.start_utc)
    end = _instant(event.end_utc)
    if end == start:  # a zero-length marker still belongs to the day it lands on
        return window_start <= start < window_end
    return start < window_end and end > window_start


def _instant(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        return dt.isoformat()
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat() + "Z"
