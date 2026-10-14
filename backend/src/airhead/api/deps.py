"""Injection seams.

Every route reaches storage and the acting member through these functions, so tests
swap in `tests/fakes.py` with `app.dependency_overrides` and production picks its
backend from the environment. Nothing here constructs a boto3 client at import time —
a cold Lambda that imports botocore before it knows whether it needs it pays for that
on every invocation, and pytest would need AWS credentials just to import the app.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Header, Request

from airhead.api.errors import NotFound, Unauthorized
from airhead.domain import Member
from airhead.repo.base import EventRepo, MemberRepo, RoutineRepo, SourceRepo
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
    # The private household behind Cognito sign-in. None = family login not deployed yet,
    # so `/family/*` answers 404 and the public demo (`household_id`) is all there is.
    family_household_id: str | None = None
    # Public SPA client id (no secret); served to the login screen by /api/auth/config.
    cognito_client_id: str | None = None
    region: str = "us-east-1"


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
        family_household_id=os.environ.get("AIRHEAD_FAMILY_HOUSEHOLD_ID") or None,
        cognito_client_id=os.environ.get("AIRHEAD_COGNITO_CLIENT_ID") or None,
        region=os.environ.get("AWS_REGION", "us-east-1"),
    )


@lru_cache(maxsize=1)
def _repos() -> tuple[EventRepo, MemberRepo, SourceRepo, TurnRepo, RoutineRepo]:
    settings = get_settings()
    if settings.backend == "sqlite":
        from airhead.repo.sqlite import (
            SqliteEventRepo,
            SqliteMemberRepo,
            SqliteRoutineRepo,
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
            SqliteRoutineRepo(conn),
        )

    # Imported lazily: this is the only line in the API package that pulls in boto3.
    from airhead.repo.dynamo import (
        DynamoEventRepo,
        DynamoMemberRepo,
        DynamoRoutineRepo,
        DynamoSourceRepo,
    )
    from airhead.repo.turns import DynamoTurnRepo

    table = settings.table_name
    return (
        DynamoEventRepo(table),
        DynamoMemberRepo(table),
        DynamoSourceRepo(table),
        DynamoTurnRepo(table),
        DynamoRoutineRepo(table),
    )


FAMILY_SCOPE_KEY = "airhead.family"


def verified_claims(request: Request) -> dict[str, Any] | None:
    """The Cognito claims API Gateway's JWT authorizer verified, or None.

    Read off the Lambda event Mangum keeps in the ASGI scope, never off a header: a
    client can send any header it likes, but `requestContext.authorizer` is written by
    API Gateway and only on routes that carry the authorizer (the `/family/*` routes in
    infra/api.tf). Signature, expiry, issuer and client id are all checked there.
    """
    event = request.scope.get("aws.event") or {}
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims")
    return claims if isinstance(claims, dict) and claims.get("sub") else None


def get_household_id(request: Request) -> str:
    """Which household this request reads and writes - the realm boundary.

    `/family/*` (prefix stripped by `FamilyPrefixMiddleware`) is the private household
    and requires verified claims; everything else is the public demo. The prefix only
    *selects* the realm - the claims check is the gate - so a `/family` request that
    reaches the unauthorized `$default` route (an unlisted method, say) is a 401, not a
    read of the private calendar.
    """
    settings = get_settings()
    if not request.scope.get(FAMILY_SCOPE_KEY):
        return settings.household_id
    if settings.family_household_id is None:
        raise NotFound("Family calendar is not configured.")
    if verified_claims(request) is None:
        raise Unauthorized("Sign in to view the family calendar.")
    return settings.family_household_id


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


def get_routine_repo() -> RoutineRepo:
    return _repos()[4]


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
    routines: Annotated[RoutineRepo, Depends(get_routine_repo)],
) -> Any:
    settings = get_settings()
    return runner.AgentDeps(
        events=events,
        members=members,
        routines=routines,
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
    """Resolve the acting member from the `X-Airhead-Member` header.

    Cognito authenticates the *household*, not the person (PRD §13): the kitchen
    screen is a shared device signed in once, and the header picks who is acting from
    that household's roster - the member-selection step. The lookup is scoped by
    `household_id`, which `get_household_id` only resolves to the private household
    after verifying the JWT, so a demo visitor cannot name a family member.
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
Routines = Annotated[RoutineRepo, Depends(get_routine_repo)]
Runner = Annotated[Any, Depends(get_runner)]
AgentRuntime = Annotated[Any, Depends(get_agent_deps)]
HouseholdId = Annotated[str, Depends(get_household_id)]
Tz = Annotated[str, Depends(get_tz)]


def get_estimator() -> Callable[[str, str | None], Any]:
    """`POST /api/routines/estimate`'s model step, as a `(name, context) -> Resolution`.

    A callable rather than a client so the tests fake the whole step. The Bedrock
    client is built lazily *inside* the callable: a catalog hit never invokes it, and
    resolving this dependency must stay free of boto3 for that path.
    """

    def estimate(name: str, context: str | None) -> Any:
        from airhead.routines.estimate import estimate_interval

        return estimate_interval(
            _anthropic_client(), model=get_settings().agent_model, name=name, context=context
        )

    return estimate


Estimator = Annotated[Callable[[str, str | None], Any], Depends(get_estimator)]
