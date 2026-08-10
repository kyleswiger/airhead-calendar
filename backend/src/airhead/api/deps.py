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
from typing import Annotated, Any

from fastapi import Depends, Header

from airhead.api.errors import Unauthorized
from airhead.domain import Member
from airhead.repo.base import EventRepo, MemberRepo, SourceRepo
from airhead.repo.turns import TurnRepo


@dataclass(frozen=True, slots=True)
class Settings:
    household_id: str
    tz: str
    backend: str  # "dynamodb" | "sqlite"
    table_name: str
    sqlite_path: str
    agent_model: str
    # Depth, not length: Sonnet 4.6 deprecates `budget_tokens` in favor of `output_config.effort`.
    agent_effort: str
    # Caps thinking *plus* response text, so it is sized well above the visible answer.
    agent_max_tokens: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        household_id=os.environ.get("AIRHEAD_HOUSEHOLD_ID", "hh_1"),
        tz=os.environ.get("AIRHEAD_TZ", "America/New_York"),
        backend=os.environ.get("AIRHEAD_REPO_BACKEND", "dynamodb"),
        table_name=os.environ.get("AIRHEAD_TABLE", "airhead"),
        sqlite_path=os.environ.get("AIRHEAD_SQLITE_PATH", ":memory:"),
        agent_model=os.environ.get("AIRHEAD_AGENT_MODEL", "us.anthropic.claude-sonnet-4-6"),
        agent_effort=os.environ.get("AIRHEAD_AGENT_EFFORT", "medium"),
        agent_max_tokens=int(os.environ.get("AIRHEAD_AGENT_MAX_TOKENS", "16000")),
    )


@lru_cache(maxsize=1)
def _repos() -> tuple[EventRepo, MemberRepo, SourceRepo, TurnRepo]:
    settings = get_settings()
    if settings.backend == "sqlite":
        from airhead.repo.sqlite import (
            SqliteEventRepo,
            SqliteMemberRepo,
            SqliteSourceRepo,
            connect,
        )
        from airhead.repo.turns import SqliteTurnRepo

        conn = connect(settings.sqlite_path)
        return (
            SqliteEventRepo(conn),
            SqliteMemberRepo(conn),
            SqliteSourceRepo(conn),
            SqliteTurnRepo(conn),
        )

    # Imported lazily: this is the only line in the API package that pulls in boto3.
    from airhead.repo.dynamo import DynamoEventRepo, DynamoMemberRepo, DynamoSourceRepo
    from airhead.repo.turns import DynamoTurnRepo

    table = settings.table_name
    return (
        DynamoEventRepo(table),
        DynamoMemberRepo(table),
        DynamoSourceRepo(table),
        DynamoTurnRepo(table),
    )


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


def get_turn_repo() -> TurnRepo:
    return _repos()[3]


@lru_cache(maxsize=1)
def _anthropic_client() -> Any:
    """The Bedrock client (legacy InvokeModel path), built on first use and reused
    across warm invocations.

    Legacy `AnthropicBedrock` rather than `AnthropicBedrockMantle` on purpose: this
    AWS account is not onboarded to the bedrock-mantle endpoint (every model 403s
    there), while the bedrock-runtime InvokeModel path works today with the
    `us.anthropic.claude-sonnet-4-6` inference profile.

    No API key anywhere: the client SigV4-signs requests with the Lambda role's own
    credentials, so auth is the IAM policy in infra/iam.tf (bedrock:InvokeModel* on the
    Claude inference profile) and billing lands on the AWS account. Locally, the same
    default credential chain applies - any profile with Bedrock access works.

    Never at import time: pytest would need AWS credentials just to import the app, and
    a cold Lambda that imports the SDK before it knows it needs to pays for that on
    every invocation, including the routes that never touch the model.
    """
    from anthropic import AnthropicBedrock

    return AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1"))


def get_runner() -> Any:
    """The agent's model loop (`airhead.agent.runner`).

    Injected as a whole module rather than a bare function so the route can build a
    `TurnRequest` and a `Confirmation` without importing the agent package at module
    scope — that import pulls in the Anthropic SDK, and the HTTP surface is tested
    with a stub of this seam so the routes stay independent of the model loop.
    """
    from airhead.agent import runner

    return runner


def get_agent_deps(
    runner: Annotated[Any, Depends(get_runner)],
    events: Annotated[EventRepo, Depends(get_event_repo)],
    members: Annotated[MemberRepo, Depends(get_member_repo)],
) -> Any:
    settings = get_settings()
    return runner.AgentDeps(
        events=events,
        members=members,
        client=_anthropic_client(),
        model=settings.agent_model,
        effort=settings.agent_effort,
        max_tokens=settings.agent_max_tokens,
    )


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
Turns = Annotated[TurnRepo, Depends(get_turn_repo)]
Runner = Annotated[Any, Depends(get_runner)]
AgentRuntime = Annotated[Any, Depends(get_agent_deps)]
HouseholdId = Annotated[str, Depends(get_household_id)]
Tz = Annotated[str, Depends(get_tz)]
