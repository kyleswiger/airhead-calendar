"""The agent HTTP surface (docs/M2-CONTRACT.md).

Three things this module is responsible for, none of which the model loop can be
trusted to do for itself:

1. *The actor.* It comes from the auth shim, never the body. Every tool call the
   runner makes carries it, so a request that could name its own actor would defeat
   the whole authorization layer underneath.
2. *The history.* A client sends a `conversationId`; the server loads the history off
   the stored head turn. History is the model's context, so a client that could send
   it could send instructions instead — and a conversation is bound to the member who
   started it, or a minor could inherit an adult's context and read it back out.
3. *The gate.* A pending confirmation lives on the conversation's head turn and
   nowhere else. Answering it appends a new head, which is what makes a `callId`
   single-use: it cannot be replayed, cannot authorize a different call, and cannot
   be answered by a member who was not the one asked. The answer itself is the
   `approved` boolean and the `callId` match — never the prose alongside them.

Logging follows PRD §13: turn ids, tool names and counts, never the user's message,
the reply, or a tool's detail string. Those carry event titles, which are household PII.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from airhead.api.deps import Actor, AgentRuntime, HouseholdId, Runner, Turns, Tz
from airhead.api.errors import Conflict, NotFound, UpstreamError
from airhead.api.schemas import (
    PendingConfirmationOut,
    ToolActionOut,
    TurnOut,
    TurnRequestIn,
    TurnResponse,
    TurnsResponse,
    UsageOut,
)
from airhead.repo.turns import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PENDING_STATUS,
    AgentTurn,
    PendingCall,
    ToolCall,
    TurnUsage,
)

log = logging.getLogger("airhead.api.agent")

router = APIRouter(prefix="/api/agent", tags=["agent"])


# --- serialization -----------------------------------------------------------


def _actions_out(turn: AgentTurn) -> list[ToolActionOut]:
    # A gated call performed no write, so per the contract it is not an action. It is
    # still on the stored turn: the audit log records what was attempted.
    return [
        ToolActionOut(tool=a.tool, status=a.status, event_id=a.event_id, detail=a.detail)
        for a in turn.actions
        if a.status != PENDING_STATUS
    ]


def _pending_out(pending: PendingCall | None) -> PendingConfirmationOut | None:
    if pending is None:
        return None
    return PendingConfirmationOut(
        call_id=pending.call_id,
        tool=pending.tool,
        summary=pending.summary,
        event_id=pending.event_id,
    )


def _usage_out(usage: TurnUsage) -> UsageOut:
    return UsageOut(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
    )


def _turn_response(turn: AgentTurn) -> TurnResponse:
    return TurnResponse(
        conversation_id=turn.conversation_id,
        turn_id=turn.turn_id,
        # Prose is welcome beside a gate - the display renders it above the question.
        # What may not sit beside a gate is the write itself, which `_actions_out` drops.
        reply=turn.reply,
        actions=_actions_out(turn),
        pending_confirmation=_pending_out(turn.pending),
        usage=_usage_out(turn.usage),
    )


def _turn_out(turn: AgentTurn) -> TurnOut:
    return TurnOut(
        turn_id=turn.turn_id,
        conversation_id=turn.conversation_id,
        actor_member_id=turn.actor_member_id,
        created_at=turn.created_at,
        message=turn.message,
        reply=turn.reply,
        actions=_actions_out(turn),
        pending_confirmation=_pending_out(turn.pending),
        usage=_usage_out(turn.usage),
    )


# --- routes ------------------------------------------------------------------


@router.post("/turn", response_model=TurnResponse)
def agent_turn(
    body: TurnRequestIn,
    actor: Actor,
    turns: Turns,
    runner: Runner,
    agent_deps: AgentRuntime,
    household_id: HouseholdId,
    tz: Tz,
) -> TurnResponse:
    previous: AgentTurn | None = None
    if body.conversation_id:
        previous = turns.latest(household_id, body.conversation_id)
        # "Unknown" and "someone else's" are the same 404. Which conversations exist is
        # itself household information, and the alternative - continuing a conversation
        # a client named but never started - would let anyone graft onto another
        # member's context.
        if previous is None or previous.actor_member_id != actor.member_id:
            raise NotFound("Conversation not found.")
        conversation_id = body.conversation_id
    else:
        conversation_id = f"cnv_{uuid.uuid4().hex}"

    confirmation = None
    if body.confirm is not None:
        pending = previous.pending if previous is not None else None
        # One shot. The gate exists only on the head turn, so answering it appends a
        # new head with no pending call: the same callId replayed finds nothing, and a
        # callId from an older turn cannot authorize the call that is pending now.
        if pending is None or pending.call_id != body.confirm.call_id:
            raise Conflict(
                "No pending confirmation matches that callId.", code="stale_confirmation"
            )
        # The decision is the boolean and the callId match, nothing else. `message` on a
        # confirm turn is conversational filler the display sends so the transcript
        # reads naturally ("Yes - go ahead."); it is also the field an attacker can
        # influence, and a gate that can be talked past is not a gate. It is recorded,
        # never interpreted.
        confirmation = runner.Confirmation(
            call_id=body.confirm.call_id, approved=body.confirm.approved
        )

    request = runner.TurnRequest(
        household_id=household_id,
        actor=actor,
        message=body.message,
        # The client clock reaches the model as a user-turn fact. It must not go near
        # the system prompt: a timestamp in the cached prefix means the cache never
        # hits, silently, and nothing errors to say so.
        now=body.now or datetime.now(UTC),
        tz=body.tz or tz,
        history=list(previous.history) if previous is not None else [],
        confirm=confirmation,
    )

    try:
        result = runner.run_turn(request, deps=agent_deps)
    except Exception as exc:
        # Type only. An SDK exception quotes the failed request, and the request quotes
        # event titles - so neither the message nor the traceback may reach CloudWatch.
        log.error(
            "agent_turn_failed",
            extra={
                "actor": actor.member_id,
                "conversation_id": conversation_id,
                "error_type": type(exc).__name__,
            },
        )
        raise UpstreamError("The assistant is unavailable right now.") from exc

    turn = AgentTurn(
        turn_id=f"turn_{uuid.uuid4().hex}",
        household_id=household_id,
        conversation_id=conversation_id,
        actor_member_id=actor.member_id,
        # Server time, not the client's: an audit record whose ordering a client can
        # choose is not an audit record.
        created_at=datetime.now(UTC),
        message=body.message,
        reply=result.reply,
        actions=[
            ToolCall(tool=o.tool, status=o.status, event_id=o.event_id, detail=o.detail)
            for o in result.actions
        ],
        pending=_pending_call(result.pending),
        history=list(result.history),
        usage=TurnUsage(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_input_tokens=result.usage.cache_read_input_tokens,
        ),
        confirmed_call_id=body.confirm.call_id if body.confirm is not None else None,
    )
    turns.put(turn)

    log.info(
        "agent_turn",
        extra={
            "turn_id": turn.turn_id,
            "conversation_id": conversation_id,
            "actor": actor.member_id,
            "tools": [a.tool for a in turn.actions],
            "statuses": [a.status for a in turn.actions],
            "gated": turn.pending is not None,
            "input_tokens": turn.usage.input_tokens,
            "output_tokens": turn.usage.output_tokens,
            "cache_read_input_tokens": turn.usage.cache_read_input_tokens,
        },
    )
    return _turn_response(turn)


@router.get("/turns", response_model=TurnsResponse)
def list_turns(
    actor: Actor,
    turns: Turns,
    household_id: HouseholdId,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> TurnsResponse:
    # The same visibility rule as everywhere else, applied to transcripts: an adult's
    # reply can quote an adults-only event that the query layer would never have shown
    # a minor directly, so a minor sees only their own turns.
    member_id = None if actor.is_adult else actor.member_id
    recent = turns.list_recent(household_id, member_id=member_id, limit=limit)
    return TurnsResponse(turns=[_turn_out(t) for t in recent])


def _pending_call(pending: Any) -> PendingCall | None:
    if pending is None:
        return None
    return PendingCall(
        call_id=pending.call_id,
        tool=pending.tool,
        summary=pending.summary,
        event_id=pending.event_id,
    )
