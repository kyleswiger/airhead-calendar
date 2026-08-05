"""In-memory repositories and an app factory for the API tests.

`list_range` filters through `AgendaQuery.allows` rather than reimplementing the rules,
so a fake can never be more permissive than the real stores — which matters here,
because these are the fixtures the visibility tests are judged against.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from airhead.api import deps
from airhead.api.app import app
from airhead.domain import (
    Event,
    EventSource,
    Member,
    MemberRole,
    Source,
    SourceKind,
    Tier,
    TierSource,
    Visibility,
)
from airhead.repo.base import AgendaQuery, Page

HOUSEHOLD = "hh_1"
TZ = "America/New_York"


class InMemoryEventRepo:
    def __init__(self, events: list[Event] | None = None) -> None:
        self._events: dict[tuple[str, str], Event] = {}
        for event in events or []:
            self.put(event)

    def get(self, household_id: str, event_id: str) -> Event | None:
        stored = self._events.get((household_id, event_id))
        return copy.deepcopy(stored) if stored else None

    def put(self, event: Event) -> Event:
        stored = copy.deepcopy(event)
        stored.updated_at = datetime.now(UTC)
        self._events[(stored.household_id, stored.event_id)] = stored
        return copy.deepcopy(stored)

    def delete(self, household_id: str, event_id: str, *, at: datetime) -> Event | None:
        stored = self._events.get((household_id, event_id))
        if stored is None:
            return None
        stored.deleted_at = at
        return copy.deepcopy(stored)

    def list_range(self, query: AgendaQuery, *, cursor: str | None = None) -> Page:
        del cursor  # The fake never paginates; the API drains pages regardless.
        hits = [copy.deepcopy(e) for e in self._events.values() if query.allows(e)]
        hits.sort(key=lambda e: (e.start_utc, e.event_id))
        return Page(events=hits, cursor=None)

    def get_by_external_id(self, source_id: str, external_id: str) -> Event | None:
        for event in self._events.values():
            src = event.source
            if src.source_id == source_id and src.external_id == external_id:
                return copy.deepcopy(event)
        return None


class InMemoryMemberRepo:
    def __init__(self, members: list[Member] | None = None) -> None:
        self._members: dict[tuple[str, str], Member] = {}
        for member in members or []:
            self.put(member)

    def get(self, household_id: str, member_id: str) -> Member | None:
        stored = self._members.get((household_id, member_id))
        return replace(stored) if stored else None

    def list(self, household_id: str) -> list[Member]:
        hits = [m for m in self._members.values() if m.household_id == household_id]
        return [replace(m) for m in sorted(hits, key=lambda m: m.member_id)]

    def put(self, member: Member) -> Member:
        self._members[(member.household_id, member.member_id)] = replace(member)
        return replace(member)


class InMemorySourceRepo:
    def __init__(self, sources: list[Source] | None = None) -> None:
        self._sources: dict[tuple[str, str], Source] = {}
        for source in sources or []:
            self.put(source)

    def get(self, household_id: str, source_id: str) -> Source | None:
        stored = self._sources.get((household_id, source_id))
        return replace(stored) if stored else None

    def list(self, household_id: str) -> list[Source]:
        hits = [s for s in self._sources.values() if s.household_id == household_id]
        return [replace(s) for s in sorted(hits, key=lambda s: s.source_id)]

    def put(self, source: Source) -> Source:
        self._sources[(source.household_id, source.source_id)] = replace(source)
        return replace(source)


# --- fixtures ----------------------------------------------------------------

ALEX = Member(
    member_id="mem_alex",
    household_id=HOUSEHOLD,
    display_name="Alex",
    role=MemberRole.ADULT,
    color="#7aa2f7",
)
SAM = Member(
    member_id="mem_sam",
    household_id=HOUSEHOLD,
    display_name="Sam",
    role=MemberRole.ADULT,
    color="#9ece6a",
)
RILEY = Member(
    member_id="mem_riley",
    household_id=HOUSEHOLD,
    display_name="Riley",
    role=MemberRole.MINOR,
    color="#f7768e",
)
ROSTER = [ALEX, SAM, RILEY]


def make_event(
    event_id: str,
    *,
    title: str = "Soccer practice",
    start: datetime,
    end: datetime,
    owner: str = "mem_riley",
    involves: list[str] | None = None,
    tier: Tier = Tier.HOUSEHOLD,
    tier_source: TierSource = TierSource.AUTO,
    visibility: Visibility = Visibility.ALL,
    all_day: bool = False,
    created_by: str | None = None,
    location: str | None = None,
) -> Event:
    return Event(
        event_id=event_id,
        household_id=HOUSEHOLD,
        title=title,
        start_utc=start,
        end_utc=end,
        tz=TZ,
        owner_member_id=owner,
        source=EventSource(kind=SourceKind.NATIVE),
        all_day=all_day,
        involves=involves or [],
        location=location,
        tier=tier,
        tier_source=tier_source,
        visibility=visibility,
        created_by=created_by or owner,
    )


def build_client(
    events: list[Event] | None = None, members: list[Member] | None = None
) -> tuple[TestClient, InMemoryEventRepo, InMemoryMemberRepo]:
    event_repo = InMemoryEventRepo(events)
    member_repo = InMemoryMemberRepo(members if members is not None else ROSTER)
    source_repo = InMemorySourceRepo()

    app.dependency_overrides[deps.get_event_repo] = lambda: event_repo
    app.dependency_overrides[deps.get_member_repo] = lambda: member_repo
    app.dependency_overrides[deps.get_source_repo] = lambda: source_repo
    app.dependency_overrides[deps.get_household_id] = lambda: HOUSEHOLD
    app.dependency_overrides[deps.get_tz] = lambda: TZ

    return TestClient(app, raise_server_exceptions=False), event_repo, member_repo


def as_member(member_id: str) -> dict[str, str]:
    return {"X-Airhead-Member": member_id}
