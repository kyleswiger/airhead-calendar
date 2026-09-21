"""Ground rule 1, step 4: one structured model call for an interval the catalog misses.

Kept deliberately small and boring: a single non-streaming request that asks for
strict JSON, parsed defensively. Anything the model does wrong - prose around the
object, code fences, a string where an int should be, an outage - degrades to
`service.UNKNOWN` (the routine is created `unscheduled` and the person is asked),
never to a 500 on the route. Nothing here is a source of truth: the result is stored
as `estimated` with its rationale and confidence so the display can say "est." and
the person can overwrite it with one tap.

Logging: error types and counts only. The routine name is household text.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from airhead.domain import Anchor, IntervalSource
from airhead.routines import catalog
from airhead.routines.service import UNKNOWN, Resolution

log = logging.getLogger("airhead.routines.estimate")

# Caps thinking *plus* the visible JSON on a thinking-by-default model; the JSON itself
# is a few hundred tokens.
MAX_TOKENS = 4000
EFFORT = "low"

# Longest sensible answer: a decade. Anything beyond is a model hallucinating units.
MAX_INTERVAL_DAYS = 3650

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")


@dataclass(frozen=True, slots=True)
class Estimate(Resolution):
    """A `Resolution` plus the one thing only a model produces: a follow-up question
    when a single fact (mileage, pets, climate) would change the answer materially."""

    follow_up_question: str | None = None


def _categories() -> list[str]:
    return sorted({e.category for e in catalog.entries()} | {"other"})


def _system_prompt() -> str:
    return (
        "You estimate how often a household chore or maintenance item recurs. "
        "Answer with ONE JSON object and nothing else: no prose, no markdown fences.\n"
        "Keys (all required):\n"
        '  "interval_days": integer number of days between occurrences, or null if '
        "you genuinely cannot say\n"
        '  "range_days": [min, max] integers bracketing a reasonable interval, or null\n'
        '  "confidence": number 0..1\n'
        '  "rationale": one short sentence citing the guidance you are relying on\n'
        '  "usage_dependent": true if usage (mileage, hours, pets, climate) moves it\n'
        '  "follow_up_question": one question that would sharpen the estimate, or null\n'
        '  "anchor": "elapsed" (due N days after last done) or "calendar" '
        "(same date every year, e.g. holiday decorations)\n"
        f'  "category": one of {json.dumps(_categories())}\n'
        "Prefer manufacturer, safety-body (NFPA, EPA, CDC) or common trade guidance. "
        "Never invent precision: when unsure, widen range_days and lower confidence."
    )


def _user_prompt(name: str, context: str | None) -> str:
    text = f"Item: {name}"
    if context and context.strip():
        text += f"\nContext from the household: {context.strip()}"
    return text


def estimate_interval(
    client: Any, *, model: str, name: str, context: str | None = None
) -> Resolution:
    """One structured call; `UNKNOWN` on any failure. Never raises."""
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            output_config={"effort": EFFORT},
            system=_system_prompt(),
            messages=[{"role": "user", "content": _user_prompt(name, context)}],
        )
    except Exception as exc:
        # Type only: an SDK exception quotes the request, which quotes the routine name.
        log.warning("estimate_call_failed", extra={"error_type": type(exc).__name__})
        return UNKNOWN
    text = _response_text(response)
    resolution = parse_estimate(text)
    log.info(
        "routine_estimated",
        extra={
            "known": resolution.interval_days is not None,
            "confidence": resolution.confidence,
            "usage_dependent": resolution.usage_dependent,
        },
    )
    return resolution


def _response_text(response: Any) -> str:
    """Concatenate the text blocks; thinking and any other block kinds are skipped."""
    parts: list[str] = []
    for block in getattr(response, "content", None) or ():
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def parse_estimate(text: str) -> Resolution:
    """Strict-ish: fences and surrounding prose are tolerated, wrong shapes are not."""
    data = _extract_json(text)
    if data is None:
        log.warning("estimate_parse_failed", extra={"chars": len(text)})
        return UNKNOWN

    interval = _as_int(data.get("interval_days"))
    if interval is not None and not 1 <= interval <= MAX_INTERVAL_DAYS:
        interval = None
    if interval is None:
        # Ground rule 1 step 5: the model declining is a legitimate answer. Keep the
        # question it wants answered; drop everything else.
        return Estimate(
            interval_days=None,
            source=None,
            follow_up_question=_as_str(data.get("follow_up_question")),
        )

    confidence = _as_float(data.get("confidence"))
    if confidence is not None:
        confidence = min(1.0, max(0.0, confidence))
    anchor_raw = data.get("anchor")
    anchor = Anchor(anchor_raw) if anchor_raw in {a.value for a in Anchor} else None
    category = _as_str(data.get("category"))
    if category not in _categories():
        category = None

    return Estimate(
        interval_days=interval,
        source=IntervalSource.ESTIMATED,
        note=_as_str(data.get("rationale")),
        confidence=confidence,
        category=category,
        anchor=anchor,
        range_days=_as_range(data.get("range_days"), interval),
        usage_dependent=bool(data.get("usage_dependent", False)),
        follow_up_question=_as_str(data.get("follow_up_question")),
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    stripped = _FENCE.sub("", text or "").strip()
    candidates = [stripped]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_range(value: Any, interval: int) -> tuple[int, int] | None:
    if not isinstance(value, list | tuple) or len(value) != 2:
        return None
    lo, hi = _as_int(value[0]), _as_int(value[1])
    if lo is None or hi is None or lo < 1 or hi < lo:
        return None
    return (min(lo, interval), max(hi, interval))
