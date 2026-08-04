"""DynamoDB specifics: key layout, cursors, and the item bookkeeping the
single-table design forces on us. The behavioural contract lives in
test_repo_contract.py and runs against this backend too."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import boto3
import pytest
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from moto import mock_aws

from airhead.domain import Event, EventSource, SourceKind, Tier, Visibility
from airhead.repo.base import AgendaQuery, RepoError
from airhead.repo.dynamo import DEFAULT_TABLE, DynamoEventRepo, DynamoMemberRepo
from conftest import TEST_REGION, TEST_TABLE, create_airhead_table

HH = "hh_1"
WINDOW = {
    "household_id": HH,
    "start_utc": datetime(2026, 8, 3, tzinfo=UTC),
    "end_utc": datetime(2026, 8, 10, tzinfo=UTC),
}


def make_event(**overrides) -> Event:
    base = {
        "event_id": "evt_1",
        "household_id": HH,
        "title": "Soccer practice",
        "start_utc": datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
        "tz": "America/New_York",
        "owner_member_id": "mem_riley",
        "source": EventSource(kind=SourceKind.GOOGLE, source_id="src_1", external_id="ext_1"),
    }
    return Event(**{**base, **overrides})


@pytest.fixture
def repo(dynamo_resource):
    return DynamoEventRepo(TEST_TABLE, resource=dynamo_resource)


def scan_sks(resource, prefix: str = "") -> list[str]:
    items = resource.Table(TEST_TABLE).scan()["Items"]
    return sorted(i["SK"] for i in items if i["SK"].startswith(prefix))


class TestKeyLayout:
    def test_keys_match_the_prd(self, repo, dynamo_resource):
        repo.put(make_event(owner_member_id="mem_riley"))

        item = dynamo_resource.Table(TEST_TABLE).get_item(
            Key={
                "PK": f"HH#{HH}",
                "SK": "EVENT#2026-08-06T20:00:00.000000Z#evt_1",
            }
        )["Item"]

        assert item["PK"] == "HH#hh_1"
        assert item["GSI1PK"] == "SRC#src_1"
        assert item["GSI1SK"] == "EXT#ext_1"
        assert item["GSI2PK"] == "HH#hh_1#MEM#mem_riley"
        assert item["GSI2SK"] == "2026-08-06T20:00:00.000000Z"

    def test_a_native_event_carries_no_gsi1_keys(self, repo, dynamo_resource):
        """GSI1 is the external-identity index; a locally created event has none."""
        repo.put(make_event(source=EventSource(kind=SourceKind.NATIVE)))

        item = dynamo_resource.Table(TEST_TABLE).scan(FilterExpression=Attr("entity").eq("event"))[
            "Items"
        ][0]

        assert "GSI1PK" not in item

    def test_only_the_canonical_item_is_indexed(self, repo, dynamo_resource):
        """A mirror or pointer that also carried GSI keys would return the same
        event twice from one index read."""
        repo.put(make_event(rrule="FREQ=WEEKLY;BYDAY=TH"))

        indexed = dynamo_resource.Table(TEST_TABLE).query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq("SRC#src_1"),
        )["Items"]

        assert len(indexed) == 1

    def test_a_pointer_item_resolves_get_by_id(self, repo, dynamo_resource):
        repo.put(make_event())

        pointer = dynamo_resource.Table(TEST_TABLE).get_item(
            Key={"PK": f"HH#{HH}", "SK": "EVTID#evt_1"}
        )["Item"]

        assert pointer["targetSk"] == "EVENT#2026-08-06T20:00:00.000000Z#evt_1"


class TestMovingAnEvent:
    def test_moving_the_start_leaves_no_orphan(self, repo, dynamo_resource):
        """The sort key embeds startUtc, so a reschedule writes a *new* item;
        without the compensating delete the event shows at both times forever."""
        repo.put(make_event())
        repo.put(
            make_event(
                start_utc=datetime(2026, 8, 7, 20, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 7, 21, 30, tzinfo=UTC),
            )
        )

        assert scan_sks(dynamo_resource, "EVENT#") == ["EVENT#2026-08-07T20:00:00.000000Z#evt_1"]
        page = repo.list_range(AgendaQuery(**WINDOW))
        assert [e.start_utc for e in page.events] == [datetime(2026, 8, 7, 20, 0, tzinfo=UTC)]

    def test_moving_the_start_updates_the_pointer(self, repo):
        repo.put(make_event())
        repo.put(
            make_event(
                start_utc=datetime(2026, 8, 7, 20, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 7, 21, 30, tzinfo=UTC),
            )
        )

        assert repo.get(HH, "evt_1").start_utc == datetime(2026, 8, 7, 20, 0, tzinfo=UTC)

    def test_moving_a_recurring_master_leaves_no_orphan_mirror(self, repo, dynamo_resource):
        repo.put(make_event(rrule="FREQ=WEEKLY;BYDAY=TH"))
        repo.put(
            make_event(
                rrule="FREQ=WEEKLY;BYDAY=FR",
                start_utc=datetime(2026, 8, 7, 20, 0, tzinfo=UTC),
                end_utc=datetime(2026, 8, 7, 21, 30, tzinfo=UTC),
            )
        )

        assert scan_sks(dynamo_resource, "RECUR#") == ["RECUR#2026-08-07T20:00:00.000000Z#evt_1"]
        assert len(repo.list_range(AgendaQuery(**WINDOW)).events) == 1

    def test_dropping_the_rrule_removes_the_mirror(self, repo, dynamo_resource):
        repo.put(make_event(rrule="FREQ=WEEKLY;BYDAY=TH"))
        repo.put(make_event(rrule=None))

        assert scan_sks(dynamo_resource, "RECUR#") == []

    def test_a_master_outside_the_window_survives_via_the_mirror(self, repo):
        """The `between` on the sort key cannot express "…or it recurs"."""
        repo.put(
            make_event(
                start_utc=datetime(2024, 1, 4, 20, 0, tzinfo=UTC),
                end_utc=datetime(2024, 1, 4, 21, 0, tzinfo=UTC),
                rrule="FREQ=WEEKLY;BYDAY=TH",
            )
        )

        assert [e.event_id for e in repo.list_range(AgendaQuery(**WINDOW)).events] == ["evt_1"]

    def test_a_master_inside_the_window_is_returned_once(self, repo):
        repo.put(make_event(rrule="FREQ=WEEKLY;BYDAY=TH"))

        assert len(repo.list_range(AgendaQuery(**WINDOW)).events) == 1


class TestSoftDeleteItems:
    def test_a_tombstone_keeps_its_key(self, repo, dynamo_resource):
        repo.put(make_event())
        repo.delete(HH, "evt_1", at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC))

        assert scan_sks(dynamo_resource, "EVENT#") == ["EVENT#2026-08-06T20:00:00.000000Z#evt_1"]
        assert repo.get(HH, "evt_1").is_deleted


class TestCursor:
    def test_is_opaque_base64_json(self, dynamo_resource):
        repo = DynamoEventRepo(TEST_TABLE, resource=dynamo_resource, page_size=1)
        for hour in (9, 10):
            repo.put(
                make_event(
                    event_id=f"evt_{hour}",
                    start_utc=datetime(2026, 8, 4, hour, tzinfo=UTC),
                    end_utc=datetime(2026, 8, 4, hour, 30, tzinfo=UTC),
                )
            )

        page = repo.list_range(AgendaQuery(**WINDOW))

        assert page.cursor is not None
        decoded = json.loads(base64.urlsafe_b64decode(page.cursor.encode()))
        assert decoded["PK"] == "HH#hh_1"

    def test_a_malformed_cursor_is_a_repo_error(self, repo):
        with pytest.raises(RepoError):
            repo.list_range(AgendaQuery(**WINDOW), cursor="not-a-cursor")


class TestErrorTranslation:
    def test_a_missing_table_raises_repo_error_not_client_error(self, dynamo_resource):
        repo = DynamoEventRepo("table-that-does-not-exist", resource=dynamo_resource)

        with pytest.raises(RepoError):
            repo.get(HH, "evt_1")

    def test_the_original_client_error_is_chained(self, dynamo_resource):
        repo = DynamoEventRepo("table-that-does-not-exist", resource=dynamo_resource)

        with pytest.raises(RepoError) as excinfo:
            repo.put(make_event())

        assert isinstance(excinfo.value.__cause__, ClientError)


class TestTableName:
    def test_defaults_to_the_env_var(self, monkeypatch):
        monkeypatch.setenv("AIRHEAD_TABLE", "airhead-prod")

        assert DynamoEventRepo().table_name == "airhead-prod"

    def test_falls_back_to_the_project_default(self, monkeypatch):
        monkeypatch.delenv("AIRHEAD_TABLE", raising=False)

        assert DynamoMemberRepo().table_name == DEFAULT_TABLE

    def test_an_explicit_name_wins(self, monkeypatch):
        monkeypatch.setenv("AIRHEAD_TABLE", "airhead-prod")

        assert DynamoEventRepo("airhead-dev").table_name == "airhead-dev"


class TestVisibilityUnderPagination:
    def test_an_adults_event_is_filtered_on_every_page(self, dynamo_resource):
        """S5 again, this time with the filter applied per page rather than once."""
        repo = DynamoEventRepo(TEST_TABLE, resource=dynamo_resource, page_size=1)
        for hour in range(4):
            repo.put(
                make_event(
                    event_id=f"evt_{hour}",
                    start_utc=datetime(2026, 8, 4, 9 + hour, tzinfo=UTC),
                    end_utc=datetime(2026, 8, 4, 9 + hour, 30, tzinfo=UTC),
                    visibility=Visibility.ADULTS if hour % 2 else Visibility.ALL,
                    tier=Tier.HOUSEHOLD,
                )
            )

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = repo.list_range(AgendaQuery(**WINDOW), cursor=cursor)
            seen += [e.event_id for e in page.events]
            cursor = page.cursor
            if cursor is None:
                break

        assert seen == ["evt_0", "evt_2"]


def test_the_table_helper_matches_the_specced_indexes():
    """Guards the fixture itself: if the TF schema drifts, these tests should
    stop passing rather than quietly testing a table nobody deploys."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=TEST_REGION)
        table = create_airhead_table(resource, "airhead-schema-check")

        indexes = {i["IndexName"] for i in table.global_secondary_indexes}

        assert indexes == {"GSI1", "GSI2"}
        assert [k["AttributeName"] for k in table.key_schema] == ["PK", "SK"]
