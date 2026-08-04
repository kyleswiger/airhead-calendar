"""Injection seams.

Every route reaches storage and the acting member through these functions, so tests
swap in `tests/fakes.py` with `app.dependency_overrides` and production picks its
backend from the environment. Nothing here constructs a boto3 client at import time —
a cold Lambda that imports botocore before it knows whether it needs it pays for that
on every invocation, and pytest would need AWS credentials just to import the app.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header

from airhead.api.errors import Unauthorized
from airhead.domain import Member
from airhead.repo.base import EventRepo, MemberRepo, SourceRepo


@dataclass(frozen=True, slots=True)
class Settings:
    household_id: str
    tz: str
    backend: str  # "dynamodb" | "sqlite"
    table_name: str
    sqlite_path: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        household_id=os.environ.get("AIRHEAD_HOUSEHOLD_ID", "hh_1"),
        tz=os.environ.get("AIRHEAD_TZ", "America/New_York"),
        backend=os.environ.get("AIRHEAD_REPO_BACKEND", "dynamodb"),
        table_name=os.environ.get("AIRHEAD_TABLE", "airhead"),
        sqlite_path=os.environ.get("AIRHEAD_SQLITE_PATH", ":memory:"),
    )


@lru_cache(maxsize=1)
def _repos() -> tuple[EventRepo, MemberRepo, SourceRepo]:
    settings = get_settings()
    if settings.backend == "sqlite":
        from airhead.repo.sqlite import (
            SqliteEventRepo,
            SqliteMemberRepo,
            SqliteSourceRepo,
            connect,
        )

        conn = connect(settings.sqlite_path)
        return SqliteEventRepo(conn), SqliteMemberRepo(conn), SqliteSourceRepo(conn)

    # Imported lazily: this is the only line in the API package that pulls in boto3.
    from airhead.repo.dynamo import DynamoEventRepo, DynamoMemberRepo, DynamoSourceRepo

    table = settings.table_name
    return DynamoEventRepo(table), DynamoMemberRepo(table), DynamoSourceRepo(table)


def get_household_id() -> str:
    return get_settings().household_id


def get_tz() -> str:
    return get_settings().tz


def get_event_repo() -> EventRepo:
    return _repos()[0]


def get_member_repo() -> MemberRepo:
    return _repos()[1]


def get_source_repo() -> SourceRepo:
    return _repos()[2]


def get_actor(
    members: Annotated[MemberRepo, Depends(get_member_repo)],
    household_id: Annotated[str, Depends(get_household_id)],
    x_airhead_member: Annotated[str | None, Header(alias="X-Airhead-Member")] = None,
) -> Member:
    """Resolve the acting member from the M1 header shim.

    THIS IS THE ONLY FUNCTION COGNITO CHANGES. When the authorizer lands it reads the
    verified `sub` off the request context and looks the member up by `cognito_sub`
    instead of by header; every route already takes its actor from here and every
    authorization decision downstream is already server-side, so nothing else moves.
    """
    if not x_airhead_member:
        raise Unauthorized("Missing X-Airhead-Member header.")
    member = members.get(household_id, x_airhead_member)
    if member is None:
        raise Unauthorized("Unknown member.")
    return member


Actor = Annotated[Member, Depends(get_actor)]
Events = Annotated[EventRepo, Depends(get_event_repo)]
Members = Annotated[MemberRepo, Depends(get_member_repo)]
Sources = Annotated[SourceRepo, Depends(get_source_repo)]
HouseholdId = Annotated[str, Depends(get_household_id)]
Tz = Annotated[str, Depends(get_tz)]
