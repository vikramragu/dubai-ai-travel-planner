"""The five agents with a stubbed LLM (canned structured outputs) and without one."""

from __future__ import annotations

from datetime import date

import pytest

from agents.base import PROMPTS, AgentContext, feedback, load_prompt
from agents.budget import BudgetAgent, is_downgrade
from agents.destination import DestinationAgent
from agents.logistics import LogisticsAgent
from agents.orchestrator import Orchestrator
from agents.review import ReviewAgent
from agents.skills.budget_engine import Plan, PlanInputs, plan_trip
from agents.skills.rule_checks import check_itinerary
from agents.skills.scheduler import SchedulerOptions, schedule, suits_diet
from agents.skills.scripted import example_request, pick_hotel
from core.llm import ScriptedLLM as FakeLLM
from core.tracing import TraceKind
from data.providers import DataProviders
from schemas import (
    Avoidance,
    CrowdLevel,
    HotelTier,
    Interest,
    IssueOrigin,
    Itinerary,
    ReviewIssue,
    RouteTarget,
    Severity,
    SlotKind,
    TripRequest,
)


def _req(**changes: object) -> TripRequest:
    r = example_request()
    return r.model_copy(update={"constraints": r.constraints.model_copy(update=changes)})


def _ctx(providers: DataProviders, llm: FakeLLM | None = None) -> AgentContext:
    return AgentContext(providers=providers, llm=llm)


def test_every_prompt_loads() -> None:
    names = sorted(p.stem for p in PROMPTS.glob("*.md"))
    assert len(names) == 6
    for name in names:  # substitute() raises on a stray "$" or a missing placeholder
        text = load_prompt(name, today="2026-11-15", revision_feedback=feedback([]))
        assert "$today" not in text and "$revision_feedback" not in text


# ------------------------------------------------------------------ destination
class TestDestination:
    def test_without_llm(self, providers: DataProviders) -> None:
        out = DestinationAgent(_ctx(providers)).research(example_request())
        r = out.research
        assert len(r.attractions) >= 12
        assert all(a.crowd_level is not CrowdLevel.HIGH for a in r.attractions)
        assert set(r.rationale) >= {a.id for a in r.attractions}
        assert all(isinstance(v, str) and v for v in r.rationale.values())
        assert not out.assumptions  # culture, food and beach are well covered

    def test_coverage_note_for_thin_interests(self, providers: DataProviders) -> None:
        req = _req(interests=[Interest.NIGHTLIFE], avoid=[Avoidance.CROWDS])
        out = DestinationAgent(_ctx(providers)).research(req)
        assert any("nightlife" in a for a in out.assumptions)

    def test_seasonal_places_left_out_in_summer(self, providers: DataProviders) -> None:
        req = _req(interests=[Interest.NIGHTLIFE], avoid=[], start_date=date(2027, 7, 1))
        ids = {a.id for a in DestinationAgent(_ctx(providers)).research(req).research.attractions}
        assert "global-village" not in ids
        winter = _req(interests=[Interest.NIGHTLIFE], avoid=[], start_date=date(2027, 1, 4))
        assert "global-village" in {
            a.id for a in DestinationAgent(_ctx(providers)).research(winter).research.attractions
        }

    def test_flagged_places_are_excluded(self, providers: DataProviders) -> None:
        issue = ReviewIssue(
            code="crowded_place",
            severity=Severity.MAJOR,
            message="m",
            route_to=RouteTarget.DESTINATION,
            place_id="al-fahidi-historical-neighbourhood",
        )
        out = DestinationAgent(_ctx(providers)).research(example_request(), issues=[issue])
        assert issue.place_id not in {a.id for a in out.research.attractions}

    def test_dietary_restaurants(self, providers: DataProviders) -> None:
        out = DestinationAgent(_ctx(providers)).research(_req(dietary=["vegetarian"]))
        assert out.research.restaurants
        assert all(suits_diet(r, ["vegetarian"]) for r in out.research.restaurants)

    def test_llm_picks_are_validated(self, providers: DataProviders) -> None:
        seen: list[dict] = []  # type: ignore[type-arg]

        def answer(payload: dict) -> dict:  # type: ignore[type-arg]
            seen.append(payload)
            third, first = payload["attractions"][2]["id"], payload["attractions"][0]["id"]
            return {
                "attraction_ids": [third, "burj-al-moon", first],
                "restaurant_ids": [payload["restaurants"][0]["id"]],
                "rationale": {third: "Quiet and free.", "burj-al-moon": "x"},
            }

        llm = FakeLLM({"DestinationPicks": answer})
        ctx = _ctx(providers, llm)
        out = DestinationAgent(ctx).research(example_request())
        r = out.research
        assert out.dropped_ids == ["burj-al-moon"]
        third, first = seen[0]["attractions"][2]["id"], seen[0]["attractions"][0]["id"]
        assert [a.id for a in r.attractions[:2]] == [third, first]  # the LLM's order is kept
        assert r.rationale[third] == "Quiet and free."
        assert "burj-al-moon" not in r.rationale
        assert len(r.attractions) >= 4 * 4  # topped up to ~4 per day
        assert len(r.restaurants) == 1
        assert any(e.kind is TraceKind.ERROR and "burj-al-moon" in e.summary for e in ctx.events)
        assert "There are no descriptions or ratings" in llm.calls[0][2]


# ------------------------------------------------------------------ logistics
class TestLogistics:
    def test_without_llm(self, providers: DataProviders) -> None:
        out = LogisticsAgent(_ctx(providers)).plan(example_request())
        assert out.recommended_hotel.tier is HotelTier.BUDGET
        assert out.recommended_hotel.metro_access
        modes = {o.mode.value for o in out.transport_options}
        assert {"metro", "taxi", "abra", "walk"} <= modes
        metro = next(o for o in out.transport_options if o.mode.value == "metro")
        assert "3 / 5 / 7.5" in metro.summary  # official RTA fares
        assert len(out.travel_matrix.entries) > 200

    def test_cost_issue_puts_price_first(self, providers: DataProviders) -> None:
        req = _req(hotel_tier=HotelTier.MID)
        issue = ReviewIssue(
            code="hotel_cost_driver",
            severity=Severity.MAJOR,
            message="m",
            route_to=RouteTarget.LOGISTICS,
        )
        out = LogisticsAgent(_ctx(providers)).plan(req, issues=[issue])
        mids = [a.nightly_rate.value.midpoint for a in out.hotel_areas]
        assert mids[0] == min(mids)

    def test_llm_reorders_and_unknown_areas_are_ignored(self, providers: DataProviders) -> None:
        llm = FakeLLM(
            {
                "LogisticsChoice": {
                    "hotel_ranking": ["atlantis-city", "al-karama"],
                    "rationale": {"al-karama": "Central and cheap.", "atlantis-city": "x"},
                    "transport_notes": "Metro for long hops.",
                }
            }
        )
        ctx = _ctx(providers, llm)
        out = LogisticsAgent(ctx).plan(example_request())
        assert out.recommended_hotel.neighborhood_id == "al-karama"
        assert out.recommended_hotel.rationale == "Central and cheap."
        assert out.notes == "Metro for long hops."
        assert len(out.hotel_areas) == 5
        assert any("atlantis-city" in e.summary for e in ctx.events if e.kind is TraceKind.ERROR)


# ------------------------------------------------------------------ orchestrator assemble
class TestAssemble:
    def test_scheduler_themes_without_llm(self, providers: DataProviders) -> None:
        ctx = _ctx(providers)
        req = example_request()
        research = DestinationAgent(ctx).research(req).research
        hotel = LogisticsAgent(ctx).plan(req)
        plan = Orchestrator(ctx).assemble(req, research, hotel)
        it = plan.itinerary
        assert len(it.days) == 4 and it.hotel == hotel.recommended_hotel
        ids = {s.place_id for d in it.days for s in d.slots if s.kind is SlotKind.ACTIVITY}
        assert ids <= {a.id for a in research.attractions}  # only the agent's picks

    def test_llm_themes(self, providers: DataProviders) -> None:
        llm = FakeLLM(
            {
                "DayThemes": lambda p: {
                    "days": [{"day": d["day"], "theme": f"Theme {d['day']}"} for d in p["days"]]
                }
            }
        )
        ctx = _ctx(providers, llm)
        req = example_request()
        plan = Orchestrator(ctx).assemble(
            req, DestinationAgent(ctx).research(req).research, LogisticsAgent(ctx).plan(req)
        )
        assert [d.theme for d in plan.itinerary.days] == [
            "Theme 1",
            "Theme 2",
            "Theme 3",
            "Theme 4",
        ]


# ------------------------------------------------------------------ budget
@pytest.fixture(scope="module")
def over_plan(providers: DataProviders) -> tuple[TripRequest, Plan]:
    req = _req(hotel_tier=HotelTier.MID, budget_aed=3000)
    return req, plan_trip(req, PlanInputs(hotel=pick_hotel(req, providers)), providers)


class TestBudget:
    def test_within_budget_has_no_alternatives(self, providers: DataProviders) -> None:
        req = example_request()
        plan = plan_trip(req, PlanInputs(hotel=pick_hotel(req, providers)), providers)
        ev = BudgetAgent(_ctx(providers)).evaluate(plan, req)
        assert ev.budget.within_budget and ev.alternatives == []

    def test_over_budget_is_fixed(
        self, providers: DataProviders, over_plan: tuple[TripRequest, Plan]
    ) -> None:
        req, plan = over_plan
        agent = BudgetAgent(_ctx(providers))
        ev = agent.evaluate(plan, req)
        assert not ev.budget.within_budget and ev.alternatives
        rev = agent.revise(plan, req)
        assert rev.plan.budget.within_budget and rev.applied
        blocking = {i.code for i in check_itinerary(rev.plan.itinerary, providers) if i.blocks_pass}
        assert blocking == set()  # no fix introduced a new problem

    def test_downgrade_is_explained(
        self, providers: DataProviders, over_plan: tuple[TripRequest, Plan]
    ) -> None:
        req, plan = over_plan
        rev = BudgetAgent(_ctx(providers)).revise(plan, req)
        if any(is_downgrade(a, plan) for a in rev.applied):
            assert any("because no mid option fits" in n for n in rev.notes)

    def test_llm_cannot_pick_a_downgrade_while_same_tier_options_lead(
        self,
        providers: DataProviders,
        over_plan: tuple[TripRequest, Plan],
    ) -> None:
        req, plan = over_plan
        agent = BudgetAgent(_ctx(providers))
        options = agent._ordered(plan, req)
        downgrade = next(a for a, _ in options if is_downgrade(a, plan))
        assert not is_downgrade(options[0][0], plan)  # same-tier first
        agent.ctx.llm = FakeLLM({"BudgetChoice": {"apply_ids": [downgrade.id]}})
        alt, _ = agent._choose(plan, options, [])
        assert not is_downgrade(alt, plan)
        # ...but when the engine puts the downgrade first (nothing else fits), it is allowed
        first = [o for o in options if o[0].id == downgrade.id] + [
            o for o in options if o[0].id != downgrade.id
        ]
        alt, _ = agent._choose(plan, first, [])
        assert alt.id == downgrade.id

    def test_downgrade_first_when_same_tier_cannot_close_the_gap(
        self,
        providers: DataProviders,
        over_plan: tuple[TripRequest, Plan],
    ) -> None:
        req, plan = over_plan
        options = BudgetAgent(_ctx(providers))._ordered(plan, req)
        reordered = BudgetAgent._downgrade_first_if_needed(plan, options)
        same_sum = sum(a.saving_aed for a, _ in options if not is_downgrade(a, plan))
        assert is_downgrade(reordered[0][0], plan) == (same_sum < -plan.budget.remaining_aed)


# ------------------------------------------------------------------ review
class TestReview:
    def _plan(self, providers: DataProviders) -> Itinerary:
        req = example_request()
        return plan_trip(req, PlanInputs(hotel=pick_hotel(req, providers)), providers).itinerary

    def test_rules_only(self, providers: DataProviders) -> None:
        report = ReviewAgent(_ctx(providers)).review(self._plan(providers))
        assert report.passed and report.score == 100

    def test_llm_comments_are_minor_and_never_block(self, providers: DataProviders) -> None:
        llm = FakeLLM(
            {
                "LLMReview": {
                    "comments": [
                        {
                            "message": "Day 2 is museum-heavy; add a beach.",
                            "day": 2,
                            "route_to": "destination",
                        },
                        {"message": "Long gap before dinner.", "day": 6},
                    ],
                    "summary": "A solid, quiet plan.",
                }
            }
        )
        report = ReviewAgent(_ctx(providers, llm)).review(self._plan(providers))
        llm_issues = [i for i in report.issues if i.origin is IssueOrigin.LLM]
        assert len(llm_issues) == 2
        assert all(i.severity is Severity.MINOR for i in llm_issues)
        assert llm_issues[1].day is None  # a 4-day trip has no day 6
        assert report.passed and report.score == 94
        assert "A solid, quiet plan." in report.summary


# ------------------------------------------------------------------ scheduler dietary addition
def test_vegetarian_meals(providers: DataProviders) -> None:
    req = _req(dietary=["vegetarian"], interests=[Interest.CULTURE], avoid=[])
    it = schedule(req, pick_hotel(req, providers), providers).itinerary
    meals = [s for d in it.days for s in d.slots if s.kind is SlotKind.MEAL]
    assert meals
    for s in meals:
        r = providers.places.get_place(s.place_id or "")
        assert r is not None and suits_diet(r, ["vegetarian"])  # type: ignore[arg-type]
    assert "dietary_not_met" not in {i.code for i in check_itinerary(it, providers)}


def test_dietary_rule(providers: DataProviders) -> None:
    req = example_request()
    it = schedule(req, pick_hotel(req, providers), providers).itinerary
    veg = it.model_copy(update={"request": _req(dietary=["vegetarian"])})
    codes = [i.code for i in check_itinerary(veg, providers)]
    assert "dietary_not_met" in codes


def test_preferred_restaurants(providers: DataProviders) -> None:
    """A restaurant the Destination agent recommended wins over the default pick."""
    from agents.skills.scheduler import Scheduler
    from schemas import MealType

    req = example_request()
    hotel = pick_hotel(req, providers)
    fahidi = providers.places.get_place("al-fahidi-historical-neighbourhood")
    assert fahidi is not None
    day = Scheduler(req, hotel, providers).calendar[0]

    def lunch(preferred: frozenset[str]) -> str | None:
        sch = Scheduler(req, hotel, providers, SchedulerOptions(preferred_restaurants=preferred))
        r = sch._restaurant(MealType.LUNCH, fahidi.location, 13 * 60, day, frozenset(), "al-fahidi")
        return r.id if r else None

    default = lunch(frozenset())
    assert default is not None
    others = [
        r.id
        for r in DestinationAgent(_ctx(providers)).research(req).research.restaurants
        if r.id != default and r.neighborhood_id == "al-fahidi" and MealType.LUNCH in r.meal_types
    ]
    assert others
    assert lunch(frozenset({others[0]})) == others[0]
