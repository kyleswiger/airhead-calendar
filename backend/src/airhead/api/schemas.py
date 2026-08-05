"""Wire models for the M1 contract (docs/M1-CONTRACT.md).

camelCase on the wire, snake_case in Python — the alias generator does that once here so
no route ever hand-spells a field name. Local ("floating") timestamps are pre-formatted
strings because the display does no timezone math: an all-day row must carry a plain
`2026-08-04` and a timed row a `2026-08-04T16:00:00`, and one field can be either.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from airhead.domain import MemberRole, Tier, TierSource, Visibility

MAX_TITLE = 500
MAX_LOCATION = 500


class Wire(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        use_enum_values=False,
    )


def fmt_local(value: dt.datetime, *, all_day: bool) -> str:
    """Floating local string. All-day events get a bare date, never a midnight instant."""
    return value.date().isoformat() if all_day else value.replace(tzinfo=None).isoformat()


# --- responses ---------------------------------------------------------------


class MemberOut(Wire):
    member_id: str
    display_name: str
    role: MemberRole
    color: str


class MembersResponse(Wire):
    members: list[MemberOut]


class RangeOut(Wire):
    start: dt.date
    end: dt.date
    tz: str


class EventRowOut(Wire):
    kind: Literal["event"] = "event"
    event_id: str
    title: str
    start_local: str
    end_local: str
    start_utc: dt.datetime
    all_day: bool
    tier: Tier
    tier_source: TierSource
    owner_member_id: str
    member_ids: list[str]
    location: str | None = None
    visibility: Visibility
    is_family: bool
    occurrence_id: str | None = None


class BusyRowOut(Wire):
    kind: Literal["busy"] = "busy"
    member_id: str
    start_local: str
    end_local: str
    count: int
    event_ids: list[str]


Row = Annotated[EventRowOut | BusyRowOut, Field(discriminator="kind")]


class AgendaDayOut(Wire):
    date: dt.date
    rows: list[Row]


class AgendaResponse(Wire):
    range: RangeOut
    members: list[MemberOut]
    days: list[AgendaDayOut]


class ErrorDetail(Wire):
    code: str
    message: str


class ErrorResponse(Wire):
    error: ErrorDetail


# --- requests ----------------------------------------------------------------


def _check_tz(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # ZoneInfoNotFoundError is a KeyError, which pydantic would let escape as a 500.
        raise ValueError("unknown IANA timezone") from exc
    return value


def _check_floating(value: dt.datetime | None) -> dt.datetime | None:
    # A local wall-clock time with an offset is a contradiction; `tz` names the zone.
    if value is not None and value.tzinfo is not None:
        raise ValueError("must be a floating local time without an offset")
    return value


class EventCreate(Wire):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    start_local: dt.datetime | None = None
    end_local: dt.datetime | None = None
    date: dt.date | None = None
    all_day: bool = False
    tz: str | None = None
    rrule: str | None = Field(default=None, max_length=1000)
    owner_member_id: str | None = None
    involves: list[str] = Field(default_factory=list)
    location: str | None = Field(default=None, max_length=MAX_LOCATION)
    tier: Tier | None = None
    visibility: Visibility | None = None

    _tz = field_validator("tz")(_check_tz)
    _floating = field_validator("start_local", "end_local")(_check_floating)

    @model_validator(mode="after")
    def _times_coherent(self) -> EventCreate:
        if self.all_day or self.date is not None:
            if self.date is None:
                raise ValueError("allDay requires date")
        elif self.start_local is None or self.end_local is None:
            raise ValueError("startLocal and endLocal are required")
        if self.start_local and self.end_local and self.end_local < self.start_local:
            raise ValueError("endLocal precedes startLocal")
        return self


class EventPatch(Wire):
    """Every field optional. `model_fields_set` distinguishes absent from explicit null."""

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    start_local: dt.datetime | None = None
    end_local: dt.datetime | None = None
    date: dt.date | None = None
    all_day: bool | None = None
    tz: str | None = None
    rrule: str | None = Field(default=None, max_length=1000)
    owner_member_id: str | None = None
    involves: list[str] | None = None
    location: str | None = Field(default=None, max_length=MAX_LOCATION)
    tier: Tier | None = None
    visibility: Visibility | None = None

    _tz = field_validator("tz")(_check_tz)
    _floating = field_validator("start_local", "end_local")(_check_floating)

    @model_validator(mode="after")
    def _times_coherent(self) -> EventPatch:
        if self.start_local and self.end_local and self.end_local < self.start_local:
            raise ValueError("endLocal precedes startLocal")
        return self
