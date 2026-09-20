"""The curated interval table - ground rule 1, step 3.

~110 common household items with a default interval, a plausible range and a
one-line "why". Deterministic first: a name that matches here never costs a
model call, and the answer is the same every time. Curated from manufacturer,
NFPA/EPA/CDC and common trade guidance; the data lives in `catalog.json` next
to this module so it can be edited without touching code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from airhead.domain import Anchor

_DATA = Path(__file__).with_name("catalog.json")

# Words that carry no signal for matching: "the haircut", "my car's cabin filter".
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "my",
        "our",
        "your",
        "his",
        "her",
        "their",
        "its",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "and",
        "or",
        "with",
        "get",
        "do",
    ]
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    key: str
    name: str
    category: str
    interval_days: int
    interval_min_days: int | None
    interval_max_days: int | None
    interval_miles: int | None
    anchor: Anchor
    season_hint: str | None
    aliases: tuple[str, ...]
    notes: str
    sources: tuple[str, ...]


def normalize(text: str) -> tuple[str, ...]:
    """Lower-case word tokens with punctuation and stop-words removed.

    Possessives and plurals are folded crudely ("filters" -> "filter") so a
    household's phrasing does not have to match the table's.
    """
    words = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
    out: list[str] = []
    for w in words:
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return tuple(out)


@lru_cache(maxsize=1)
def entries() -> tuple[CatalogEntry, ...]:
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    return tuple(
        CatalogEntry(
            key=e["key"],
            name=e["name"],
            category=e["category"],
            interval_days=int(e["interval_days"]),
            interval_min_days=e.get("interval_min_days"),
            interval_max_days=e.get("interval_max_days"),
            interval_miles=e.get("interval_miles"),
            anchor=Anchor(e.get("anchor", "elapsed")),
            season_hint=e.get("season_hint"),
            aliases=tuple(e.get("aliases", ())),
            notes=e.get("notes", ""),
            sources=tuple(e.get("sources", ())),
        )
        for e in raw
    )


@lru_cache(maxsize=1)
def _index() -> list[tuple[tuple[str, ...], CatalogEntry]]:
    """Every alias and name, tokenized, longest first so the most specific wins."""
    pairs: list[tuple[tuple[str, ...], CatalogEntry]] = []
    for entry in entries():
        for phrase in (entry.name, *entry.aliases):
            tokens = normalize(phrase)
            if tokens:
                pairs.append((tokens, entry))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def lookup(name: str) -> CatalogEntry | None:
    """The entry whose name or alias appears in `name`, most specific first.

    "Cabin air filter 2023 Kia EV6" matches the alias "cabin air filter";
    "Haircut" matches exactly; "Buy milk" matches nothing and returns None, at
    which point the caller may ask the model (ground rule 1, step 4).
    """
    tokens = normalize(name)
    if not tokens:
        return None
    for phrase, entry in _index():
        if _contains(tokens, phrase):
            return entry
    return None


def get(key: str) -> CatalogEntry | None:
    return next((e for e in entries() if e.key == key), None)
