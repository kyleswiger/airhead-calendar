"""Core domain types.

Deliberately dependency-free so both the DynamoDB and SQLite repositories can
import it, and so a future Raspberry Pi deployment does not drag AWS types along.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Tier(StrEnum):
    """Household relevance. The whole product hangs off this."""

    HOUSEHOLD = "T1"  # Constrains someone else - pickups, travel, shared meals.
    PERSONAL = "T2"  # A real commitment that obligates nobody else.
    BUSY = "T3"  # Only means "unavailable". Collapses to a band on the display.


class TierSource(StrEnum):
    AUTO = "auto"
    HUMAN = "human"


class Visibility(StrEnum):
    ALL = "all"
    ADULTS = "adults"


class SourceKind(StrEnum):
    NATIVE = "native"  # Created in Airhead itself.
    GOOGLE = "google"
    CALDAV = "caldav"
    GRAPH = "graph"
    ICS = "ics"


class MemberRole(StrEnum):
    ADULT = "adult"
    MINOR = "minor"


# Which source wins when a merge group picks its canonical record. Lower is better.
SOURCE_PRIORITY: dict[SourceKind, int] = {
    SourceKind.NATIVE: 0,
    SourceKind.GOOGLE: 1,
    SourceKind.GRAPH: 2,
    SourceKind.CALDAV: 3,
    SourceKind.ICS: 4,
}


@dataclass(slots=True)
class EventSource:
    kind: SourceKind
    source_id: str | None = None
    external_id: str | None = None
    etag: str | None = None


@dataclass(slots=True)
class Member:
    member_id: str
    household_id: str
    display_name: str
    role: MemberRole
    color: str  # Never the only carrier of identity - the name label is always shown too.
    cognito_sub: str | None = None

    @property
    def is_adult(self) -> bool:
        return self.role is MemberRole.ADULT

    def visibility_scope(self) -> Visibility:
        """The widest visibility this member is ever allowed to see."""
        return Visibility.ADULTS if self.is_adult else Visibility.ALL


@dataclass(slots=True)
class Source:
    source_id: str
    household_id: str
    kind: SourceKind
    owner_member_id: str
    label: str
    default_tier: Tier | None = None  # e.g. a calendar connected as "work" defaults to T3.
    cursor: str | None = None  # syncToken / ctag / ETag, opaque to everything but the adapter.
    last_sync_at: datetime | None = None
    enabled: bool = True


@dataclass(slots=True)
class Event:
    event_id: str
    household_id: str
    title: str
    start_utc: datetime
    end_utc: datetime
    tz: str
    owner_member_id: str
    source: EventSource
    all_day: bool = False
    rrule: str | None = None
    exdates: list[datetime] = field(default_factory=list)
    involves: list[str] = field(default_factory=list)
    location: str | None = None
    tier: Tier = Tier.PERSONAL
    tier_source: TierSource = TierSource.AUTO
    visibility: Visibility = Visibility.ALL
    merge_group_id: str | None = None
    recurrence_parent_id: str | None = None
    recurrence_id: str | None = None  # Original start of the instance this override replaces.
    created_by: str | None = None
    updated_at: datetime | None = None
    # Soft delete only. Source records are immutable truth and an unmerge or an
    # "actually, put that back" has to stay possible; a tombstone is swept after 30 days.
    deleted_at: datetime | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def content_hash(self) -> str:
        """Fingerprint of the fields a remote source owns.

        Sync compares this instead of diffing field by field, so locally-owned
        fields (tier, visibility, merge membership) never make an event look
        changed upstream and trigger a pointless write.
        """
        payload = {
            "title": self.title.strip(),
            "start": self.start_utc.isoformat(),
            "end": self.end_utc.isoformat(),
            "tz": self.tz,
            "all_day": self.all_day,
            "rrule": self.rrule or "",
            "location": (self.location or "").strip(),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def apply_remote_update(existing: Event, incoming: Event) -> Event:
    """Merge an upstream pull into the stored event.

    A human tier override is permanent. A re-sync that silently reverted it
    would make every correction on the kitchen display evaporate within 15
    minutes, which is the fastest way to lose trust in the whole system.
    """
    keep_tier = existing.tier_source is TierSource.HUMAN

    incoming.event_id = existing.event_id
    incoming.merge_group_id = existing.merge_group_id
    incoming.visibility = existing.visibility
    incoming.created_by = existing.created_by

    if keep_tier:
        incoming.tier = existing.tier
        incoming.tier_source = TierSource.HUMAN

    return incoming
