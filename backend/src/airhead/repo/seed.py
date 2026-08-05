"""Demo household seed.

Placeholder people, deliberately: this repository is public, so nothing here
uses a real name. The week is shaped to exercise every tier - a day where one
adult's work meetings must collapse to a band with a count above one, family
items that constrain more than one person, an all-day row, a recurring master
that only exists as an RRULE, and an adults-only item a minor must never be
handed. That combination is what the M1 exit criterion is checked against.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from airhead.domain import (
    Event,
    EventSource,
    Member,
    MemberRole,
    SourceKind,
    Tier,
    TierSource,
    Visibility,
)
from airhead.repo.base import EventRepo, MemberRepo

# Must match var.household_id in infra/ and the AIRHEAD_HOUSEHOLD_ID default in api/deps.py.
# A seeder that disagrees writes to a partition nothing reads, and the failure looks like an
# empty calendar rather than an error.
HOUSEHOLD_ID = "hh_1"
HOUSEHOLD_TZ = "America/New_York"

ALEX = "mem_alex"
SAM = "mem_sam"
RILEY = "mem_riley"

WORK_SOURCE_ID = "src_work_alex"

_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def roster(household_id: str = HOUSEHOLD_ID) -> list[Member]:
    return [
        Member(
            member_id=ALEX,
            household_id=household_id,
            display_name="Alex",
            role=MemberRole.ADULT,
            color="#7aa2f7",
        ),
        Member(
            member_id=SAM,
            household_id=household_id,
            display_name="Sam",
            role=MemberRole.ADULT,
            color="#9ece6a",
        ),
        Member(
            member_id=RILEY,
            household_id=household_id,
            display_name="Riley",
            role=MemberRole.MINOR,
            color="#e0af68",
        ),
    ]


def seed_household(
    event_repo: EventRepo,
    member_repo: MemberRepo,
    *,
    today: date,
    household_id: str = HOUSEHOLD_ID,
    tz: str = HOUSEHOLD_TZ,
) -> list[Event]:
    """Write the placeholder roster and a week of events. Idempotent - ids are
    derived from `today`, so re-seeding the same day replaces rather than doubles."""
    zone = ZoneInfo(tz)

    for member in roster(household_id):
        member_repo.put(member)

    def at(day: int, hour: int, minute: int = 0) -> datetime:
        # Built in household-local time and converted once, here: an event that
        # lands on the wrong side of a DST boundary is invisible until someone
        # misses a pickup.
        local = datetime.combine(today + timedelta(days=day), time(hour, minute), tzinfo=zone)
        return local.astimezone(UTC)

    def midnight(day: int) -> datetime:
        return at(day, 0)

    def work(n: int) -> EventSource:
        return EventSource(
            kind=SourceKind.GOOGLE, source_id=WORK_SOURCE_ID, external_id=f"ext_work_{n}"
        )

    native = EventSource(kind=SourceKind.NATIVE)

    specs: list[dict] = [
        # Day 0 - three work meetings for one adult, so the collapse band counts > 1.
        dict(
            title="Standup",
            start=at(0, 9),
            end=at(0, 9, 15),
            owner=ALEX,
            tier=Tier.BUSY,
            source=work(1),
        ),
        dict(
            title="Sprint review",
            start=at(0, 11),
            end=at(0, 12),
            owner=ALEX,
            tier=Tier.BUSY,
            source=work(2),
        ),
        dict(
            title="1:1 with manager",
            start=at(0, 14),
            end=at(0, 14, 30),
            owner=ALEX,
            tier=Tier.BUSY,
            source=work(3),
        ),
        dict(
            title="Soccer practice",
            start=at(0, 16),
            end=at(0, 17, 30),
            owner=RILEY,
            involves=[RILEY, SAM],
            tier=Tier.HOUSEHOLD,
            location="Riverside Park Field 3",
        ),
        # Day 1
        dict(
            title="Dentist - Riley",
            start=at(1, 8, 30),
            end=at(1, 9, 30),
            owner=RILEY,
            involves=[RILEY, ALEX],
            tier=Tier.HOUSEHOLD,
            location="Bridge Street Dental",
        ),
        dict(
            title="Gym",
            start=at(1, 6),
            end=at(1, 7),
            owner=SAM,
            tier=Tier.PERSONAL,
        ),
        # Day 2 - all-day, and a lone work meeting (band of exactly one).
        dict(
            title="No school - teacher inservice",
            start=midnight(2),
            end=midnight(3),
            owner=RILEY,
            involves=[RILEY, ALEX, SAM],
            tier=Tier.HOUSEHOLD,
            all_day=True,
        ),
        dict(
            title="Quarterly planning",
            start=at(2, 10),
            end=at(2, 12),
            owner=ALEX,
            tier=Tier.BUSY,
            source=work(4),
        ),
        # Day 3 - the recurring master, stored as an RRULE and never expanded here.
        dict(
            title="Trash and recycling out",
            start=at(3, 20),
            end=at(3, 20, 15),
            owner=ALEX,
            involves=[ALEX, RILEY],
            tier=Tier.HOUSEHOLD,
            rrule=f"FREQ=WEEKLY;BYDAY={_WEEKDAY_CODES[(today + timedelta(days=3)).weekday()]}",
        ),
        # The one a minor must never be handed.
        dict(
            title="Riley birthday surprise planning",
            start=at(3, 21),
            end=at(3, 21, 45),
            owner=ALEX,
            involves=[ALEX, SAM],
            tier=Tier.PERSONAL,
            visibility=Visibility.ADULTS,
        ),
        # Day 4
        dict(
            title="Client demo",
            start=at(4, 13),
            end=at(4, 14),
            owner=ALEX,
            tier=Tier.BUSY,
            source=work(5),
        ),
        dict(
            title="Dinner at Grandma's",
            start=at(4, 18),
            end=at(4, 20, 30),
            owner=SAM,
            involves=[SAM, ALEX, RILEY],
            tier=Tier.HOUSEHOLD,
            location="41 Cedar Lane",
        ),
        # Day 5
        dict(
            title="Book club",
            start=at(5, 19),
            end=at(5, 21),
            owner=SAM,
            tier=Tier.PERSONAL,
        ),
        # Day 6
        dict(
            title="Soccer game vs Eastside",
            start=at(6, 10),
            end=at(6, 11, 30),
            owner=RILEY,
            involves=[RILEY, ALEX, SAM],
            tier=Tier.HOUSEHOLD,
            location="Eastside Athletic Complex",
        ),
    ]

    stored: list[Event] = []
    for index, spec in enumerate(specs, start=1):
        event = Event(
            event_id=f"evt_seed_{today.isoformat()}_{index:02d}",
            household_id=household_id,
            title=spec["title"],
            start_utc=spec["start"],
            end_utc=spec["end"],
            tz=tz,
            owner_member_id=spec["owner"],
            source=spec.get("source", native),
            all_day=spec.get("all_day", False),
            rrule=spec.get("rrule"),
            involves=spec.get("involves", [spec["owner"]]),
            location=spec.get("location"),
            tier=spec["tier"],
            tier_source=TierSource.AUTO,
            visibility=spec.get("visibility", Visibility.ALL),
            created_by=spec["owner"],
        )
        stored.append(event_repo.put(event))
    return stored
