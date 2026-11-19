"""Checks on a typed request before planning, shown under the text box (no LLM call)."""

from __future__ import annotations

from dataclasses import dataclass

from agents.orchestrator import keyword_parse

MAX_DAYS = 7


@dataclass(frozen=True)
class Check:
    level: str  # "error" (planning would fail) | "warning" | "info"
    message: str


def precheck(text: str) -> list[Check]:
    """What the planner would complain about or assume, from the keyword parser's reading.

    Only the keyword parser is used, so this is instant and free. When an LLM is configured it
    may understand more than these checks do, so only clear blockers are errors."""
    if not text.strip():
        return [Check("error", "Describe your trip first.")]
    parsed = keyword_parse(text)
    out: list[Check] = []
    if "dubai" not in parsed.destination.lower():
        out.append(Check("error", f"This planner only covers Dubai, not {parsed.destination}."))
    if parsed.budget_amount is None:
        out.append(Check("error", 'Add a budget, e.g. "budget AED 5,000".'))
    if parsed.days is None:
        out.append(Check("info", "No trip length found: 3 days will be planned."))
    elif parsed.days > MAX_DAYS:
        out.append(Check("warning", f"{parsed.days} days asked: the first {MAX_DAYS} are planned."))
    if not parsed.interests:
        out.append(Check("info", "No interests found: culture and food will be assumed."))
    return out
