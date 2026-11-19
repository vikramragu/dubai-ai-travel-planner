"""Phase 8: children and must-visit places, from the request contract to the final plan."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from agents.base import AgentContext
from agents.destination import DestinationAgent
from agents.orchestrator import Orchestrator
from agents.skills.places_match import match_name, places_in_text
from agents.skills.rule_checks import check_itinerary
from agents.skills.scheduler import FAMILY_BOOST, score
from core.config import Settings
from core.runner import run
from data.providers import AttractionQuery, DataProviders
from schemas import Interest, TripConstraints

OFFLINE = Settings(_env_file=None)


def _ctx(providers: DataProviders) -> AgentContext:
    return AgentContext(providers=providers, settings=OFFLINE)


# ------------------------------------------------------------------ contract
def test_children_and_must_visit_contract() -> None:
    c = TripConstraints(
        days=3,
        budget_aed=5000,
        interests=[Interest.CULTURE],
        travelers=4,
        children=2,
        must_visit=["kite-beach", "kite-beach", "etihad-museum"],
    )
    assert c.adults == 2 and c.must_visit == ["kite-beach", "etihad-museum"]
    with pytest.raises(ValidationError, match="adult"):
        TripConstraints(days=1, budget_aed=1, interests=[Interest.CULTURE], travelers=2, children=2)
    with pytest.raises(ValidationError):
        TripConstraints(
            days=1,
            budget_aed=1,
            interests=[Interest.CULTURE],
            must_visit=[f"p{i}" for i in range(11)],
        )


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize(
    ("text", "travelers", "children"),
    [
        ("4 days in Dubai, AED 6000, family of four with two kids, culture", 4, 2),
        ("Dubai 3 days budget 4000, 2 adults and 3 children, culture", 5, 3),
        ("Dubai with my son, 2 days, AED 2000, culture", 2, 1),  # 1 adult assumed
        ("We are a family with 2 kids, 3 days in Dubai, AED 5000, beaches", 4, 2),  # 2 adults
        ("4 days in Dubai, AED 5000, culture", 1, 0),
    ],
)
def test_parser_reads_children(
    providers: DataProviders, text: str, travelers: int, children: int
) -> None:
    r = Orchestrator(_ctx(providers)).parse(text).request
    assert (r.constraints.travelers, r.constraints.children) == (travelers, children)
    if children:
        assert any("adult prices" in a for a in r.assumptions)


def test_must_visit_from_text(providers: DataProviders) -> None:
    places = providers.places.search_attractions(AttractionQuery(limit=1000))
    text = "I want to see the Etihad Museum and Kite Beach, but please avoid Dubai Mall"
    ids = places_in_text(text, places)
    assert ids[:2] == ["etihad-museum", "kite-beach"] and "dubai-mall" not in ids
    # a short name inside a longer one is one mention
    assert places_in_text("visit Hatta Heritage Village", places) == ["hatta-heritage-village"]
    assert places_in_text("a museum and a beach", places) == []  # general wishes pin nothing
    assert match_name("etihad museum", places) == "etihad-museum"
    assert match_name("museum", places) is None  # ambiguous


def test_unknown_must_visit_names_are_reported(providers: DataProviders) -> None:
    from agents.orchestrator import ParsedTrip

    orch = Orchestrator(_ctx(providers))
    parsed = ParsedTrip(
        days=2,
        budget_amount=3000,
        interests=[Interest.CULTURE],
        must_visit=["Etihad Museum", "Atlantis Lost City"],
    )
    from agents.orchestrator import normalize

    req, _ = normalize(parsed, "Dubai trip", OFFLINE)
    req = orch._must_visit(req, parsed)
    assert req.constraints.must_visit == ["etihad-museum"]
    assert any("Atlantis Lost City" in a and "isn't in the dataset" in a for a in req.assumptions)


# ------------------------------------------------------------------ agents and scheduler
def test_destination_keeps_must_visit_first(providers: DataProviders) -> None:
    orch = Orchestrator(_ctx(providers))
    req = orch.parse("2 days in Dubai, AED 3000, culture, avoid crowds").request
    # Dubai Mall is a high-crowd shopping place: the filters alone would never offer it
    c = req.constraints.model_copy(update={"must_visit": ["dubai-mall"]})
    req = req.model_copy(update={"constraints": c})
    found = DestinationAgent(_ctx(providers)).research(req).research
    assert found.attractions[0].id == "dubai-mall"
    assert found.rationale["dubai-mall"] == "You asked to include this place."


def test_family_places_score_higher(providers: DataProviders) -> None:
    place = next(
        a
        for a in providers.places.search_attractions(AttractionQuery(limit=1000))
        if "family" in a.tags and "culture" in a.tags
    )
    base = TripConstraints(days=2, budget_aed=3000, interests=[Interest.CULTURE], travelers=2)
    kids = base.model_copy(update={"children": 1})
    assert score(place, kids, 12) == pytest.approx(score(place, base, 12) * FAMILY_BOOST, abs=1e-3)


# ------------------------------------------------------------------ end to end
def test_family_trip_includes_must_visit_places(providers: DataProviders) -> None:
    text = (
        "3 days in Dubai, AED 6000, 2 adults and 2 kids, culture and beaches, "
        "I want to see the Etihad Museum and Kite Beach"
    )
    it = run(text, providers=providers, llm=None, settings=OFFLINE).itinerary
    assert {"etihad-museum", "kite-beach"} <= set(it.place_ids)
    assert it.review is not None and it.review.passed
    assert it.request.constraints.children == 2


@pytest.mark.parametrize(
    ("text", "place", "reason"),
    [
        (
            "2 days in Dubai in July, AED 3000, culture, must see Global Village",
            "global-village",
            "closed in July",
        ),
        (
            "2 days in Dubai, AED 4000, culture, I'd love to visit Hatta Heritage Village",
            "hatta-heritage-village",
            "from the hotel area",
        ),
    ],
)
def test_impossible_must_visit_is_explained_not_forced(
    providers: DataProviders, text: str, place: str, reason: str
) -> None:
    it = run(text, providers=providers, llm=None, settings=OFFLINE).itinerary
    assert place not in it.place_ids
    issue = next(i for i in it.review.issues if i.code == "must_visit_missing")  # type: ignore[union-attr]
    assert reason in issue.message and issue.severity.value == "minor"
    assert it.review is not None and it.review.passed  # a known reason doesn't block the plan


def test_unexplained_missing_must_visit_is_major(providers: DataProviders) -> None:
    it = run("2 days in Dubai, AED 4000, culture", providers=providers, llm=None, settings=OFFLINE)
    plan = it.itinerary
    c = plan.request.constraints.model_copy(update={"must_visit": ["kite-beach"]})
    forced = plan.model_copy(update={"request": plan.request.model_copy(update={"constraints": c})})
    assert "kite-beach" not in forced.place_ids  # a culture-only plan has no beach
    issues = check_itinerary(forced, providers, month=date(2026, 12, 1).month)
    issue = next(i for i in issues if i.code == "must_visit_missing")
    assert issue.severity.value == "major" and issue.route_to.value == "assemble"


def test_budget_llm_cannot_skip_a_needed_downgrade(providers: DataProviders) -> None:
    """When only a cheaper tier can close the gap, the LLM picking small same-tier swaps
    first is overruled (seen with gpt-oss: metro first, then every hotel move broke a rule)."""
    from core.llm import ScriptedLLM

    def pick_small_swaps(p: dict) -> dict:  # type: ignore[type-arg]
        same = [a["id"] for a in p["alternatives"] if not a["changes_hotel_tier"]]
        return {"apply_ids": same, "explanation": "small swaps first"}

    llm = ScriptedLLM({"BudgetChoice": pick_small_swaps})
    text = "4 days in Dubai, mid-range hotel, budget AED 3000, culture and beaches, avoid crowds"
    result = run(text, providers=providers, llm=llm, settings=OFFLINE)
    applied = [
        e.summary for e in result.trace if e.agent == "budget" and e.summary.startswith("Applied:")
    ]
    assert applied and "hotel area" in applied[0]  # the downgrade first, as the engine decided
    it = result.itinerary
    assert it.budget is not None and it.budget.within_budget
