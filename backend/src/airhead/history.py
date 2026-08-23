"""Conversation history, in the shape the model will accept back.

A turn's `history` is replayed verbatim as the `messages` prefix of the *next*
turn's request, so it has exactly one job: survive a round trip through JSON and
still be a valid content-block array.

The SDK hands back response objects — `BetaToolUseBlock`, `ParsedBetaTextBlock`,
`BetaThinkingBlock` — not dicts. `json.dumps(..., default=str)` will happily
serialize those, but it serializes them as their Python `repr()`, and a `repr()`
is not a content block. Nothing fails at write time; the conversation is simply
poisoned, and the *next* turn is rejected by the model with a 400 that surfaces
as a 502. So blocks are converted to their wire dicts here, on the way in.

`sanitize_history` is the other half: rows written before that fix exist, and a
conversation that was already poisoned must degrade to "the model forgot" rather
than "the API is down".

This module is deliberately dependency-free — both `agent.runner` (which
produces history) and `repo.turns` (which stores it) import it.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

__all__ = [
    "sanitize_history",
    "to_wire_content",
    "to_wire_message",
]

log = logging.getLogger("airhead.history")

ROLES = frozenset({"user", "assistant", "system"})

# Every wire field of every block type this agent can produce or replay. Only used
# by the last-resort path below, for a block object that is neither a pydantic model
# nor a dataclass; anything not present on the object is simply left out.
_BLOCK_FIELDS = (
    "type",
    "id",
    "name",
    "input",
    "text",
    "thinking",
    "signature",
    "data",
    "tool_use_id",
    "content",
    "is_error",
    "citations",
)


# --- writing ------------------------------------------------------------------


def to_wire_message(message: Any) -> dict[str, Any]:
    """One history entry as plain JSON-native data."""
    if isinstance(message, dict):
        role = message.get("role") or "user"
        content = message.get("content")
    else:
        role = getattr(message, "role", None) or "assistant"
        content = getattr(message, "content", None)
    return {"role": str(role), "content": to_wire_content(content)}


def to_wire_content(content: Any) -> Any:
    """A message's content as a string or a list of block dicts."""
    if content is None:
        return []
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return [_block(content)]
    if isinstance(content, list | tuple):
        return [_block(block) for block in content]
    return [_block(content)]


def _block(block: Any) -> dict[str, Any]:
    if isinstance(block, str):
        return {"type": "text", "text": block}
    if isinstance(block, dict):
        return {str(key): _plain(value) for key, value in block.items()}

    dump = getattr(block, "model_dump", None)
    if callable(dump):
        # Exactly how the SDK serializes one of its own response models when it is
        # handed back as request input (`anthropic/_utils/_transform.py`): API field
        # names, only the fields the API actually set, and the model's own
        # `__api_exclude__` — which is how `parsed_output`, an SDK convenience field
        # that is not part of the wire schema, stays out of the request.
        return dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
            exclude=getattr(block, "__api_exclude__", None),
        )

    if dataclasses.is_dataclass(block) and not isinstance(block, type):
        return {str(key): _plain(value) for key, value in dataclasses.asdict(block).items()}

    return {key: _plain(getattr(block, key)) for key in _BLOCK_FIELDS if _has(block, key)}


def _has(block: Any, key: str) -> bool:
    return getattr(block, key, None) is not None


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return _block(value)


# --- reading ------------------------------------------------------------------


def sanitize_history(history: Any, *, conversation_id: str | None = None) -> list[dict[str, Any]]:
    """A stored history, or an empty one if it is not replayable.

    An unreplayable history is dropped whole rather than per entry: a `tool_use`
    block whose matching `tool_result` was dropped (or the reverse) is itself a 400,
    so a partial repair would just move the failure. Losing the context of an old
    conversation costs the model its memory of it; keeping a corrupt one costs every
    subsequent turn a 502.
    """
    reason = _unreplayable(history)
    if reason is None:
        return list(history or [])
    # PRD §13: the entries quote event titles, so the reason travels, never the data.
    log.warning(
        "turn_history_dropped",
        extra={"conversation_id": conversation_id, "reason": reason},
    )
    return []


def _unreplayable(history: Any) -> str | None:
    if history is None or history == []:
        return None
    if not isinstance(history, list):
        return "not_a_list"
    for entry in history:
        reason = _bad_message(entry)
        if reason is not None:
            return reason
    return None


def _bad_message(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "entry_not_an_object"
    if entry.get("role") not in ROLES:
        return "unknown_role"
    content = entry.get("content")
    if isinstance(content, str):
        return None
    if not isinstance(content, list):
        return "content_not_a_list"
    for block in content:
        # The stringified-block failure lands here: `default=str` turned each block
        # into its `repr()`, so the content list holds strings, not objects.
        if not isinstance(block, dict):
            return "block_not_an_object"
        reason = _bad_block(block)
        if reason is not None:
            return reason
    return None


def _bad_block(block: dict[str, Any]) -> str | None:
    kind = block.get("type")
    if not isinstance(kind, str) or not kind:
        return "block_without_type"
    if kind == "tool_use":
        if not block.get("id") or not block.get("name"):
            return "tool_use_without_identity"
        if not isinstance(block.get("input"), dict):
            return "tool_use_without_input"
    if kind == "tool_result" and not block.get("tool_use_id"):
        return "tool_result_without_tool_use_id"
    return None
