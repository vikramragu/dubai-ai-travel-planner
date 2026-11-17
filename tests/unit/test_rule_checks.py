"""Rule checks: each rule fires on a hand-broken itinerary and routes to the right node."""

from __future__ import annotations

from datetime import date, time
from typing import Any

import pytest

from agents.skills.budget_engine import PlanInputs, compute_budget, plan_trip, with_budget
from agents.skills.rule_checks import check_itinerary, meal_of, review_from_rules, score
from agents.skills.scripted import example_request, pick_hotel
from data.providers import DataProviders
from schemas import (
    Interest,
    Itinerary,
    MealType,
    Pace,
    ReviewIssue,
    RouteTarget,
    Severity,
    SlotKind,
)


@pytest.fixture(scope="module")
def good(providers: DataProviders) -> Itinerary:
    req = example_request()
    return plan_trip(req, PlanInputs(hotel=pick_hotel(req, providers)), providers).itinerary


def _codes(it: Itinerary, providers: DataProviders, **kw: Any) -> dict[str, ReviewIssue]:
    return {i.code: i for i in check_itinerary(it, providers, **kw)}


def _with_constraints(it: Itinerary, **changes: object) -> Itinerary:
    c = it.request.constraints.model_copy(update=changes)
    return it.model_copy(update={"request": it.request.model_copy(update={"constraints": c})})


def _replace_slot(it: Itinerary, day: int, index: int, **changes: object) -> Itinerary:
    d = it.days[day]
    slots = list(d.slots)
    slots[index] = slots[index].model_copy(update=changes)
    days = list(it.days)
    days[day] = d.model_copy(update={"slots": slots})
    return it.model_copy(update={"days": days})


def _first(it: Itinerary, kind: SlotKind, day: int = 0) -> int:
    return next(i for i, s in enumerate(it.days[day].slots) if s.kind is kind)


def test_good_plan_passes(good: Itinerary, providers: DataProviders) -> None:
    report = review_from_rules(good, providers)
    assert report.passed and report.score == 100 and report.issues == []


def test_over_budget_blocker(good: Itinerary, providers: DataProviders) -> None:
    tight = _with_constraints(good, budget_aed=1000)
    it = with_budget(tight, compute_budget(tight, providers))
    issues = _codes(it, providers)
    assert issues["over_budget"].severity is Severity.BLOCKER
    assert issues["over_budget"].route_to is RouteTarget.BUDGET
    assert issues["hotel_cost_driver"].route_to is RouteTarget.LOGISTICS
    assert not review_from_rules(it, providers).passed


def test_budget_missing(good: Itinerary, providers: DataProviders) -> None:
    draft = good.model_copy(update={"budget": None})
    assert _codes(draft, providers)["budget_missing"].severity is Severity.MINOR


def test_crowded_place_when_avoiding_crowds(good: Itinerary, providers: DataProviders) -> None:
    """The 'avoid crowds' fixture: a high-crowd place slipped into the plan."""
    i = _first(good, SlotKind.ACTIVITY, day=1)
    slot = good.days[1].slots[i]
    it = _replace_slot(
        good,
        1,
        i,
        place_id="dubai-mall",
        title="The Dubai Mall",
        start=time(10),
        end=max(slot.end, time(11)),
    )
    issue = _codes(it, providers)["crowded_place"]
    assert issue.severity is Severity.MAJOR
    assert issue.route_to is RouteTarget.DESTINATION
    assert issue.place_id == "dubai-mall"
    relaxed = _with_constraints(it, avoid=[])
    assert "crowded_place" not in _codes(relaxed, providers)


def test_unknown_place(good: Itinerary, providers: DataProviders) -> None:
    i = _first(good, SlotKind.ACTIVITY)
    it = _replace_slot(good, 0, i, place_id="made-up-museum")
    issue = _codes(it, providers)["unknown_place"]
    assert issue.severity is Severity.BLOCKER and issue.route_to is RouteTarget.DESTINATION


def test_outside_opening_hours(good: Itinerary, providers: DataProviders) -> None:
    i = _first(good, SlotKind.ACTIVITY)
    it = _replace_slot(good, 0, i, place_id="coffee-museum", start=time(18), end=time(18, 45))
    # keep slots ordered: put it last
    d = it.days[0]
    slots = [s for j, s in enumerate(d.slots) if j != i]
    late = d.slots[i].model_copy(update={"start": time(23), "end": time(23, 30)})
    days = [d.model_copy(update={"slots": [*slots, late]}), *it.days[1:]]
    it = it.model_copy(update={"days": days})
    issue = _codes(it, providers)["outside_opening_hours"]
    assert issue.severity is Severity.BLOCKER and issue.route_to is RouteTarget.ASSEMBLE


def test_duplicate_place(good: Itinerary, providers: DataProviders) -> None:
    pid = good.days[0].slots[_first(good, SlotKind.ACTIVITY)].place_id
    i = _first(good, SlotKind.ACTIVITY, day=1)
    it = _replace_slot(good, 1, i, place_id=pid)
    issue = _codes(it, providers)["duplicate_place"]
    assert issue.severity is Severity.MAJOR and issue.route_to is RouteTarget.DESTINATION


def test_day_overloaded(good: Itinerary, providers: DataProviders) -> None:
    it = _with_constraints(good, pace=Pace.RELAXED)
    busiest = max(d.minutes_of(SlotKind.ACTIVITY, SlotKind.TRANSIT) for d in good.days)
    issues = check_itinerary(it, providers)
    overloaded = [i for i in issues if i.code == "day_overloaded"]
    assert (busiest > 360) == bool(overloaded)
    assert all(i.severity is Severity.BLOCKER and i.day for i in overloaded)


def test_interest_missing(good: Itinerary, providers: DataProviders) -> None:
    it = _with_constraints(
        good, interests=[*good.request.constraints.interests, Interest.ADVENTURE]
    )
    issue = _codes(it, providers)["interest_missing"]
    assert "adventure" in issue.message and issue.route_to is RouteTarget.DESTINATION


def test_missing_meal(good: Itinerary, providers: DataProviders) -> None:
    d = good.days[0]
    no_meals = [s for s in d.slots if s.kind is not SlotKind.MEAL]
    it = good.model_copy(
        update={"days": [d.model_copy(update={"slots": no_meals}), *good.days[1:]]}
    )
    issues = [i for i in check_itinerary(it, providers) if i.code == "missing_meal"]
    assert {i.message for i in issues} == {"Day 1 has no lunch", "Day 1 has no dinner"}
    assert all(i.severity is Severity.MINOR for i in issues)


def test_missing_leg(good: Itinerary, providers: DataProviders) -> None:
    d = good.days[0]
    slots = d.slots
    # drop the transit slot between two places that have a move between them
    j = next(
        i
        for i in range(1, len(slots) - 1)
        if slots[i].kind is SlotKind.TRANSIT
        and slots[i - 1].place_id
        and slots[i + 1].place_id
        and slots[i - 1].place_id != slots[i + 1].place_id
    )
    it = good.model_copy(
        update={
            "days": [d.model_copy(update={"slots": [*slots[:j], *slots[j + 1 :]]}), *good.days[1:]]
        }
    )
    assert _codes(it, providers)["missing_leg"].severity is Severity.MINOR


def test_repeated_restaurant(good: Itinerary, providers: DataProviders) -> None:
    lunch = next(
        s
        for s in good.days[0].slots
        if s.kind is SlotKind.MEAL and meal_of(s.start) is MealType.LUNCH
    )
    j = next(
        i
        for i, s in enumerate(good.days[1].slots)
        if s.kind is SlotKind.MEAL and meal_of(s.start) is MealType.DINNER
    )
    it = _replace_slot(good, 1, j, place_id=lunch.place_id)
    assert "repeated_restaurant" in _codes(it, providers)


def test_seasonal_and_heat_in_summer(good: Itinerary, providers: DataProviders) -> None:
    i = _first(good, SlotKind.ACTIVITY)
    slot = good.days[0].slots[i]
    it = _replace_slot(good, 0, i, place_id="global-village", start=slot.start, end=slot.end)
    summer = _with_constraints(it, start_date=date(2027, 7, 5))
    issues = _codes(summer, providers)
    assert issues["closed_for_season"].severity is Severity.BLOCKER
    assert "closed_for_season" not in _codes(
        _with_constraints(it, start_date=date(2027, 1, 4)), providers
    )
    beach = _replace_slot(good, 0, i, place_id="kite-beach", start=time(12), end=time(13))
    hot = _codes(_with_constraints(beach, start_date=date(2027, 8, 2)), providers)
    assert hot["outdoor_midday_heat"].severity is Severity.MINOR


def test_long_leg(good: Itinerary, providers: DataProviders) -> None:
    i = _first(good, SlotKind.TRANSIT)
    leg = good.days[0].slots[i].leg
    assert leg is not None
    slow = leg.model_copy(
        update={"duration_min": leg.duration_min.model_copy(update={"value": 75})}
    )
    it = _replace_slot(good, 0, i, leg=slow)
    assert _codes(it, providers)["long_leg"].route_to is RouteTarget.LOGISTICS


def test_early_and_late(good: Itinerary, providers: DataProviders) -> None:
    it = _with_constraints(good, avoid=["early_mornings", "late_nights"])
    first = it.days[0].slots[0]
    it = _replace_slot(it, 0, 0, start=time(8), end=first.end)
    codes = _codes(it, providers)
    assert "early_start" in codes


def test_score_and_meal_of() -> None:
    def mk(sev: Severity) -> ReviewIssue:
        return ReviewIssue(code="x", severity=sev, message="m", route_to=RouteTarget.BUDGET)

    assert score([]) == 100
    assert score([mk(Severity.BLOCKER), mk(Severity.MAJOR), mk(Severity.MINOR)]) == 55
    assert score([mk(Severity.BLOCKER)] * 5) == 0
    assert meal_of(time(8)) is MealType.BREAKFAST
    assert meal_of(time(13)) is MealType.LUNCH
    assert meal_of(time(19)) is MealType.DINNER
