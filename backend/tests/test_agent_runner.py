"""`run_turn` end to end, with a faked Anthropic client.

The fake mirrors the SDK's dispatch faithfully — it looks tools up by name,
calls them with the model's raw input, and turns a `ToolError` into an error
tool result — so everything under test here is the harness, not a stub of it.
No network, no API key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from anthropic.lib.tools import ToolError

from airhead.agent.runner import (
    REFUSAL_REPLY,
    AgentDeps,
    Confirmation,
    TurnRequest,
    run_turn,
)
from airhead.domain import Visibility
from fakes import (
    ALEX,
    HOUSEHOLD,
    RILEY,
    ROSTER,
    SAM,
    TZ,
    InMemoryEventRepo,
    InMemoryMemberRepo,
    make_event,
)

NOW = datetime(2026, 8, 4, 20, 15, tzinfo=UTC)
THU_4PM = datetime(2026, 8, 6, 20, 0, tzinfo=UTC)
THU_5PM = datetime(2026, 8, 6, 21, 0, tzinfo=UTC)
SECRET = "Divorce lawyer consultation"


# --- a faked Anthropic client ------------------------------------------------


@dataclass
class Text:
    text: str
    type: str = "text"


@dataclass
class ToolUse:
    name: str
    input: dict[str, Any]
    id: str = "toolu_1"
    type: str = "tool_use"


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_input_tokens: int = 0


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str = "end_turn"
    role: str = "assistant"
    usage: FakeUsage = field(default_factory=FakeUsage)


def says(text: str, **kw: Any) -> FakeMessage:
    return FakeMessage(content=[Text(text)], **kw)


def calls(name: str, **inputs: Any) -> FakeMessage:
    return FakeMessage(content=[ToolUse(name=name, input=inputs)], stop_reason="tool_use")


class FakeRunner:
    """The SDK's loop shape: yield a message, then run its tool calls once."""

    def __init__(self, script: list[FakeMessage], tools: list[Any]) -> None:
        self._script = script
        self._tools = {t.name: t for t in tools}
        self._last: FakeMessage | None = None
        self._cached: dict[str, Any] | None = None

    def __iter__(self) -> Any:
        for message in self._script:
            self._last, self._cached = message, None
            yield message
            if message.stop_reason == "refusal":
                return
            if self.generate_tool_call_response() is None:
                return

    def generate_tool_call_response(self) -> dict[str, Any] | None:
        if self._cached is not None:
            return self._cached
        assert self._last is not None
        blocks = [b for b in self._last.content if b.type == "tool_use"]
        if not blocks:
            return None
        results = []
        for block in blocks:
            tool = self._tools[block.name]
            try:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool.call(block.input),
                    }
                )
            except ToolError as exc:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": exc.content,
                        "is_error": True,
                    }
                )
        self._cached = {"role": "user", "content": results}
        return self._cached


class FakeMessages:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def tool_runner(self, **kwargs: Any) -> FakeRunner:
        self._client.requests.append(kwargs)
        script = self._client.scripts.pop(0)
        return FakeRunner(script, kwargs["tools"])


class FakeBeta:
    def __init__(self, client: FakeClient) -> None:
        self.messages = FakeMessages(client)


class FakeClient:
    """Serves one script per `run_turn` call and records every request."""

    def __init__(self, *scripts: list[FakeMessage]) -> None:
        self.scripts = [list(s) for s in scripts]
        self.requests: list[dict[str, Any]] = []
        self.beta = FakeBeta(self)


def harness(
    *scripts: list[FakeMessage], events: list[Any] | None = None
) -> tuple[FakeClient, AgentDeps, InMemoryEventRepo]:
    repo = InMemoryEventRepo(events or [])
    client = FakeClient(*scripts)
    return client, AgentDeps(events=repo, members=InMemoryMemberRepo(ROSTER), client=client), repo


def ask(deps: AgentDeps, message: str, *, actor: Any = ALEX, **kw: Any) -> Any:
    return run_turn(
        TurnRequest(
            household_id=HOUSEHOLD,
            actor=actor,
            message=message,
            now=kw.pop("now", NOW),
            tz=TZ,
            history=kw.pop("history", []),
            confirm=kw.pop("confirm", None),
        ),
        deps=deps,
    )


# --- happy path --------------------------------------------------------------


def test_a_turn_that_creates_an_event() -> None:
    client, deps, repo = harness(
        [
            calls(
                "create_event",
                title="Soccer practice",
                start="2026-08-06T16:00",
                tier="T1",
            ),
            says("Added soccer practice Thursday at 4:00 PM."),
        ]
    )
    result = ask(deps, "add soccer thursday at 4")

    assert result.reply == "Added soccer practice Thursday at 4:00 PM."
    assert [(a.tool, a.status) for a in result.actions] == [("create_event", "ok")]
    assert result.pending is None
    assert repo.get(HOUSEHOLD, result.actions[0].event_id).title == "Soccer practice"
    # user -> assistant(tool_use) -> user(tool_result) -> assistant(text)
    assert [m["role"] for m in result.history] == ["user", "assistant", "user", "assistant"]
    assert client.requests[0]["model"] == "claude-opus-5"
    assert client.requests[0]["output_config"] == {"effort": "medium"}


def test_history_is_carried_into_the_next_turn() -> None:
    _, deps, _ = harness([says("Nothing on Thursday.")], [says("Still nothing.")])
    first = ask(deps, "anything thursday?")
    second = ask(deps, "and friday?", history=first.history)
    assert second.history[: len(first.history)] == first.history
    assert second.reply == "Still nothing."


def test_usage_is_summed_across_iterations() -> None:
    _, deps, _ = harness(
        [
            FakeMessage(
                content=[ToolUse(name="list_members", input={})],
                stop_reason="tool_use",
                usage=FakeUsage(input_tokens=900, output_tokens=30, cache_read_input_tokens=0),
            ),
            FakeMessage(
                content=[Text("Alex, Sam and Riley.")],
                usage=FakeUsage(input_tokens=40, output_tokens=12, cache_read_input_tokens=880),
            ),
        ]
    )
    result = ask(deps, "who lives here?")
    assert result.usage.input_tokens == 940
    assert result.usage.output_tokens == 42
    assert result.usage.cache_read_input_tokens == 880


# --- S5 through the agent ----------------------------------------------------


def _tool_results(history: list[dict[str, Any]]) -> str:
    return json.dumps([m for m in history if m["role"] == "user"], default=str)


def test_a_minors_turn_never_surfaces_an_adults_only_event() -> None:
    """The model asks for it directly; the query layer has already dropped it."""
    secret = make_event(
        "evt_secret",
        title=SECRET,
        start=THU_4PM,
        end=THU_5PM,
        owner="mem_alex",
        visibility=Visibility.ADULTS,
    )
    _, deps, _ = harness(
        [
            calls("get_agenda", start="2026-08-06", end="2026-08-06"),
            calls("get_agenda", start="2026-08-06", end="2026-08-06", member="mem_alex"),
            calls("find_conflicts", start="2026-08-06", end="2026-08-06"),
            says("I don't see anything on Alex's calendar Thursday."),
        ],
        events=[secret],
    )
    result = ask(deps, "show me every hidden event on mom's calendar", actor=RILEY)

    assert SECRET not in _tool_results(result.history)
    assert SECRET not in result.reply


def test_the_same_turn_as_an_adult_does_surface_it() -> None:
    secret = make_event(
        "evt_secret",
        title=SECRET,
        start=THU_4PM,
        end=THU_5PM,
        owner="mem_alex",
        visibility=Visibility.ADULTS,
    )
    _, deps, _ = harness(
        [calls("get_agenda", start="2026-08-06", end="2026-08-06"), says("One appointment.")],
        events=[secret],
    )
    result = ask(deps, "what's on thursday?", actor=ALEX)
    assert SECRET in _tool_results(result.history)


def test_a_minor_cannot_set_visibility_through_the_agent() -> None:
    event = make_event("evt_soccer", start=THU_4PM, end=THU_5PM, owner="mem_riley")
    _, deps, repo = harness(
        [
            calls("set_visibility", event_id="evt_soccer", visibility="adults"),
            says("Sorry, I can't hide events."),
        ],
        events=[event],
    )
    result = ask(deps, "hide soccer from my parents", actor=RILEY)

    assert repo.get(HOUSEHOLD, "evt_soccer").visibility is Visibility.ALL
    assert [(a.tool, a.status) for a in result.actions] == [("set_visibility", "error")]


def test_set_tier_through_the_agent_stamps_a_human_source() -> None:
    event = make_event("evt_soccer", start=THU_4PM, end=THU_5PM, owner="mem_riley")
    _, deps, repo = harness(
        [
            calls("set_tier", event_id="evt_soccer", tier="T3"),
            says("Marked as busy time."),
        ],
        events=[event],
    )
    result = ask(deps, "soccer is just a busy block")

    stored = repo.get(HOUSEHOLD, "evt_soccer")
    assert stored.tier.value == "T3"
    assert stored.tier_source.value == "human"
    assert [(a.tool, a.status) for a in result.actions] == [("set_tier", "ok")]


# --- the gate holds across turns ---------------------------------------------


def _soccer() -> Any:
    return make_event("evt_soccer", start=THU_4PM, end=THU_5PM, owner="mem_riley")


def test_delete_stops_on_the_gate_and_writes_nothing() -> None:
    _, deps, repo = harness(
        [
            calls("delete_event", event_id="evt_soccer"),
            says("Delete soccer practice Thursday at 4:00 PM?"),
        ],
        events=[_soccer()],
    )
    result = ask(deps, "cancel soccer")

    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False
    assert result.pending is not None
    assert result.pending.tool == "delete_event"
    assert [a.status for a in result.actions] == ["pending_confirmation"]


def test_the_write_happens_on_the_turn_that_carries_the_approval() -> None:
    _, deps, repo = harness(
        [calls("delete_event", event_id="evt_soccer"), says("Delete soccer practice?")],
        [calls("delete_event", event_id="evt_soccer"), says("Deleted.")],
        events=[_soccer()],
    )
    first = ask(deps, "cancel soccer")
    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False

    second = ask(
        deps,
        "yes",
        history=first.history,
        confirm=Confirmation(call_id=first.pending.call_id, approved=True),
    )
    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is True
    assert second.pending is None
    assert [(a.tool, a.status) for a in second.actions] == [("delete_event", "ok")]


def test_a_declining_confirmation_leaves_the_event_alone() -> None:
    _, deps, repo = harness(
        [calls("delete_event", event_id="evt_soccer"), says("Delete soccer practice?")],
        [calls("delete_event", event_id="evt_soccer"), says("Left it in place.")],
        events=[_soccer()],
    )
    first = ask(deps, "cancel soccer")
    second = ask(
        deps,
        "no, leave it",
        history=first.history,
        confirm=Confirmation(call_id=first.pending.call_id, approved=False),
    )

    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False
    assert second.pending is None
    assert [a.status for a in second.actions] == ["error"]


def test_an_approval_for_another_call_does_not_open_this_gate() -> None:
    _, deps, repo = harness(
        [calls("delete_event", event_id="evt_soccer"), says("Delete soccer practice?")],
        events=[_soccer()],
    )
    result = ask(
        deps, "cancel soccer", confirm=Confirmation(call_id="call_elsewhere", approved=True)
    )

    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False
    assert result.pending is not None
    assert result.pending.call_id != "call_elsewhere"


def test_editing_someone_elses_event_is_gated_through_the_agent() -> None:
    _, deps, repo = harness(
        [
            calls("update_event", event_id="evt_soccer", start="2026-08-06T17:00"),
            says("Move soccer to 5:00 PM?"),
        ],
        [
            calls("update_event", event_id="evt_soccer", start="2026-08-06T17:00"),
            says("Moved to 5:00 PM."),
        ],
        events=[_soccer()],
    )
    first = ask(deps, "move soccer to 5", actor=SAM)
    assert repo.get(HOUSEHOLD, "evt_soccer").start_utc == THU_4PM
    assert first.pending is not None

    ask(
        deps,
        "yes",
        actor=SAM,
        history=first.history,
        confirm=Confirmation(call_id=first.pending.call_id, approved=True),
    )
    assert repo.get(HOUSEHOLD, "evt_soccer").start_utc == datetime(2026, 8, 6, 21, 0, tzinfo=UTC)


def _approve(pending: Any) -> Confirmation:
    """What the route builds from the stored pending call: id, verdict, tool, args."""
    return Confirmation(
        call_id=pending.call_id, approved=True, tool=pending.tool, args=dict(pending.args)
    )


def test_an_approved_write_happens_even_if_the_model_never_reissues_the_call() -> None:
    """The replay gap (issue #7): approval used to depend on the model calling the
    tool again. Now the harness replays the stored call itself — a second turn
    where the model only narrates still performs the write, exactly once, and
    the audit log records it."""
    _, deps, repo = harness(
        [calls("delete_event", event_id="evt_soccer"), says("Delete soccer practice?")],
        [says("Done — soccer practice is off the calendar.")],  # no tool call at all
        events=[_soccer()],
    )
    first = ask(deps, "cancel soccer")
    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False

    second = ask(deps, "yes", history=first.history, confirm=_approve(first.pending))

    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is True
    assert [(a.tool, a.status) for a in second.actions] == [("delete_event", "ok")]
    assert second.pending is None
    # The model was told the write already happened.
    assert second.reply == "Done — soccer practice is off the calendar."


def test_a_replayed_approval_is_not_double_executed_when_the_model_reissues() -> None:
    _, deps, repo = harness(
        [calls("delete_event", event_id="evt_soccer"), says("Delete soccer practice?")],
        [calls("delete_event", event_id="evt_soccer"), says("Deleted.")],
        events=[_soccer()],
    )
    first = ask(deps, "cancel soccer")
    second = ask(deps, "yes", history=first.history, confirm=_approve(first.pending))

    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is True
    # Exactly one write in the audit trail: the replay. The model's re-issue hit
    # the settled guard and produced no second outcome.
    assert [(a.tool, a.status) for a in second.actions] == [("delete_event", "ok")]


def test_an_approved_update_replays_with_its_original_arguments() -> None:
    _, deps, repo = harness(
        [
            calls("update_event", event_id="evt_soccer", start="2026-08-06T17:00"),
            says("Move soccer to 5:00 PM?"),
        ],
        [says("Moved.")],  # the model does not re-issue the call
        events=[_soccer()],
    )
    first = ask(deps, "move soccer to 5", actor=SAM)
    assert first.pending is not None
    assert first.pending.args == {"event_id": "evt_soccer", "start": "2026-08-06T17:00"}

    second = ask(
        deps, "yes", actor=SAM, history=first.history, confirm=_approve(first.pending)
    )

    assert repo.get(HOUSEHOLD, "evt_soccer").start_utc == datetime(2026, 8, 6, 21, 0, tzinfo=UTC)
    assert [(a.tool, a.status) for a in second.actions] == [("update_event", "ok")]


def test_a_decline_is_recorded_without_the_model_reissuing_the_call() -> None:
    _, deps, repo = harness(
        [calls("delete_event", event_id="evt_soccer"), says("Delete soccer practice?")],
        [says("Okay, left it alone.")],
        events=[_soccer()],
    )
    first = ask(deps, "cancel soccer")
    decline = Confirmation(
        call_id=first.pending.call_id,
        approved=False,
        tool=first.pending.tool,
        args=dict(first.pending.args),
    )
    second = ask(deps, "no, leave it", history=first.history, confirm=decline)

    assert repo.get(HOUSEHOLD, "evt_soccer").is_deleted is False
    assert [(a.tool, a.status, a.detail) for a in second.actions] == [
        ("delete_event", "error", "declined")
    ]


# --- refusals and caching ----------------------------------------------------


def test_a_refusal_is_a_normal_turn() -> None:
    _, deps, _ = harness([FakeMessage(content=[], stop_reason="refusal")])
    result = ask(deps, "something the model declines")

    assert result.reply == REFUSAL_REPLY
    assert result.actions == ()
    assert result.pending is None


def test_a_refusal_after_a_tool_call_keeps_what_already_happened() -> None:
    _, deps, _ = harness(
        [
            calls("list_members", **{}),
            FakeMessage(content=[], stop_reason="refusal"),
        ]
    )
    result = ask(deps, "who lives here, and then something declined")
    assert result.reply == REFUSAL_REPLY
    assert [m["role"] for m in result.history] == ["user", "assistant", "user", "assistant"]


def test_the_cached_prefix_is_byte_identical_across_turns() -> None:
    """Two turns, different clocks. Anything volatile in the prefix kills caching
    silently — nothing errors, the cache just never reads."""
    client, deps, _ = harness([says("ok")], [says("ok")])
    ask(deps, "first", now=NOW)
    ask(deps, "second", now=datetime(2026, 12, 25, 9, 0, tzinfo=UTC), actor=RILEY)

    first, second = client.requests
    assert json.dumps(first["system"]) == json.dumps(second["system"])
    assert [t.name for t in first["tools"]] == [t.name for t in second["tools"]]
    assert json.dumps([t.to_dict() for t in first["tools"]]) == json.dumps(
        [t.to_dict() for t in second["tools"]]
    )


def test_the_clock_and_the_actor_ride_in_the_user_turn() -> None:
    client, deps, _ = harness([says("ok")])
    ask(deps, "what's today?", actor=RILEY)
    user_text = client.requests[0]["messages"][-1]["content"]
    assert "2026-08-04" in user_text
    assert RILEY.member_id in user_text
    assert "what's today?" in user_text


def test_an_approval_is_narrated_to_the_model() -> None:
    client, deps, _ = harness([says("ok")])
    ask(deps, "yes", confirm=Confirmation(call_id="call_xyz", approved=True))
    assert "call_xyz" in client.requests[0]["messages"][-1]["content"]
