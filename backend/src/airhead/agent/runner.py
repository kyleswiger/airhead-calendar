"""One conversational turn.

Drives the SDK's beta tool runner, mirrors the conversation so the caller can
persist it, and reports what actually happened to the calendar. The interesting
behaviour is deliberately *not* here — visibility, authorization and the
confirmation gate all live in `tools.py`, where the model cannot reach them.

Sonnet 4.6 specifics worth remembering: `max_tokens` caps thinking plus
response text when thinking is on, so 16k is a floor not a ceiling;
`budget_tokens` is deprecated; and depth is `output_config.effort`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from airhead.agent.prompt import build_system, user_turn
from airhead.agent.tools import (
    Confirmation,
    PendingConfirmation,
    ToolContext,
    ToolOutcome,
    build_tools,
    settle_confirmation,
)
from airhead.domain import Member
from airhead.repo.base import EventRepo, MemberRepo

__all__ = [
    "AgentDeps",
    "Confirmation",
    "PendingConfirmation",
    "ToolOutcome",
    "TurnRequest",
    "TurnResult",
    "Usage",
    "run_turn",
]

log = logging.getLogger("airhead.agent")

# A conversational turn that needs more round trips than this has gone wrong;
# the cap bounds the blast radius of a loop rather than expressing a real limit.
MAX_ITERATIONS = 8

REFUSAL_REPLY = "Sorry — I can't help with that one."
EMPTY_REPLY = "Sorry, I didn't catch that. Could you say it another way?"


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int


@dataclass(frozen=True, slots=True)
class TurnRequest:
    household_id: str
    actor: Member
    message: str
    now: datetime
    tz: str
    history: list[dict] = field(default_factory=list)
    confirm: Confirmation | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    reply: str
    actions: tuple[ToolOutcome, ...]
    pending: PendingConfirmation | None
    history: list[dict]
    usage: Usage


@dataclass(frozen=True, slots=True)
class AgentDeps:
    events: EventRepo
    members: MemberRepo
    # An Anthropic client (legacy AnthropicBedrock in prod); injected so tests can fake it.
    client: Any
    model: str = "us.anthropic.claude-sonnet-4-6"
    effort: str = "medium"
    # Caps thinking *plus* visible text with thinking enabled. Sized for the thinking, not
    # for the two sentences the kitchen screen shows.
    max_tokens: int = 16000


def run_turn(request: TurnRequest, *, deps: AgentDeps) -> TurnResult:
    roster = deps.members.list(request.household_id)
    ctx = ToolContext(
        household_id=request.household_id,
        actor=request.actor,
        events=deps.events,
        members=deps.members,
        now=request.now,
        tz=request.tz,
        confirm=request.confirm,
    )

    # An answered gate is settled *before* the model runs: the harness replays
    # an approved write itself, with the arguments that were approved, so the
    # write no longer depends on the model choosing to re-issue the call. The
    # model is told the outcome below and only narrates it.
    settled = settle_confirmation(ctx)

    messages: list[dict] = [
        *request.history,
        {
            "role": "user",
            "content": user_turn(
                message=request.message,
                now=request.now,
                tz=request.tz,
                actor=request.actor,
                confirmation=_confirmation_note(request.confirm, settled),
            ),
        },
    ]

    runner = _tool_runner(
        deps.client,
        model=deps.model,
        max_tokens=deps.max_tokens,
        # Roster only — no clock, no actor. Everything volatile is in the user
        # turn above, which renders after the cache breakpoint.
        system=build_system(roster),
        tools=build_tools(ctx),
        messages=messages,
        output_config={"effort": deps.effort},
        max_iterations=MAX_ITERATIONS,
    )

    history = list(messages)
    totals = [0, 0, 0]
    last: Any = None

    for message in runner:
        last = message
        _add_usage(totals, getattr(message, "usage", None))
        # The runner keeps its own history and does not expose it, so mirror it
        # here; `generate_tool_call_response` is cached, so the tools still run
        # exactly once per turn.
        history.append({"role": "assistant", "content": message.content})
        if getattr(message, "stop_reason", None) == "refusal":
            break
        response = runner.generate_tool_call_response()
        if response is not None:
            history.append(response)

    result = TurnResult(
        reply=_reply(last),
        actions=tuple(ctx.outcomes),
        pending=ctx.pending,
        history=history,
        usage=Usage(*totals),
    )
    # Event titles are household PII (PRD §13) — ids and tool names only.
    log.info(
        "agent_turn",
        extra={
            "actor": request.actor.member_id,
            "tools": [o.tool for o in result.actions],
            "statuses": [o.status for o in result.actions],
            "pending": result.pending.tool if result.pending else None,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cache_read_input_tokens": result.usage.cache_read_input_tokens,
        },
    )
    return result


def _tool_runner(client: Any, **kwargs: Any) -> Any:
    """The SDK's beta tool runner, on whatever client we were handed.

    The first-party and Mantle clients expose `beta.messages.tool_runner`
    directly. The legacy `AnthropicBedrock` client (the bedrock-runtime
    InvokeModel path — the one that actually works on this AWS account) does
    not, but its beta messages resource already borrows the first-party
    `create`, which is all the runner calls — so borrow the first-party
    `tool_runner` the same way and bind it to the Bedrock resource. Test fakes
    define `tool_runner` themselves and take the first branch.
    """
    messages = client.beta.messages
    if hasattr(messages, "tool_runner"):
        return messages.tool_runner(**kwargs)
    from anthropic.resources.beta.messages.messages import Messages as _FirstPartyBetaMessages

    # The runner's request path is `beta.messages.parse`, which — like the
    # borrowed `create` — posts to /v1/messages and rides the Bedrock client's
    # URL mapping to /model/{id}/invoke. Bind it once on the cached resource.
    if not hasattr(messages, "parse"):
        messages.parse = _FirstPartyBetaMessages.parse.__get__(messages)
    return _FirstPartyBetaMessages.tool_runner(messages, **kwargs)


def _confirmation_note(confirm: Confirmation | None, settled: ToolOutcome | None) -> str | None:
    """Tell the model the gate was answered and what the harness did about it.

    This is narration for the conversation only — the authoritative copy of the
    answer is on `ToolContext`, and by the time the model reads this the write
    (or its refusal) has already happened in `settle_confirmation`.
    """
    if confirm is None:
        return None
    verdict = "approved" if confirm.approved else "declined"
    note = f"the person {verdict} the pending request {confirm.call_id}"
    if settled is None:
        return note
    if settled.status == "ok":
        return (
            f"{note}; the system has already applied it. Do not call the tool "
            "again — just tell them it is done."
        )
    if settled.detail == "declined":
        return f"{note}; nothing was changed. Do not call the tool again."
    return (
        f"{note}; the system tried to apply it but the change failed "
        f"({settled.detail or 'error'}). Do not retry the call — tell them it did not work."
    )


def _reply(message: Any) -> str:
    """The visible answer.

    `stop_reason` is checked before `content` on purpose: Opus 5 can refuse with
    an empty content list, and indexing into that raises.
    """
    if message is None:
        return EMPTY_REPLY
    if getattr(message, "stop_reason", None) == "refusal":
        return REFUSAL_REPLY
    parts = [
        block.text
        for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text" and getattr(block, "text", "")
    ]
    return "\n".join(parts).strip() or EMPTY_REPLY


def _add_usage(totals: list[int], usage: Any) -> None:
    if usage is None:
        return
    totals[0] += getattr(usage, "input_tokens", 0) or 0
    totals[1] += getattr(usage, "output_tokens", 0) or 0
    totals[2] += getattr(usage, "cache_read_input_tokens", 0) or 0
