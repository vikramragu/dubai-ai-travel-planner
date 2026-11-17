"""Request parsing: the keyword fallback, the normalisation rules and the LLM path.

The five sample requests cover the data's edge cases (implementation-plan Phase 4): a month or
dates, a USD budget, more than 7 days, a non-Dubai destination, a vegetarian diet, 2 travellers
in 1 room, and phrases that map to interests.
"""

from __future__ import annotations

from datetime import date

import pytest

from agents.base import AgentContext
from agents.orchestrator import (
    Orchestrator,
    ParsedTrip,
    RequestError,
    keyword_parse,
    normalize,
)
from core.config import Settings
from core.llm import ScriptedLLM as FakeLLM
from core.tracing import TraceKind
from data.providers import DataProviders
from schemas import Avoidance, HotelTier, Interest, Pace, TripRequest

TODAY = date(2026, 11, 15)
SETTINGS = Settings(_env_file=None)


def parse(text: str) -> tuple[TripRequest, int | None]:
    return normalize(keyword_parse(text, today=TODAY), text, SETTINGS)


def test_example_request() -> None:
    req, month = parse(
        "Plan a 4-day trip to Dubai with a budget of AED 5,000, excluding flights. I enjoy "
        "cultural attractions, local food, and beaches. I prefer affordable hotels and want "
        "to avoid crowded places."
    )
    c = req.constraints
    assert (c.days, c.budget_aed, c.hotel_tier) == (4, 5000, HotelTier.BUDGET)
    assert c.interests == [Interest.CULTURE, Interest.FOOD, Interest.BEACH]
    assert c.avoid == [Avoidance.CROWDS] and c.budget_excludes == ["flights"]
    assert month is None and any("December" in a for a in req.assumptions)


def test_couple_in_march_usd_vegetarian() -> None:
    req, month = parse(
        "My wife and I want 5 days in Dubai in March, budget USD 3000, one room, mid-range "
        "hotel. We love museums, art and nightlife. We're vegetarian."
    )
    c = req.constraints
    assert (c.travelers, c.rooms, c.days) == (2, 1, 5)
    assert c.budget_aed == pytest.approx(3000 * 3.6725)
    assert month == 3 and c.start_date is None
    assert c.interests == [Interest.CULTURE, Interest.ART, Interest.NIGHTLIFE]
    assert c.dietary == ["vegetarian"] and c.hotel_tier is HotelTier.MID
    assert any("Converted USD" in a for a in req.assumptions)


def test_long_family_trip_is_capped() -> None:
    req, _ = parse(
        "10 day luxury holiday in Dubai for a family of 4, 2 rooms, budget 40k AED, kids love "
        "water parks and the beach, relaxed pace please"
    )
    c = req.constraints
    assert c.days == 7 and any("10 days" in a for a in req.assumptions)
    assert (c.travelers, c.rooms, c.budget_aed) == (4, 2, 40_000)
    assert c.hotel_tier is HotelTier.LUXURY and c.pace is Pace.RELAXED
    assert set(c.interests) == {Interest.BEACH, Interest.ADVENTURE, Interest.FAMILY}


def test_other_destination_is_refused() -> None:
    with pytest.raises(RequestError, match="only covers Dubai"):
        parse("Weekend trip to Abu Dhabi, 2 days, AED 2000")


def test_dates_gbp_avoidances_and_vegan() -> None:
    req, month = parse(
        "3 days in Dubai starting 2027-01-15, budget £1,500, solo, adventure and desert, "
        "I hate early mornings and don't like long walks, vegan"
    )
    c = req.constraints
    assert c.start_date == date(2027, 1, 15) and month is None
    assert c.budget_aed == pytest.approx(1500 * 4.7)
    assert set(c.avoid) == {Avoidance.EARLY_MORNINGS, Avoidance.LONG_WALKS}
    assert set(c.interests) == {Interest.ADVENTURE, Interest.NATURE}
    assert c.dietary == [] and any("vegan" in a for a in req.assumptions)
    assert any("mid-range" in a for a in req.assumptions)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("4 days in Dubai, culture", "include a total budget"),
        ("4 days in Dubai, budget 5000 yen", "include a total budget"),
        ("4 days in Dubai, budget 5000 €", "include a total budget"),
        ("4 days in Dubai, I have 5000", "include a total budget"),  # no "budget", no currency
    ],
)
def test_missing_budget(text: str, match: str) -> None:
    with pytest.raises(RequestError, match=match):
        parse(text)


def test_normalize_rules() -> None:
    with pytest.raises(RequestError, match="aren't supported"):
        normalize(ParsedTrip(budget_amount=100, budget_currency="JPY"), "x", SETTINGS)
    with pytest.raises(RequestError, match="at least 1 day"):
        normalize(ParsedTrip(days=0, budget_amount=100), "x", SETTINGS)
    req, _ = normalize(ParsedTrip(budget_amount=2000, travelers=3), "x", SETTINGS)
    c = req.constraints
    assert c.days == 3 and c.rooms == 2  # defaults, recorded
    assert c.interests == [Interest.CULTURE, Interest.FOOD]
    assert len(req.assumptions) >= 3


def test_date_without_year_is_the_next_one() -> None:
    p = keyword_parse("2 days in Dubai from 10 March, AED 1000", today=TODAY)
    assert p.start_date == date(2027, 3, 10)
    p = keyword_parse("2 days in Dubai from December 20, AED 1000", today=TODAY)
    assert p.start_date == date(2026, 12, 20)


def test_llm_parse_is_used_and_normalised(providers: DataProviders, fake_llm: object) -> None:

    llm = FakeLLM(
        {
            "ParsedTrip": {
                "days": 12,
                "budget_amount": 1000,
                "budget_currency": "USD",
                "interests": ["beach"],
            }
        }
    )
    ctx = AgentContext(providers=providers, llm=llm, settings=SETTINGS)
    result = Orchestrator(ctx).parse("anything")
    assert result.used_llm and result.request.constraints.days == 7
    assert result.request.constraints.budget_aed == pytest.approx(3672.5)
    assert llm.calls[0][1] == "ParsedTrip" and "Today is" in llm.calls[0][2]


def test_llm_failure_falls_back_to_keywords(providers: DataProviders) -> None:

    ctx = AgentContext(providers=providers, llm=FakeLLM(), settings=SETTINGS)
    result = Orchestrator(ctx).parse("3 days in Dubai, AED 3000, beaches")
    assert not result.used_llm and result.request.constraints.interests == [Interest.BEACH]
    assert any(e.kind is TraceKind.ERROR for e in ctx.events)


@pytest.mark.parametrize(
    ("text", "aed"),
    [
        ("Dubai budget 5000", 5000),
        ("3 days in Dubai, budget of 4k, culture", 4000),
        ("Dubai, budget: 2,500.", 2500),
        ("Dubai trip, my budget is around 6000 and I like beaches", 6000),
    ],
)
def test_budget_without_a_currency_is_aed(text: str, aed: float) -> None:
    req, _ = parse(text)
    assert req.constraints.budget_aed == aed
    assert any("assumed AED" in a for a in req.assumptions)


def test_explicit_currency_needs_no_assumption() -> None:
    req, _ = parse("Dubai budget AED 5000")
    assert req.constraints.budget_aed == 5000
    assert not any("assumed AED" in a for a in req.assumptions)
