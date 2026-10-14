"""The realm boundary: `/api/*` is the public demo, `/family/api/*` the private household.

Driven through the real Lambda entry point (`airhead.handler.handler`) with API Gateway
v2 events, because the guarantee lives in two places a TestClient never touches: the
prefix rewrite and the `requestContext.authorizer` claims that only API Gateway writes.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest

from airhead.api import deps
from airhead.api.app import app
from airhead.domain import Member, MemberRole
from airhead.handler import handler
from fakes import (
    HOUSEHOLD,
    ROSTER,
    TZ,
    InMemoryEventRepo,
    InMemoryMemberRepo,
    InMemoryRoutineRepo,
    InMemorySourceRepo,
)

FAMILY = "hh_family_test"
CLIENT_ID = "testclientid123"

PARENT = Member(
    member_id="mem_parent",
    household_id=FAMILY,
    display_name="Parent",
    role=MemberRole.ADULT,
    color="#7aa2f7",
)

CLAIMS = {"sub": "0f6e-test-sub", "client_id": CLIENT_ID, "token_use": "access"}


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[InMemoryEventRepo, Any]]:
    monkeypatch.setenv("AIRHEAD_HOUSEHOLD_ID", HOUSEHOLD)
    monkeypatch.setenv("AIRHEAD_FAMILY_HOUSEHOLD_ID", FAMILY)
    monkeypatch.setenv("AIRHEAD_COGNITO_CLIENT_ID", CLIENT_ID)
    deps.get_settings.cache_clear()

    events = InMemoryEventRepo()
    members = InMemoryMemberRepo([*ROSTER, PARENT])
    routines = InMemoryRoutineRepo()
    sources = InMemorySourceRepo()
    app.dependency_overrides.clear()
    app.dependency_overrides[deps.get_event_repo] = lambda: events
    app.dependency_overrides[deps.get_member_repo] = lambda: members
    app.dependency_overrides[deps.get_routine_repo] = lambda: routines
    app.dependency_overrides[deps.get_source_repo] = lambda: sources
    app.dependency_overrides[deps.get_tz] = lambda: TZ
    yield events, members
    app.dependency_overrides.clear()
    deps.get_settings.cache_clear()


def invoke(
    method: str,
    path: str,
    *,
    member: str | None = None,
    claims: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    query: str = "",
) -> tuple[int, Any]:
    headers = {"content-type": "application/json"}
    if member is not None:
        headers["x-airhead-member"] = member
    context: dict[str, Any] = {
        "http": {
            "method": method,
            "path": path,
            "protocol": "HTTP/1.1",
            "sourceIp": "203.0.113.7",
            "userAgent": "pytest",
        },
        "requestId": "req-test",
        "stage": "$default",
    }
    if claims is not None:
        context["authorizer"] = {"jwt": {"claims": claims, "scopes": None}}
    event = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "headers": headers,
        "requestContext": context,
        "isBase64Encoded": False,
        "body": json.dumps(body) if body is not None else None,
    }
    response = handler(event, None)
    raw = response.get("body") or ""
    if response.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    return response["statusCode"], (json.loads(raw) if raw else None)


def _member_ids(payload: Any) -> list[str]:
    return [m["memberId"] for m in payload["members"]]


def test_demo_route_serves_demo_household(repos: Any) -> None:
    status, payload = invoke("GET", "/api/members", member="mem_alex")
    assert status == 200
    assert _member_ids(payload) == ["mem_alex", "mem_riley", "mem_sam"]


def test_family_route_with_verified_claims_serves_family_household(repos: Any) -> None:
    status, payload = invoke("GET", "/family/api/members", member="mem_parent", claims=CLAIMS)
    assert status == 200
    assert _member_ids(payload) == ["mem_parent"]


def test_family_route_without_claims_is_401(repos: Any) -> None:
    # What reaches the Lambda if a /family request falls through to the $default route.
    status, payload = invoke("GET", "/family/api/members", member="mem_parent")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_family_route_ignores_claims_without_sub(repos: Any) -> None:
    status, _ = invoke("GET", "/family/api/members", member="mem_parent", claims={"x": "y"})
    assert status == 401


def test_roster_needs_no_actor_so_the_screen_can_choose_one(repos: Any) -> None:
    status, payload = invoke("GET", "/family/api/members", claims=CLAIMS)
    assert status == 200 and _member_ids(payload) == ["mem_parent"]


def test_family_route_with_malformed_authorizer_is_401(repos: Any) -> None:
    status, _ = invoke("GET", "/family/api/members", claims="not-a-dict")  # type: ignore[arg-type]
    assert status == 401


def test_demo_visitor_cannot_act_as_family_member(repos: Any) -> None:
    status, _ = invoke("GET", "/api/routines", member="mem_parent")
    assert status == 401


def test_family_session_cannot_act_as_demo_member(repos: Any) -> None:
    status, _ = invoke("GET", "/family/api/routines", member="mem_alex", claims=CLAIMS)
    assert status == 401


def test_family_writes_never_reach_the_demo(repos: Any) -> None:
    events, _ = repos
    status, created = invoke(
        "POST",
        "/family/api/events",
        member="mem_parent",
        claims=CLAIMS,
        body={"title": "Private thing", "date": "2026-10-20"},
    )
    assert status == 201
    stored = events.get(FAMILY, created["eventId"])
    assert stored is not None and stored.household_id == FAMILY
    assert events.get(HOUSEHOLD, created["eventId"]) is None

    status, _ = invoke("GET", f"/api/events/{created['eventId']}", member="mem_alex")
    assert status == 404

    status, agenda = invoke(
        "GET", "/api/agenda", member="mem_alex", query="start=2026-10-20&end=2026-10-20"
    )
    assert status == 200
    titles = [r.get("title") for d in agenda["days"] for r in d["rows"]]
    assert "Private thing" not in titles


def test_family_not_configured_is_404(repos: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIRHEAD_FAMILY_HOUSEHOLD_ID")
    deps.get_settings.cache_clear()
    status, _ = invoke("GET", "/family/api/members", member="mem_parent", claims=CLAIMS)
    assert status == 404


def test_prefix_must_be_a_whole_segment(repos: Any) -> None:
    # `/familyx/...` is not the family realm; it is simply an unrouted path (405 because
    # the OPTIONS catch-all matches every path).
    status, _ = invoke("GET", "/familyx/api/members", member="mem_alex", claims=CLAIMS)
    assert status in (404, 405)


def test_auth_config_when_enabled(repos: Any) -> None:
    status, payload = invoke("GET", "/api/auth/config")
    assert status == 200
    assert payload == {"familyLogin": True, "region": "us-east-1", "clientId": CLIENT_ID}


def test_auth_config_when_disabled(repos: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIRHEAD_COGNITO_CLIENT_ID")
    deps.get_settings.cache_clear()
    status, payload = invoke("GET", "/api/auth/config")
    assert status == 200
    assert payload == {"familyLogin": False, "region": None, "clientId": None}
