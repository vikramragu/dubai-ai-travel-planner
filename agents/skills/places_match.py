"""Match place names a traveller wrote ("I want to see the Etihad Museum") to dataset ids.

Used for must-visit places in typed requests: the keyword parser scans the text for known
names, and names the LLM extracted are matched the same way. Matching is deliberately strict
(a whole known name, or all the significant words of one) so a passing mention of "Dubai" or
"the beach" never pins a place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from schemas import Attraction

# Words that don't identify a place on their own.
_GENERIC = {"al", "the", "of", "and", "dubai", "a", "an", "at", "in", "on", "de", "el"}
_NEGATION = re.compile(r"\b(avoid|avoiding|not|no|skip|skipping|except|without|hate)\b")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _names(place: Attraction) -> list[str]:
    """The full name and the name without a parenthesised part ("Heritage House (Al Ras)")."""
    full = norm(place.name)
    short = norm(re.sub(r"\(.*?\)", " ", place.name))
    return [n for n in dict.fromkeys([full, short]) if n]


def _significant(name: str) -> set[str]:
    return set(name.split()) - _GENERIC


def match_name(name: str, places: Iterable[Attraction]) -> str | None:
    """The id of the place a written name refers to, or ``None``."""
    want = norm(name)
    if not want:
        return None
    pool = list(places)
    for p in pool:
        if want == p.id or want in _names(p):
            return p.id
    words = _significant(want)
    if not words:
        return None
    hits = [p for p in pool if any(words <= _significant(n) for n in _names(p))]
    return hits[0].id if len(hits) == 1 else None  # ambiguous ("museum") -> no match


def places_in_text(text: str, places: Iterable[Attraction]) -> list[str]:
    """Ids of known places named in free text, in the order they appear. A name only counts
    when it has at least two significant words (or one long one) and isn't preceded by a
    negation such as "avoid" or "skip"."""
    t = f" {norm(text)} "
    found: list[tuple[int, int, str]] = []  # (start, end, id)
    for p in places:
        for name in _names(p):
            words = _significant(name)
            if not words or (len(words) == 1 and len(next(iter(words))) < 7):
                continue
            pos = t.find(f" {name} ")
            if pos < 0:
                continue
            before = t[max(0, pos - 30) : pos]
            if _NEGATION.search(before):
                continue
            found.append((pos, pos + len(name) + 1, p.id))
            break
    # A name inside a longer matched name is the same mention ("Heritage Village" inside
    # "Hatta Heritage Village"): keep only the longest.
    kept = [
        f
        for f in found
        if not any(
            g is not f and g[0] <= f[0] and f[1] <= g[1] and g[1] - g[0] > f[1] - f[0]
            for g in found
        )
    ]
    return [pid for _, _, pid in sorted(kept)]
