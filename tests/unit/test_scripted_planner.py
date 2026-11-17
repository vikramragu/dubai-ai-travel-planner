"""The scripted planner (request -> scheduler -> budget -> rules, no LLM) and property tests."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agents.skills.rule_checks import check_itinerary
from agents.skills.scripted import example_request, format_itinerary, run_scripted
from data.providers import DataProviders
from schemas import (
    Avoidance,
    HotelTier,
    Interest,
    Pace,
    Severity,
    SlotKind,
    TripConstraints,
    TripRequest,
)


def test_example_request_end_to_end(providers: DataProviders) -> None:
    result = run_scripted(example_request(), providers)
    it = result.itinerary
    assert it.is_final
    assert it.budget is not None and it.budget.within_budget
    assert it.review is not None and it.review.passed and it.review.score >= 90
    text = format_itinerary(result)
    assert "Day 4" in text and "Budget:" in text


def test_forced_over_budget_is_detected_and_fixed(providers: DataProviders) -> None:
    """Exit criterion: the blocker fires, and applying alternatives brings the plan under budget."""
    req = example_request(budget=3000, tier="mid")
    unfixed = run_scripted(req, providers, fit=False).itinerary
    assert unfixed.review is not None
    assert any(
        i.code == "over_budget" and i.severity is Severity.BLOCKER for i in unfixed.review.issues
    )

    fixed = run_scripted(req, providers)
    assert fixed.applied
    it = fixed.itinerary
    assert it.budget is not None and it.budget.within_budget
    assert it.review is not None and "over_budget" not in {i.code for i in it.review.issues}


def test_deterministic(providers: DataProviders) -> None:
    a = run_scripted(example_request(), providers).itinerary
    b = run_scripted(example_request(), providers).itinerary
    assert a.model_dump() == b.model_dump()


interests = st.lists(st.sampled_from(list(Interest)), min_size=1, max_size=4, unique=True)
avoid = st.lists(st.sampled_from(list(Avoidance)), max_size=2, unique=True)


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    days=st.integers(1, 7),
    pace=st.sampled_from(list(Pace)),
    tier=st.sampled_from(list(HotelTier)),
    picked=interests,
    avoid=avoid,
    travelers=st.integers(1, 5),
    month=st.integers(1, 12),
)
def test_plan_invariants(
    providers: DataProviders,
    days: int,
    pace: Pace,
    tier: HotelTier,
    picked: list[Interest],
    avoid: list[Avoidance],
    travelers: int,
    month: int,
) -> None:
    """For any request: totals add up, no overlaps, opening hours and pace respected,
    no attraction repeated, no leg over an hour, a leg on every move."""
    from agents.skills.scheduler import SchedulerOptions

    req = TripRequest(
        raw_text="generated",
        constraints=TripConstraints(
            days=days,
            budget_aed=50_000,
            interests=picked,
            hotel_tier=tier,
            avoid=avoid,
            pace=pace,
            travelers=travelers,
            rooms=max(1, (travelers + 1) // 2),
        ),
    )
    it = run_scripted(req, providers, options=SchedulerOptions(month=month), fit=False).itinerary
    assert it.budget is not None
    assert it.budget.total == pytest.approx(
        sum(ln.amount_aed.value for ln in it.budget.lines), abs=0.05
    )
    assert len(it.days) == days  # (slot overlaps are rejected by the DayPlan model itself)
    codes = {i.code for i in check_itinerary(it, providers, month=month)}
    assert not codes & {
        "outside_opening_hours",
        "day_overloaded",
        "duplicate_place",
        "missing_leg",
        "unknown_place",
        "closed_for_season",
        "long_leg",
    }  # (repeated_restaurant is allowed: reusing a restaurant beats skipping a meal)
    ids = [s.place_id for d in it.days for s in d.slots if s.kind is SlotKind.ACTIVITY]
    assert len(ids) == len(set(ids))
    if Avoidance.CROWDS in avoid:
        assert "crowded_place" not in codes
