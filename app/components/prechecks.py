"""Checks on a typed request before planning, shown under the text box (no LLM call)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from agents.orchestrator import keyword_parse
from data.providers import DataProviders
from schemas import Itinerary

MAX_DAYS = 7
DEFAULT_DAYS = 3


@dataclass(frozen=True)
class Check:
    level: str  # "error" (planning would fail) | "warning" | "info"
    message: str


def cheapest_night(providers: DataProviders) -> float | None:
    """The lowest nightly room rate of any hotel area (AED, before fees)."""
    rates = [a.nightly_rate.value.min_aed for a in providers.hotels.areas()]
    return min(rates) if rates else None


def hotel_floor(days: int, rooms: int, night_aed: float) -> float:
    """What the cheapest hotels alone cost for the trip (``days - 1`` nights)."""
    return max(days - 1, 0) * rooms * night_aed


def precheck(text: str, *, night_aed: float | None = None) -> list[Check]:
    """What the planner would complain about or assume, from the keyword parser's reading.

    Only the keyword parser is used, so this is instant and free. When an LLM is configured it
    may understand more than these checks do, so only clear blockers are errors. With
    ``night_aed`` (the cheapest nightly rate) a budget that can't even pay for the hotels is
    flagged before planning."""
    if not text.strip():
        return [Check("error", "Describe your trip first.")]
    parsed = keyword_parse(text)
    out: list[Check] = []
    if "dubai" not in parsed.destination.lower():
        out.append(Check("error", f"This planner only covers Dubai, not {parsed.destination}."))
    if parsed.budget_amount is None:
        out.append(Check("error", 'Add a budget, e.g. "budget AED 5,000".'))
    if parsed.days is None:
        out.append(Check("info", f"No trip length found: {DEFAULT_DAYS} days will be planned."))
    elif parsed.days > MAX_DAYS:
        out.append(Check("warning", f"{parsed.days} days asked: the first {MAX_DAYS} are planned."))
    if not parsed.interests:
        out.append(Check("info", "No interests found: culture and food will be assumed."))
    if night_aed and parsed.budget_amount and parsed.budget_currency == "AED":
        days = min(parsed.days or DEFAULT_DAYS, MAX_DAYS)
        rooms = parsed.rooms or max(1, math.ceil((parsed.travelers or 1) / 2))
        floor = hotel_floor(days, rooms, night_aed)
        if parsed.budget_amount < floor:
            out.append(
                Check(
                    "warning",
                    f"AED {parsed.budget_amount:,.0f} won't cover even the cheapest hotels for "
                    f"{days} days (about AED {floor:,.0f}). You'll get the cheapest plan "
                    "possible, and it will be over budget.",
                )
            )
    return out


def budget_shortfall(it: Itinerary) -> str | None:
    """A plain message when the final plan is still over budget, with a budget that would work."""
    b = it.budget
    if b is None or b.within_budget:
        return None
    gap = b.total - b.budget_aed
    days = len(it.days)
    suggest = math.ceil(b.total / 100) * 100
    return (
        f"**AED {b.budget_aed:,.0f} isn't enough for this trip.** The cheapest plan the agents "
        f"could make costs AED {b.total:,.0f}, which is AED {gap:,.0f} over. Try a budget of "
        f"about AED {suggest:,.0f}" + (", or fewer days." if days > 1 else ".")
    )
