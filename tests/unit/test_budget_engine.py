"""Budget engine: cost lines, provenance, contingency and alternatives."""

from __future__ import annotations

import math

import pytest

from agents.skills.budget_engine import (
    BudgetOptions,
    Plan,
    PlanInputs,
    accommodation_lines,
    compute_budget,
    fit_to_budget,
    generate_alternatives,
    plan_trip,
    with_budget,
)
from agents.skills.scheduler import SchedulerOptions, schedule
from agents.skills.scripted import example_request, pick_hotel
from data.providers import DataProviders
from schemas import (
    Attraction,
    BudgetCategory,
    HotelTier,
    Itinerary,
    Source,
    TripRequest,
)


def _request(**changes: object) -> TripRequest:
    req = example_request()
    return req.model_copy(update={"constraints": req.constraints.model_copy(update=changes)})


def _draft(providers: DataProviders, req: TripRequest | None = None) -> Itinerary:
    req = req or example_request()
    return schedule(req, pick_hotel(req, providers), providers).itinerary


@pytest.fixture(scope="module")
def draft(providers: DataProviders) -> Itinerary:
    return _draft(providers)


class TestCostLines:
    def test_totals_add_up(self, draft: Itinerary, providers: DataProviders) -> None:
        b = compute_budget(draft, providers)
        assert b.total == pytest.approx(sum(ln.amount_aed.value for ln in b.lines), abs=0.05)
        assert b.within_budget and b.total < 5000
        assert all(getattr(b, c.value) > 0 for c in BudgetCategory)

    def test_accommodation_formula(self, draft: Itinerary, providers: DataProviders) -> None:
        room, fees = accommodation_lines(draft, providers)
        c = draft.request.constraints
        rate = draft.hotel.nightly_rate.value.midpoint
        assert room.amount_aed.value == pytest.approx(rate * c.night_count * c.rooms, abs=0.01)
        f = providers.hotels.fees(draft.hotel.tier)
        pct = f.service_charge_pct + f.municipality_fee_pct + f.vat_pct
        expected = (
            rate * c.night_count * pct / 100 + f.tourism_dirham_per_room_night * c.night_count
        )
        assert fees.amount_aed.value == pytest.approx(expected, abs=0.01)

    def test_one_day_trip_has_no_hotel(self, providers: DataProviders) -> None:
        it = _draft(providers, _request(days=1))
        assert accommodation_lines(it, providers) == []

    def test_scales_with_travellers(self, providers: DataProviders) -> None:
        solo = _draft(providers)
        duo = solo.model_copy(
            update={
                "request": solo.request.model_copy(
                    update={
                        "constraints": solo.request.constraints.model_copy(update={"travelers": 2})
                    }
                )
            }
        )
        a, b = compute_budget(solo, providers), compute_budget(duo, providers)
        assert b.activities == pytest.approx(2 * a.activities, abs=0.05)
        assert b.food == pytest.approx(2 * a.food, abs=0.05)
        assert b.accommodation == pytest.approx(a.accommodation)  # same room

    def test_contingency_only_on_estimates(
        self, draft: Itinerary, providers: DataProviders
    ) -> None:
        b = compute_budget(draft, providers, BudgetOptions(contingency_pct=10))
        estimated = sum(
            ln.amount_aed.value
            for ln in b.lines
            if ln.amount_aed.source is Source.ESTIMATED
            and ln.category is not BudgetCategory.CONTINGENCY
        )
        assert b.contingency == pytest.approx(estimated * 0.10, abs=0.01)
        assert compute_budget(draft, providers, BudgetOptions(contingency_pct=0)).contingency == 0

    def test_verified_prices_stay_verified(self, providers: DataProviders) -> None:
        req = _request(days=1)
        museum = providers.places.get_place("al-shindagha-museum")
        assert isinstance(museum, Attraction)
        it = schedule(req, pick_hotel(req, providers), providers, candidates=[museum]).itinerary
        b = compute_budget(it, providers)
        ticket = next(ln for ln in b.lines if ln.ref_id == "al-shindagha-museum")
        assert ticket.amount_aed.source is Source.VERIFIED and ticket.amount_aed.value == 50
        assert ticket.amount_aed.as_of is not None
        assert b.total_by_source(Source.VERIFIED) >= 50

    def test_airport_transfers(self, draft: Itinerary, providers: DataProviders) -> None:
        b = compute_budget(draft, providers)
        airport = [ln for ln in b.lines if ln.ref_id == "airport"]
        assert [ln.day for ln in airport] == [1, len(draft.days)]
        assert airport[0].amount_aed.value >= 25  # airport flag-fall
        none = compute_budget(draft, providers, BudgetOptions(airport_transfers=False))
        assert not [ln for ln in none.lines if ln.ref_id == "airport"]

    def test_with_budget_sets_day_costs(self, draft: Itinerary, providers: DataProviders) -> None:
        b = compute_budget(draft, providers)
        it = with_budget(draft, b)
        assert it.is_final is False and it.budget == b
        assert sum(d.day_cost_aed for d in it.days) == pytest.approx(b.total - b.contingency, abs=1)


@pytest.fixture(scope="module")
def over(providers: DataProviders) -> tuple[TripRequest, Plan]:
    """The forced over-budget fixture: a mid-range trip on a AED 3,000 budget."""
    req = _request(budget_aed=3000, hotel_tier=HotelTier.MID)
    plan = plan_trip(req, PlanInputs(hotel=pick_hotel(req, providers)), providers)
    return req, plan


class TestAlternatives:
    def test_fixture_is_over_budget(self, over: tuple[TripRequest, Plan]) -> None:
        _, plan = over
        assert not plan.budget.within_budget

    def test_savings_are_measured_and_sorted(
        self, over: tuple[TripRequest, Plan], providers: DataProviders
    ) -> None:
        req, plan = over
        alts = generate_alternatives(plan, req, providers)
        assert alts
        savings = [a.saving_aed for a, _ in alts]
        assert savings == sorted(savings, reverse=True)
        assert "cheaper_hotel" in {a.kind.value for a, _ in alts}
        best, inputs = alts[0]
        replanned = plan_trip(req, inputs, providers)
        assert plan.budget.total - replanned.budget.total == pytest.approx(
            best.saving_aed, abs=0.01
        )

    def test_fit_to_budget(self, over: tuple[TripRequest, Plan], providers: DataProviders) -> None:
        req, plan = over
        fit = fit_to_budget(plan, req, providers)
        assert fit.plan.budget.within_budget
        assert fit.applied and all(a.applied for a in fit.applied)
        it = fit.itinerary
        assert any(a.applied for a in it.alternatives)
        assert it.budget is not None and it.budget.total <= 3000

    def test_nothing_to_do_when_within_budget(self, providers: DataProviders) -> None:
        req = example_request()
        plan = plan_trip(req, PlanInputs(hotel=pick_hotel(req, providers)), providers)
        fit = fit_to_budget(plan, req, providers)
        assert fit.applied == [] and fit.plan is plan
        assert all(not a.applied for a in fit.itinerary.alternatives)

    def test_impossible_budget_stops(self, providers: DataProviders) -> None:
        req = _request(budget_aed=300, days=7)
        plan = plan_trip(
            req, PlanInputs(hotel=pick_hotel(req, providers), options=SchedulerOptions()), providers
        )
        fit = fit_to_budget(plan, req, providers, max_steps=3)
        assert not fit.plan.budget.within_budget  # reported, not hidden
        assert len(fit.applied) <= 3
        assert math.isfinite(fit.plan.budget.total)
