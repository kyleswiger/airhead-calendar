"""Owner-run household admin against the live table. Not deployed; nothing imports it.

    cd backend
    .venv/bin/python -m airhead.admin add-member --household hh_family \\
        --id mem_kim --name Kim --role adult --color '#7aa2f7'
    .venv/bin/python -m airhead.admin list-members --household hh_family
    .venv/bin/python -m airhead.admin move-event --from hh_1 --to hh_family \\
        --event evt_... --owner mem_kim

Real names go in as arguments, never into this repo (it is public). Runs with your own
AWS credentials; the table name comes from AIRHEAD_TABLE (default `airhead`).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import replace
from datetime import UTC, datetime

from airhead.domain import Member, MemberRole
from airhead.repo.dynamo import DynamoEventRepo, DynamoMemberRepo

_MEMBER_ID = re.compile(r"^mem_[a-z0-9_]{1,40}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _add_member(args: argparse.Namespace) -> int:
    if not _MEMBER_ID.match(args.id):
        print("--id must look like mem_<lowercase>", file=sys.stderr)
        return 2
    if not _COLOR.match(args.color):
        print("--color must be #rrggbb", file=sys.stderr)
        return 2
    member = Member(
        member_id=args.id,
        household_id=args.household,
        display_name=args.name,
        role=MemberRole(args.role),
        color=args.color,
    )
    DynamoMemberRepo().put(member)
    print(f"put {member.member_id} ({member.role.value}) in {member.household_id}")
    return 0


def _list_members(args: argparse.Namespace) -> int:
    for m in DynamoMemberRepo().list(args.household):
        print(f"{m.member_id}\t{m.role.value}\t{m.color}\t{m.display_name}")
    return 0


def _move_event(args: argparse.Namespace) -> int:
    """Copy an event into another household and tombstone the original.

    For lifting a real event someone typed into the public demo into the private
    household. Owner (and `involves`) must be re-pointed at the target roster, since
    member ids differ between households.
    """
    events, members = DynamoEventRepo(), DynamoMemberRepo()
    source = events.get(args.src, args.event)
    if source is None or source.deleted_at is not None:
        print(f"{args.event} not found in {args.src}", file=sys.stderr)
        return 1
    if members.get(args.dst, args.owner) is None:
        print(f"{args.owner} is not a member of {args.dst}", file=sys.stderr)
        return 1
    events.put(
        replace(
            source,
            household_id=args.dst,
            owner_member_id=args.owner,
            involves=[args.owner],
            created_by=args.owner,
        )
    )
    events.delete(args.src, args.event, at=datetime.now(UTC))
    print(f"moved {args.event}: {args.src} -> {args.dst} (owner {args.owner})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m airhead.admin", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add-member", help="create or replace a roster member")
    add.add_argument("--household", required=True)
    add.add_argument("--id", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--role", choices=[r.value for r in MemberRole], required=True)
    add.add_argument("--color", required=True)
    add.set_defaults(fn=_add_member)

    ls = sub.add_parser("list-members", help="print a household's roster")
    ls.add_argument("--household", required=True)
    ls.set_defaults(fn=_list_members)

    mv = sub.add_parser("move-event", help="move one event between households")
    mv.add_argument("--from", dest="src", required=True)
    mv.add_argument("--to", dest="dst", required=True)
    mv.add_argument("--event", required=True)
    mv.add_argument("--owner", required=True, help="owner member id in the target household")
    mv.set_defaults(fn=_move_event)

    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
