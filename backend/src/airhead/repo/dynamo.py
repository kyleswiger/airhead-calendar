"""DynamoDB-backed repositories (single table, PRD §7).

Key layout, all in one table:

    PK = HH#<hh>   SK = EVENT#<startUtc>#<eventId>    the canonical event item
                   SK = EVTID#<eventId>               pointer -> canonical SK
                   SK = RECUR#<startUtc>#<eventId>    copy of a recurring master
                   SK = MEMBER#<memberId>
                   SK = SOURCE#<sourceId>

Two of those need justifying, because the PRD's table only names the first.

*The pointer.* `EventRepo.get` is given a household and an event id, but the
sort key embeds `startUtc`, which the caller does not have. Without a pointer
the only way to answer is to read the household's whole event partition and
filter, so a two-item GetItem is bought here instead.

*The recurring-master copy.* A weekly event whose master starts two years ago
still has instances in this week's window, and a `between` on the sort key
cannot express "…or it recurs". The master is therefore mirrored under a
`RECUR#` prefix so one extra bounded query picks up every master in the
household. Only the canonical item carries GSI keys, so neither the pointer nor
the mirror ever shows up twice in an index read.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

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
from airhead.repo import decode_instant, encode_instant
from airhead.repo.base import AgendaQuery, Page, RepoError

DEFAULT_TABLE = "airhead"
DEFAULT_PAGE_SIZE = 200

# A sort-key range cannot say "ends after the window start", so an event that
# began before the window would be missed. The lower bound is widened by this
# much and `AgendaQuery.allows` throws back what does not actually overlap. It
# matches the PRD's 30-day backward expansion window; an event longer than that
# (a month-plus vacation block) is the known limit of this design.
OVERLAP_LOOKBACK = timedelta(days=30)

# Sorts after every character DynamoDB will put in a sort key, so it closes a
# `between` upper bound without excluding anything at the boundary itself.
_HIGH = "\uffff"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _translate() -> Iterator[None]:
    try:
        yield
    except ClientError as exc:  # Nothing above the repo layer may see a botocore type.
        raise RepoError(str(exc)) from exc


def _encode_cursor(key: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(key, sort_keys=True).encode()).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception as exc:
        raise RepoError(f"malformed cursor: {cursor!r}") from exc


def _pk(household_id: str) -> str:
    return f"HH#{household_id}"


def _event_sk(start_utc: str, event_id: str) -> str:
    return f"EVENT#{start_utc}#{event_id}"


def _pointer_sk(event_id: str) -> str:
    return f"EVTID#{event_id}"


def _recur_sk(event_sk: str) -> str:
    return "RECUR#" + event_sk.removeprefix("EVENT#")


class _DynamoRepo:
    def __init__(
        self,
        table_name: str | None = None,
        *,
        resource: Any = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.table_name = table_name or os.environ.get("AIRHEAD_TABLE", DEFAULT_TABLE)
        self._resource = resource
        self._clock = clock

    @cached_property
    def table(self) -> Any:
        # Built lazily so importing this module never needs credentials, and so a
        # moto mock entered after construction is the one that gets used.
        resource = self._resource or boto3.resource("dynamodb")
        return resource.Table(self.table_name)


class DynamoEventRepo(_DynamoRepo):
    def __init__(
        self,
        table_name: str | None = None,
        *,
        resource: Any = None,
        clock: Callable[[], datetime] = _utc_now,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__(table_name, resource=resource, clock=clock)
        self.page_size = page_size

    def get(self, household_id: str, event_id: str) -> Event | None:
        sk = self._resolve_sk(household_id, event_id)
        if sk is None:
            return None
        with _translate():
            item = self.table.get_item(Key={"PK": _pk(household_id), "SK": sk}).get("Item")
        return _item_to_event(item) if item else None

    def put(self, event: Event) -> Event:
        stored = replace(event, updated_at=self._clock())
        pk = _pk(stored.household_id)
        sk = _event_sk(encode_instant(stored.start_utc), stored.event_id)
        previous_sk = self._resolve_sk(stored.household_id, stored.event_id)

        item = _event_to_item(stored)
        with _translate():
            self.table.put_item(Item=item)
            self.table.put_item(
                Item={
                    "PK": pk,
                    "SK": _pointer_sk(stored.event_id),
                    "entity": "pointer",
                    "targetSk": sk,
                }
            )
            if stored.rrule:
                mirror = {k: v for k, v in item.items() if not k.startswith("GSI")}
                mirror["SK"] = _recur_sk(sk)
                mirror["entity"] = "eventRecurMirror"
                self.table.put_item(Item=mirror)

            # Moving an event's start time moves its sort key, so the write above
            # created a second item rather than replacing the first. The stale one
            # has to go or the agenda shows the event at both times forever.
            if previous_sk is not None and previous_sk != sk:
                self.table.delete_item(Key={"PK": pk, "SK": previous_sk})
                self.table.delete_item(Key={"PK": pk, "SK": _recur_sk(previous_sk)})
            elif previous_sk is not None and not stored.rrule:
                # Recurrence was removed; the mirror would otherwise linger.
                self.table.delete_item(Key={"PK": pk, "SK": _recur_sk(sk)})
        return stored

    def delete(self, household_id: str, event_id: str, *, at: datetime) -> Event | None:
        existing = self.get(household_id, event_id)
        if existing is None:
            return None
        return self.put(replace(existing, deleted_at=at))

    def list_range(self, query: AgendaQuery, *, cursor: str | None = None) -> Page:
        pk = _pk(query.household_id)
        low = encode_instant(query.start_utc - OVERLAP_LOOKBACK)
        high = encode_instant(query.end_utc)

        found: dict[str, Event] = {}
        with _translate():
            # Recurring masters are collected once, on the first page: there are
            # only ever a handful and they are not part of the paged key range.
            if cursor is None:
                for item in self._query_all(
                    KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("RECUR#")
                ):
                    event = _item_to_event(item)
                    found[event.event_id] = event

            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("PK").eq(pk)
                & Key("SK").between(f"EVENT#{low}", f"EVENT#{high}{_HIGH}"),
                "Limit": self.page_size,
            }
            if cursor is not None:
                kwargs["ExclusiveStartKey"] = _decode_cursor(cursor)
            response = self.table.query(**kwargs)

        for item in response.get("Items", []):
            event = _item_to_event(item)
            found[event.event_id] = event

        # The visibility, tier, deleted and overlap rules are applied here rather
        # than as a FilterExpression: DynamoDB filters after the read anyway, so a
        # second copy of the rule would buy nothing and eventually drift from
        # `allows`, which is the one definition the contract test pins down.
        events = sorted(
            (e for e in found.values() if query.allows(e)),
            key=lambda e: (e.start_utc, e.event_id),
        )
        last_key = response.get("LastEvaluatedKey")
        return Page(events=events, cursor=_encode_cursor(last_key) if last_key else None)

    def get_by_external_id(self, source_id: str, external_id: str) -> Event | None:
        with _translate():
            response = self.table.query(
                IndexName="GSI1",
                KeyConditionExpression=Key("GSI1PK").eq(f"SRC#{source_id}")
                & Key("GSI1SK").eq(f"EXT#{external_id}"),
                Limit=1,
            )
        items = response.get("Items", [])
        return _item_to_event(items[0]) if items else None

    def _resolve_sk(self, household_id: str, event_id: str) -> str | None:
        with _translate():
            item = self.table.get_item(
                Key={"PK": _pk(household_id), "SK": _pointer_sk(event_id)}
            ).get("Item")
        return item["targetSk"] if item else None

    def _query_all(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        while True:
            response = self.table.query(**kwargs)
            yield from response.get("Items", [])
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return
            kwargs["ExclusiveStartKey"] = last_key


class DynamoMemberRepo(_DynamoRepo):
    def get(self, household_id: str, member_id: str) -> Member | None:
        with _translate():
            item = self.table.get_item(
                Key={"PK": _pk(household_id), "SK": f"MEMBER#{member_id}"}
            ).get("Item")
        return _item_to_member(item) if item else None

    def list(self, household_id: str) -> list[Member]:
        with _translate():
            response = self.table.query(
                KeyConditionExpression=Key("PK").eq(_pk(household_id))
                & Key("SK").begins_with("MEMBER#")
            )
        # Sort key order is member_id order, which is the stable roster ordering
        # the agent's cached prompt prefix depends on.
        return [_item_to_member(i) for i in response.get("Items", [])]

    def put(self, member: Member) -> Member:
        with _translate():
            self.table.put_item(
                Item={
                    "PK": _pk(member.household_id),
                    "SK": f"MEMBER#{member.member_id}",
                    "entity": "member",
                    "householdId": member.household_id,
                    "memberId": member.member_id,
                    "displayName": member.display_name,
                    "role": member.role.value,
                    "color": member.color,
                    **({"cognitoSub": member.cognito_sub} if member.cognito_sub else {}),
                }
            )
        return member


class DynamoSourceRepo(_DynamoRepo):
    def get(self, household_id: str, source_id: str) -> Source | None:
        with _translate():
            item = self.table.get_item(
                Key={"PK": _pk(household_id), "SK": f"SOURCE#{source_id}"}
            ).get("Item")
        return _item_to_source(item) if item else None

    def list(self, household_id: str) -> list[Source]:
        with _translate():
            response = self.table.query(
                KeyConditionExpression=Key("PK").eq(_pk(household_id))
                & Key("SK").begins_with("SOURCE#")
            )
        return [_item_to_source(i) for i in response.get("Items", [])]

    def put(self, source: Source) -> Source:
        item: dict[str, Any] = {
            "PK": _pk(source.household_id),
            "SK": f"SOURCE#{source.source_id}",
            "entity": "source",
            "householdId": source.household_id,
            "sourceId": source.source_id,
            "kind": source.kind.value,
            "ownerMemberId": source.owner_member_id,
            "label": source.label,
            "enabled": source.enabled,
        }
        if source.default_tier:
            item["defaultTier"] = source.default_tier.value
        if source.cursor:
            item["syncCursor"] = source.cursor
        if source.last_sync_at:
            item["lastSyncAt"] = encode_instant(source.last_sync_at)
        with _translate():
            self.table.put_item(Item=item)
        return source


def _event_to_item(event: Event) -> dict[str, Any]:
    start_utc = encode_instant(event.start_utc)
    item: dict[str, Any] = {
        "PK": _pk(event.household_id),
        "SK": _event_sk(start_utc, event.event_id),
        "entity": "event",
        "householdId": event.household_id,
        "eventId": event.event_id,
        "title": event.title,
        "startUtc": start_utc,
        "endUtc": encode_instant(event.end_utc),
        "tz": event.tz,
        "allDay": event.all_day,
        "exdates": [encode_instant(d) for d in event.exdates],
        "ownerMemberId": event.owner_member_id,
        "involves": list(event.involves),
        "tier": event.tier.value,
        "tierSource": event.tier_source.value,
        "visibility": event.visibility.value,
        "sourceKind": event.source.kind.value,
        "contentHash": event.content_hash(),
        # GSI2 keys the per-member day slice off the owner only. An event can hold
        # one GSI2PK, so `involves` cannot be indexed without fan-out items; the
        # member filter is applied in `allows` until that is worth building.
        "GSI2PK": f"HH#{event.household_id}#MEM#{event.owner_member_id}",
        "GSI2SK": start_utc,
    }
    optional = {
        "rrule": event.rrule,
        "location": event.location,
        "sourceId": event.source.source_id,
        "externalId": event.source.external_id,
        "etag": event.source.etag,
        "mergeGroupId": event.merge_group_id,
        "recurrenceParentId": event.recurrence_parent_id,
        "recurrenceId": event.recurrence_id,
        "createdBy": event.created_by,
    }
    item.update({k: v for k, v in optional.items() if v is not None})
    if event.updated_at:
        item["updatedAt"] = encode_instant(event.updated_at)
    if event.deleted_at:
        item["deletedAt"] = encode_instant(event.deleted_at)
    if event.source.source_id and event.source.external_id:
        item["GSI1PK"] = f"SRC#{event.source.source_id}"
        item["GSI1SK"] = f"EXT#{event.source.external_id}"
    return item


def _item_to_event(item: dict[str, Any]) -> Event:
    return Event(
        event_id=item["eventId"],
        household_id=item["householdId"],
        title=item["title"],
        start_utc=decode_instant(item["startUtc"]),
        end_utc=decode_instant(item["endUtc"]),
        tz=item["tz"],
        owner_member_id=item["ownerMemberId"],
        source=EventSource(
            kind=SourceKind(item["sourceKind"]),
            source_id=item.get("sourceId"),
            external_id=item.get("externalId"),
            etag=item.get("etag"),
        ),
        all_day=bool(item.get("allDay", False)),
        rrule=item.get("rrule"),
        exdates=[decode_instant(d) for d in item.get("exdates", [])],
        involves=list(item.get("involves", [])),
        location=item.get("location"),
        tier=Tier(item["tier"]),
        tier_source=TierSource(item["tierSource"]),
        visibility=Visibility(item["visibility"]),
        merge_group_id=item.get("mergeGroupId"),
        recurrence_parent_id=item.get("recurrenceParentId"),
        recurrence_id=item.get("recurrenceId"),
        created_by=item.get("createdBy"),
        updated_at=decode_instant(item["updatedAt"]) if item.get("updatedAt") else None,
        deleted_at=decode_instant(item["deletedAt"]) if item.get("deletedAt") else None,
    )


def _item_to_member(item: dict[str, Any]) -> Member:
    return Member(
        member_id=item["memberId"],
        household_id=item["householdId"],
        display_name=item["displayName"],
        role=MemberRole(item["role"]),
        color=item["color"],
        cognito_sub=item.get("cognitoSub"),
    )


def _item_to_source(item: dict[str, Any]) -> Source:
    return Source(
        source_id=item["sourceId"],
        household_id=item["householdId"],
        kind=SourceKind(item["kind"]),
        owner_member_id=item["ownerMemberId"],
        label=item["label"],
        default_tier=Tier(item["defaultTier"]) if item.get("defaultTier") else None,
        cursor=item.get("syncCursor"),
        last_sync_at=decode_instant(item["lastSyncAt"]) if item.get("lastSyncAt") else None,
        enabled=bool(item.get("enabled", True)),
    )
