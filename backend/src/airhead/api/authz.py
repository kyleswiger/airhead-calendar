"""Server-side authorization rules shared by the event and routine routes.

PRD §6.2: a minor cannot change another member's events, change visibility, delete
events they did not create, or connect/disconnect sources. All server-side, because
the kiosk is a shared screen and the agent is not a trust boundary. Lives apart from
`app.py` so a sub-router can import it without a circular import.
"""

from __future__ import annotations

from airhead.api.errors import Forbidden, InvalidRequest
from airhead.domain import Event, Member
from airhead.repo.base import MemberRepo


def ensure_may_edit(actor: Member, event: Event) -> None:
    if not actor.is_adult and event.owner_member_id != actor.member_id:
        raise Forbidden("Minors may only change their own events.")


def ensure_may_delete(actor: Member, event: Event) -> None:
    if not actor.is_adult and (event.created_by or event.owner_member_id) != actor.member_id:
        raise Forbidden("Minors may only delete events they created.")


def ensure_may_set_visibility(actor: Member) -> None:
    if not actor.is_adult:
        raise Forbidden("Only adults may set event visibility.")


def validate_involves(members: MemberRepo, household_id: str, involves: list[str]) -> list[str]:
    known = {m.member_id for m in members.list(household_id)}
    if set(involves) - known:
        raise InvalidRequest("Unknown member id in `involves`.")
    return list(dict.fromkeys(involves))
