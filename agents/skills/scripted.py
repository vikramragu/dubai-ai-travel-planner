"""Scripted planner: request -> scheduler -> budget -> (fit to budget) -> rule checks, no LLM.

This is the Phase 3 test harness and the deterministic fallback the agents use when an LLM
call fails. Run it from the command line to see the plan for the example request::

    uv run python -m agents.skills.scripted [--budget 3000] [--tier mid] [--days 3] [--no-fit]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from agents.skills.budget_engine import (
    BudgetOptions,
    FitResult,
    PlanInputs,
    fit_to_budget,
    plan_trip,
)
from agents.skills.calendar import trip_month
from agents.skills.rule_checks import review_from_rules
from agents.skills.scheduler import SchedulerOptions
from data.providers import DataProviders, get_providers
from schemas import (
    Attraction,
    BudgetAlternative,
    HotelArea,
    HotelTier,
    Interest,
    Itinerary,
    SlotKind,
    TripRequest,
)
from tools import get_hotel_areas


@dataclass
class ScriptedResult:
    itinerary: Itinerary  # with budget, alternatives and review
    applied: list[BudgetAlternative] = field(default_factory=list)
    unscheduled: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def pick_hotel(
    request: TripRequest, providers: DataProviders, month: int | None = None
) -> HotelArea:
    """Deterministic hotel choice: metro access, beach access if wanted, then price."""
    c = request.constraints
    areas = get_hotel_areas(c.hotel_tier, month=trip_month(c, month), providers=providers)
    wants_beach = Interest.BEACH in c.interests
    return min(
        areas,
        key=lambda a: (
            not a.metro_access,
            wants_beach and not a.beach_access and c.hotel_tier is not HotelTier.BUDGET,
            a.nightly_rate.value.midpoint,
            a.neighborhood_id,
        ),
    )


def run_scripted(
    request: TripRequest,
    providers: DataProviders,
    *,
    hotel: HotelArea | None = None,
    candidates: list[Attraction] | None = None,
    options: SchedulerOptions | None = None,
    budget_options: BudgetOptions | None = None,
    fit: bool = True,
) -> ScriptedResult:
    options = options or SchedulerOptions()
    hotel = hotel or pick_hotel(request, providers, options.month)
    inputs = PlanInputs(hotel=hotel, options=options, candidates=candidates)
    plan = plan_trip(request, inputs, providers, budget_options)
    if fit:
        result = fit_to_budget(plan, request, providers, budget_options)
    else:
        result = FitResult(plan, [], [])
    itinerary = result.itinerary
    review = review_from_rules(itinerary, providers, month=options.month)
    return ScriptedResult(
        itinerary=itinerary.model_copy(update={"review": review}),
        applied=result.applied,
        unscheduled=result.plan.schedule.unscheduled,
        notes=result.plan.schedule.notes,
    )


def format_itinerary(result: ScriptedResult) -> str:
    it = result.itinerary
    c = it.request.constraints
    lines = [
        f"{c.days}-day plan, {c.travelers} traveller(s), {it.hotel.tier.value} hotel in "
        f"{it.hotel.neighborhood_id}, budget AED {c.budget_aed:,.0f}",
        *(f"  note: {n}" for n in result.notes),
    ]
    for day in it.days:
        lines.append(f"\nDay {day.day} - {day.theme}  (AED {day.day_cost_aed:,.0f})")
        for s in day.slots:
            mark = {SlotKind.TRANSIT: "  ", SlotKind.REST: "  ."}.get(s.kind, "  *")
            lines.append(f"{mark} {s.start:%H:%M}-{s.end:%H:%M}  {s.title}")
    if it.budget is not None:
        b = it.budget
        lines.append(
            f"\nBudget: accommodation {b.accommodation:,.0f} | food {b.food:,.0f} | transport "
            f"{b.transport:,.0f} | activities {b.activities:,.0f} | contingency "
            f"{b.contingency:,.0f} = AED {b.total:,.0f} ({'within' if b.within_budget else 'OVER'} "
            f"budget, AED {b.remaining_aed:,.0f} left)"
        )
    for a in it.alternatives:
        lines.append(
            f"  {'applied' if a.applied else 'option '}: {a.description} (-AED {a.saving_aed:,.0f})"
        )
    if it.review is not None:
        lines.append(f"\nReview: score {it.review.score:.0f}, passed={it.review.passed}")
        lines += [
            f"  {i.severity.value}: {i.message} -> {i.route_to.value}" for i in it.review.issues
        ]
    if result.unscheduled:
        lines.append(f"\nNot scheduled (did not fit): {', '.join(result.unscheduled)}")
    return "\n".join(lines)


def example_request(budget: float = 5000, tier: str = "budget", days: int = 4) -> TripRequest:
    """The problem statement's example request, already parsed (the LLM parser is Phase 4)."""
    from schemas import Avoidance, TripConstraints

    return TripRequest(
        raw_text=(
            "Plan a 4-day trip to Dubai with a budget of AED 5,000, excluding flights. "
            "I enjoy cultural attractions, local food, and beaches. "
            "I prefer affordable hotels and want to avoid crowded places."
        ),
        constraints=TripConstraints(
            days=days,
            budget_aed=budget,
            interests=[Interest.CULTURE, Interest.FOOD, Interest.BEACH],
            hotel_tier=HotelTier(tier),
            avoid=[Avoidance.CROWDS],
        ),
        assumptions=["1 traveller", "no start date given"],
    )


def main(argv: list[str]) -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=float, default=5000)
    parser.add_argument("--tier", choices=[t.value for t in HotelTier], default="budget")
    parser.add_argument("--days", type=int, default=4)
    parser.add_argument("--month", type=int, default=None)
    parser.add_argument("--no-fit", action="store_true", help="don't apply cheaper alternatives")
    args = parser.parse_args(argv)
    request = example_request(args.budget, args.tier, args.days)
    result = run_scripted(
        request, get_providers(), options=SchedulerOptions(month=args.month), fit=not args.no_fit
    )
    print(format_itinerary(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
