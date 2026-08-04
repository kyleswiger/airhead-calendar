"""Repository package.

Only `base` is re-exported here. The concrete backends stay unimported on
purpose: importing this package must never drag boto3 in, or a Pi-only
deployment pays for an AWS SDK it will never call.

The instant codec lives here rather than in `base` because it is a storage
concern (sort keys, text columns) that the domain-facing protocols have no
business knowing about, and because both backends need exactly the same
answer or a row written by one would be unreadable by the other.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from airhead.repo.base import (
    TIER_ORDER,
    AgendaQuery,
    EventRepo,
    MemberRepo,
    NotFound,
    Page,
    RepoError,
    SourceRepo,
)

__all__ = [
    "TIER_ORDER",
    "AgendaQuery",
    "EventRepo",
    "MemberRepo",
    "NotFound",
    "Page",
    "RepoError",
    "SourceRepo",
    "decode_instant",
    "decode_instants",
    "encode_instant",
    "encode_instants",
]

# Microseconds are always written, even when zero. The DynamoDB sort key embeds
# this string and a `between` range compares it bytewise, so every instant has
# to be the same width - "…:00Z" and "…:00.5Z" would otherwise sort in the wrong
# order ('.' < 'Z') and drop an event at the window boundary.
_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def encode_instant(value: datetime) -> str:
    # A naive datetime is read as UTC rather than rejected; everything above this
    # layer already works in UTC and a hard failure on write helps nobody.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(_ISO_FMT) + "Z"


def decode_instant(raw: str) -> datetime:
    return datetime.fromisoformat(raw).astimezone(UTC)


def encode_instants(values: list[datetime]) -> str:
    return json.dumps([encode_instant(v) for v in values])


def decode_instants(raw: str | None) -> list[datetime]:
    return [decode_instant(v) for v in json.loads(raw or "[]")]
