from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from moto import mock_aws

from airhead.repo.base import EventRepo, MemberRepo, SourceRepo
from airhead.repo.dynamo import DynamoEventRepo, DynamoMemberRepo, DynamoSourceRepo
from airhead.repo.sqlite import (
    SqliteEventRepo,
    SqliteMemberRepo,
    SqliteSourceRepo,
    connect,
)

TEST_TABLE = "airhead-test"
TEST_REGION = "us-east-1"


@dataclass(frozen=True)
class Repos:
    """One backend's three repositories, plus a factory for the pagination test."""

    events: EventRepo
    members: MemberRepo
    sources: SourceRepo
    backend: str
    new_event_repo: Callable[..., EventRepo]


def create_airhead_table(resource: Any, name: str = TEST_TABLE) -> Any:
    """Mirrors the Terraform table definition (PRD §7)."""
    return resource.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "GSI2",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )


@pytest.fixture
def sqlite_conn() -> Iterator[Any]:
    conn = connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def dynamo_resource(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": TEST_REGION,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=TEST_REGION)
        create_airhead_table(resource)
        yield resource


@pytest.fixture(params=["sqlite", "dynamo"])
def repos(request: pytest.FixtureRequest) -> Iterator[Repos]:
    """The contract suite runs once per backend; both must answer identically."""
    if request.param == "sqlite":
        conn = connect(":memory:")
        yield Repos(
            events=SqliteEventRepo(conn),
            members=SqliteMemberRepo(conn),
            sources=SqliteSourceRepo(conn),
            backend="sqlite",
            new_event_repo=lambda **kw: SqliteEventRepo(conn, **kw),
        )
        conn.close()
    else:
        resource = request.getfixturevalue("dynamo_resource")
        yield Repos(
            events=DynamoEventRepo(TEST_TABLE, resource=resource),
            members=DynamoMemberRepo(TEST_TABLE, resource=resource),
            sources=DynamoSourceRepo(TEST_TABLE, resource=resource),
            backend="dynamo",
            new_event_repo=lambda **kw: DynamoEventRepo(TEST_TABLE, resource=resource, **kw),
        )
