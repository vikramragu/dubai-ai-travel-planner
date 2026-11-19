"""Phase 9 scenario suite (architecture.md §15): golden requests, run end to end.

Each scenario plans a whole trip through the LangGraph planner (no LLM, so the results are
reproducible) and checks the final plan the way a traveller would: right length, within
budget, the asked-for interests and hotel tier, nothing they wanted to avoid, and the review
passed. The error-path tests at the end check that broken AI answers, a dead network and
impossible budgets still end in a clear result, never a crash.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from agents.orchestrator import RequestError
from app.components.prechecks import budget_shortfall, cheapest_night, precheck
from core.config import DataMode, Settings
from core.llm import LLMOutputError, LLMUnavailableError, ScriptedLLM
from core.runner import RunResult, run
from core.tracing import TraceKind
from data.providers import DataProviders, build_providers
from data.providers.overpass import MergedPlaces
from schemas import Attraction, CrowdLevel, HotelTier, Interest, Itinerary, SlotKind

OFFLINE = Settings(_env_file=None)

EXAMPLE = (
    "Plan a 4-day trip to Dubai with a budget of AED 5,000, excluding flights. I enjoy cultural "
    "attractions, local food, and beaches. I prefer affordable hotels and want to avoid crowded "
    "places."
)
ONE_DAY_LOW = "1 day in Dubai on a budget of AED 600, local food and culture, cheap hotel"
LUXURY_WEEK = "7 days in Dubai, luxury hotel, budget AED 30,000, shopping, art and beaches"
OVER_BUDGET = "4 days in Dubai, mid-range hotel, budget AED 3000, culture and beaches, avoid crowds"
AVOID_CROWDS = "3 days in Dubai, budget AED 4,000, culture and shopping, avoid crowds"
IMPOSSIBLE = "7 days in Dubai, AED 300, culture"


@pytest.fixture(scope="module")
def plans(providers: DataProviders) -> dict[str, RunResult]:
    texts = {
        "example": EXAMPLE,
        "one_day_low": ONE_DAY_LOW,
        "luxury_week": LUXURY_WEEK,
        "over_budget": OVER_BUDGET,
        "avoid_crowds": AVOID_CROWDS,
        "crowds_allowed": AVOID_CROWDS.replace(", avoid crowds", ""),
    }
    return {k: run(t, providers=providers, llm=None, settings=OFFLINE) for k, t in texts.items()}


def _attractions(it: Itinerary, providers: DataProviders) -> list[Attraction]:
    places = [providers.places.get_place(pid) for pid in it.place_ids]
    return [p for p in places if isinstance(p, Attraction)]


def _sound(result: RunResult, providers: DataProviders) -> Itinerary:
    """Checks every finished plan must pass."""
    it = result.itinerary
    c = it.request.constraints
    assert it.review is not None and it.review.passed, it.review
    assert it.budget is not None and it.budget.within_budget
    assert it.budget.total <= c.budget_aed
    assert len(it.days) == c.days
    assert all(providers.places.get_place(pid) is not None for pid in it.place_ids)  # no made-up
    seen = [s.place_id for d in it.days for s in d.slots if s.kind is SlotKind.ACTIVITY]
    assert len(seen) == len(set(seen))  # no attraction twice (a breakfast cafe may repeat)
    assert all(d.slots for d in it.days)
    return it


# ------------------------------------------------------------------ golden requests
def test_example_request(plans: dict[str, RunResult], providers: DataProviders) -> None:
    it = _sound(plans["example"], providers)
    c = it.request.constraints
    assert (c.days, c.budget_aed, c.hotel_tier) == (4, 5000, HotelTier.BUDGET)
    assert set(c.interests) == {Interest.CULTURE, Interest.FOOD, Interest.BEACH}
    tags = {t for a in _attractions(it, providers) for t in a.tags}
    assert {"culture", "beach"} <= tags
    assert all(a.crowd_level is not CrowdLevel.HIGH for a in _attractions(it, providers))
    assert plans["example"].stop_reason == "passed" and it.revisions == 0


def test_one_day_low_budget(plans: dict[str, RunResult], providers: DataProviders) -> None:
    it = _sound(plans["one_day_low"], providers)
    assert len(it.days) == 1 and it.budget is not None and it.budget.total <= 600
    assert it.request.constraints.night_count == 0  # a day trip: no hotel night is charged
    assert it.hotel.tier is HotelTier.BUDGET


def test_seven_day_luxury_trip(plans: dict[str, RunResult], providers: DataProviders) -> None:
    it = _sound(plans["luxury_week"], providers)
    assert len(it.days) == 7 and it.hotel.tier is HotelTier.LUXURY
    tags = {t for a in _attractions(it, providers) for t in a.tags}
    assert {"shopping", "art", "beach"} <= tags
    assert it.budget is not None and it.budget.total > 15_000  # a luxury week, not a cheap one


def test_forced_over_budget_is_revised(
    plans: dict[str, RunResult], providers: DataProviders
) -> None:
    result = plans["over_budget"]
    it = _sound(result, providers)
    first = result.history[0].itinerary
    assert first.budget is not None and not first.budget.within_budget  # the first draft failed
    assert first.review is not None and any(i.code == "over_budget" for i in first.review.issues)
    assert it.revisions >= 1 and result.metrics.issues_resolved >= 1
    assert result.steps.count("review") >= 2  # reviewed, revised, reviewed again
    assert "destination" not in result.steps[result.steps.index("review") :]  # only the culprits


def test_avoid_crowds_has_no_crowded_places(
    plans: dict[str, RunResult], providers: DataProviders
) -> None:
    it = _sound(plans["avoid_crowds"], providers)
    assert all(a.crowd_level is not CrowdLevel.HIGH for a in _attractions(it, providers))
    # the same trip without "avoid crowds" does pick crowded favourites, so the filter matters
    allowed = plans["crowds_allowed"].itinerary
    assert any(a.crowd_level is CrowdLevel.HIGH for a in _attractions(allowed, providers))


# ------------------------------------------------------------------ error paths
def test_made_up_answers_from_the_ai_are_ignored(providers: DataProviders) -> None:
    """The AI "hallucinates" places, hotel areas and savings: only real ids are used."""
    llm = ScriptedLLM(
        {
            "DestinationPicks": {
                "attraction_ids": ["the-floating-pyramid", "burj-moon-park"],
                "restaurant_ids": ["imaginary-diner"],
                "rationale": {"the-floating-pyramid": "Amazing."},
            },
            "LogisticsChoice": {"hotel_ranking": ["atlantis-on-mars"], "rationale": {}},
            "BudgetChoice": {"apply_ids": ["free-money"], "explanation": "Saved everything."},
        }
    )
    result = run(OVER_BUDGET, providers=providers, llm=llm, settings=OFFLINE)
    it = _sound(result, providers)
    ids = set(it.place_ids)
    assert not ids & {"the-floating-pyramid", "burj-moon-park", "imaginary-diner"}
    assert it.hotel.neighborhood_id != "atlantis-on-mars"
    asked = {c[1] for c in llm.calls}
    assert {"DestinationPicks", "LogisticsChoice", "BudgetChoice"} <= asked  # really consulted


@pytest.mark.parametrize("error", [LLMOutputError("not JSON"), LLMUnavailableError("429")])
def test_broken_ai_still_gives_a_plan(providers: DataProviders, error: Exception) -> None:
    names = ("ParsedTrip", "DestinationPicks", "LogisticsChoice", "DayThemes", "BudgetChoice")
    llm = ScriptedLLM(dict.fromkeys(names, error))
    result = run(OVER_BUDGET, providers=providers, llm=llm, settings=OFFLINE)
    _sound(result, providers)
    assert any(e.kind is TraceKind.ERROR for e in result.trace)  # the failures are visible


def test_every_live_service_down_still_plans(seed_db: Path, tmp_path: Path) -> None:
    """Live mode with OSRM, Overpass and Open-Meteo all unreachable: the local data takes over."""
    settings = Settings(
        _env_file=None, data_mode=DataMode.LIVE, cache_dir=tmp_path, min_request_interval_s=0
    )
    providers = build_providers(seed_db, settings=settings)
    merged = providers.places.providers[0]  # type: ignore[attr-defined]
    assert isinstance(merged, MergedPlaces)
    merged.live.background = False
    try:
        with respx.mock() as router:
            router.route().mock(side_effect=httpx.ConnectError("network is down"))
            result = run(EXAMPLE, providers=providers, llm=None, settings=settings)
        _sound(result, providers)
        assert any(e.kind is TraceKind.FALLBACK for e in result.trace)
    finally:
        providers.close()


def test_impossible_budget_ends_with_a_clear_message(providers: DataProviders) -> None:
    night = cheapest_night(providers)
    assert night is not None
    warning = [c for c in precheck(IMPOSSIBLE, night_aed=night) if c.level == "warning"]
    assert warning and "cheapest hotels" in warning[0].message  # warned before planning
    result = run(IMPOSSIBLE, providers=providers, llm=None, settings=OFFLINE)
    assert result.stop_reason in {"no_improvement", "max_revisions"}  # it stops, no endless loop
    message = budget_shortfall(result.itinerary)
    assert message is not None and "isn't enough" in message and "fewer days" in message
    fine = run(EXAMPLE, providers=providers, llm=None, settings=OFFLINE)
    assert budget_shortfall(fine.itinerary) is None
    assert not [c for c in precheck(EXAMPLE, night_aed=night) if c.level == "warning"]


@pytest.mark.parametrize(
    ("text", "words"),
    [("3 days in Paris, EUR 2000", "Dubai"), ("4 days in Dubai, culture", "budget")],
)
def test_requests_we_cannot_plan_are_refused_clearly(
    providers: DataProviders, text: str, words: str
) -> None:
    with pytest.raises(RequestError, match=words):
        run(text, providers=providers, llm=None, settings=OFFLINE)
