"""Repository interfaces.

Data access sits behind these so the same application code runs against
DynamoDB in AWS and against SQLite in pytest (hermetic, in-memory) or on a
future Pi-only deployment. Nothing above this layer may import boto3.

The visibility filter lives *here*, at the query layer, on purpose. Calendar
titles are attacker-controllable through any external meeting invite, so the
model can never be the thing deciding what an 11-year-old is allowed to see.
By the time a row reaches the agent it has already been filtered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from airhead.domain import Event, Member, Source, Tier, Visibility

# Tiers ordered from most to least household-relevant, for `min_tier` filtering.
TIER_ORDER: dict[Tier, int] = {Tier.HOUSEHOLD: 0, Tier.PERSONAL: 1, Tier.BUSY: 2}


@dataclass(frozen=True, slots=True)
class AgendaQuery:
    """A bounded window of events, already narrowed to what the caller may see.

    `visibility_scope` defaults to the *most restrictive* value. Forgetting to
    pass it therefore under-shares rather than leaking an adults-only event.
    """

    household_id: str
    start_utc: datetime
    end_utc: datetime
    visibility_scope: Visibility = Visibility.ALL
    member_ids: tuple[str, ...] | None = None  # None = every member.
    min_tier: Tier = Tier.BUSY  # T3 = include everything.
    include_deleted: bool = False

    def allows(self, event: Event) -> bool:
        """Whether a stored event belongs in this query's result set.

        Repositories may push any of this down into the store, but the answer
        must match. The SQLite and DynamoDB implementations are held to it by a
        shared contract test.
        """
        if event.household_id != self.household_id:
            return False
        if event.is_deleted and not self.include_deleted:
            return False
        if self.visibility_scope is Visibility.ALL and event.visibility is Visibility.ADULTS:
            return False
        if TIER_ORDER[event.tier] > TIER_ORDER[self.min_tier]:
            return False
        if self.member_ids is not None:
            involved = {event.owner_member_id, *event.involves}
            if involved.isdisjoint(self.member_ids):
                return False
        # A recurring master overlaps the window if its RRULE puts an instance
        # there; expansion decides that, so masters are never range-excluded.
        if event.rrule:
            return True
        return event.start_utc < self.end_utc and event.end_utc > self.start_utc


@dataclass(frozen=True, slots=True)
class Page:
    """One page of events. `cursor` is opaque; None means the set is exhausted."""

    events: list[Event] = field(default_factory=list)
    cursor: str | None = None


@runtime_checkable
class EventRepo(Protocol):
    def get(self, household_id: str, event_id: str) -> Event | None:
        """Return the event including tombstones; callers decide what to do with them."""
        ...

    def put(self, event: Event) -> Event:
        """Create or fully replace. Returns the stored record with `updated_at` set."""
        ...

    def delete(self, household_id: str, event_id: str, *, at: datetime) -> Event | None:
        """Soft-delete: stamp `deleted_at`. Returns None if the event never existed."""
        ...

    def list_range(self, query: AgendaQuery, *, cursor: str | None = None) -> Page:
        """Events overlapping the window, filtered per `query`. Recurring masters
        are returned unexpanded - `airhead.recurrence` expands them at read time."""
        ...

    def get_by_external_id(self, source_id: str, external_id: str) -> Event | None:
        """Idempotent-upsert lookup for the sync path (GSI1)."""
        ...


@runtime_checkable
class MemberRepo(Protocol):
    def get(self, household_id: str, member_id: str) -> Member | None: ...

    def list(self, household_id: str) -> list[Member]:
        """Deterministically ordered by member_id - the agent's cached prompt
        prefix includes the roster, and an unstable order silently kills the
        prompt cache."""
        ...

    def put(self, member: Member) -> Member: ...


@runtime_checkable
class SourceRepo(Protocol):
    def get(self, household_id: str, source_id: str) -> Source | None: ...

    def list(self, household_id: str) -> list[Source]: ...

    def put(self, source: Source) -> Source: ...


class RepoError(RuntimeError):
    """Storage-layer failure. Implementations wrap provider exceptions in this
    so nothing above the repo layer catches a botocore or sqlite3 type."""


class NotFound(RepoError):
    pass
