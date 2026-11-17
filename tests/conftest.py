"""Shared fixtures, including a hand-written itinerary for the problem statement's example request.

All prices and travel times here are illustrative test values tagged ``estimated`` — they are not
curated data (that is Phase 2).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.llm import ScriptedLLM

if TYPE_CHECKING:
    from data.providers import DataProviders

from schemas import (
    Attraction,
    Avoidance,
    BudgetBreakdown,
    BudgetCategory,
    CostLine,
    CrowdLevel,
    DayPlan,
    HotelArea,
    HotelTier,
    Interest,
    IssueOrigin,
    Itinerary,
    LatLon,
    Leg,
    MealType,
    OpeningHours,
    PriceBand,
    PriceRange,
    ResearchResult,
    Restaurant,
    ReviewIssue,
    ReviewReport,
    RouteTarget,
    Severity,
    Slot,
    SlotKind,
    Source,
    TimeWindow,
    TransportMode,
    TripConstraints,
    TripRequest,
    Weekday,
    estimated,
)

EXAMPLE_REQUEST_TEXT = (
    "Plan a 4-day trip to Dubai with a budget of AED 5,000, excluding flights. "
    "I enjoy cultural attractions, local food, and beaches. "
    "I prefer affordable hotels and want to avoid crowded places."
)
FIXTURE_BASIS = "test fixture"


# ------------------------------------------------------------------ builders
def t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


def daily(opens: str, closes: str) -> OpeningHours:
    window = TimeWindow(opens=t(opens), closes=t(closes))
    return OpeningHours(weekly={d: [window] for d in Weekday})


ALWAYS_OPEN = OpeningHours(always_open=True)


def attraction(
    pid: str,
    name: str,
    neighborhood: str,
    lat: float,
    lon: float,
    tags: list[str],
    *,
    price: float = 0.0,
    crowd: CrowdLevel = CrowdLevel.LOW,
    duration: int = 90,
    hours: OpeningHours = ALWAYS_OPEN,
    indoor: bool = False,
) -> Attraction:
    return Attraction(
        id=pid,
        name=name,
        category="attraction",
        neighborhood_id=neighborhood,
        location=LatLon(lat=lat, lon=lon),
        tags=tags,
        crowd_level=crowd,
        opening_hours=hours,
        typical_duration_min=duration,
        price=estimated(PriceRange.fixed(price), FIXTURE_BASIS),
        source=Source.ESTIMATED,
        indoor=indoor,
    )


def restaurant(
    pid: str, name: str, neighborhood: str, lat: float, lon: float, cuisine: str, price: float
) -> Restaurant:
    return Restaurant(
        id=pid,
        name=name,
        category="restaurant",
        neighborhood_id=neighborhood,
        location=LatLon(lat=lat, lon=lon),
        tags=["local-food"],
        crowd_level=CrowdLevel.MEDIUM,
        opening_hours=daily("08:00", "23:00"),
        price=estimated(PriceRange(min_aed=price * 0.8, max_aed=price * 1.2), FIXTURE_BASIS),
        source=Source.ESTIMATED,
        cuisine=[cuisine],
        meal_types=[MealType.BREAKFAST, MealType.LUNCH, MealType.DINNER],
        price_band=PriceBand.CASUAL,
    )


def leg(a: str, b: str, mode: TransportMode, minutes: int, km: float, cost: float) -> Leg:
    return Leg(
        from_id=a,
        to_id=b,
        mode=mode,
        duration_min=estimated(minutes, FIXTURE_BASIS),
        distance_km=estimated(km, FIXTURE_BASIS),
        cost_aed=estimated(cost, FIXTURE_BASIS),
    )


def slot(
    start: str,
    end: str,
    kind: SlotKind,
    title: str,
    place_id: str | None = None,
    via: Leg | None = None,
) -> Slot:
    return Slot(start=t(start), end=t(end), kind=kind, title=title, place_id=place_id, leg=via)


def line(
    category: BudgetCategory, description: str, amount: float, day: int | None = None
) -> CostLine:
    return CostLine(
        category=category,
        description=description,
        amount_aed=estimated(amount, FIXTURE_BASIS),
        day=day,
    )


A, M, T_ = SlotKind.ACTIVITY, SlotKind.MEAL, SlotKind.TRANSIT
WALK, TAXI, METRO, ABRA = (
    TransportMode.WALK,
    TransportMode.TAXI,
    TransportMode.METRO,
    TransportMode.ABRA,
)


# ------------------------------------------------------------------ example data
def build_example_request() -> TripRequest:
    return TripRequest(
        raw_text=EXAMPLE_REQUEST_TEXT,
        constraints=TripConstraints(
            days=4,
            budget_aed=5000,
            interests=[Interest.CULTURE, Interest.FOOD, Interest.BEACH],
            hotel_tier=HotelTier.BUDGET,
            avoid=[Avoidance.CROWDS],
        ),
        assumptions=["1 traveler, 1 room", "no travel dates given"],
    )


def build_example_research() -> ResearchResult:
    museum_hours = daily("08:00", "20:00")
    return ResearchResult(
        attractions=[
            attraction(
                "al-fahidi-historical-district",
                "Al Fahidi Historical District",
                "bur-dubai",
                25.2637,
                55.2972,
                ["cultural", "heritage"],
                duration=120,
            ),
            attraction(
                "al-shindagha-museum",
                "Al Shindagha Museum",
                "bur-dubai",
                25.2660,
                55.2895,
                ["cultural", "museum"],
                price=15,
                hours=museum_hours,
                indoor=True,
            ),
            attraction(
                "deira-gold-souk",
                "Deira Gold Souk",
                "deira",
                25.2700,
                55.2970,
                ["cultural", "market"],
                crowd=CrowdLevel.MEDIUM,
            ),
            attraction(
                "deira-spice-souk",
                "Deira Spice Souk",
                "deira",
                25.2687,
                55.2979,
                ["cultural", "market", "food"],
                crowd=CrowdLevel.MEDIUM,
                duration=60,
            ),
            attraction(
                "jumeirah-mosque",
                "Jumeirah Mosque guided tour",
                "jumeirah",
                25.2340,
                55.2650,
                ["cultural", "religious"],
                price=35,
                duration=75,
                hours=daily("10:00", "11:30"),
                indoor=True,
            ),
            attraction(
                "kite-beach",
                "Kite Beach",
                "umm-suqeim",
                25.1580,
                55.2000,
                ["beach", "outdoor"],
                duration=150,
            ),
            attraction(
                "alserkal-avenue",
                "Alserkal Avenue",
                "al-quoz",
                25.1430,
                55.2260,
                ["art", "cultural"],
                hours=daily("10:00", "19:00"),
                indoor=True,
            ),
            attraction(
                "al-mamzar-beach",
                "Al Mamzar Beach Park",
                "al-mamzar",
                25.3000,
                55.3450,
                ["beach", "outdoor"],
                price=5,
                duration=180,
            ),
        ],
        restaurants=[
            restaurant(
                "arabian-tea-house",
                "Arabian Tea House",
                "bur-dubai",
                25.2638,
                55.2995,
                "emirati",
                70,
            ),
            restaurant(
                "al-ustad-kabab",
                "Al Ustad Special Kabab",
                "bur-dubai",
                25.2580,
                55.2960,
                "persian",
                50,
            ),
            restaurant(
                "deira-cafeteria", "Deira street cafeteria", "deira", 25.2695, 55.3000, "indian", 30
            ),
            restaurant(
                "jumeirah-seafood",
                "Jumeirah seafood grill",
                "jumeirah",
                25.2300,
                55.2600,
                "seafood",
                90,
            ),
            restaurant(
                "al-mamzar-grill",
                "Al Mamzar shawarma grill",
                "al-mamzar",
                25.2990,
                55.3400,
                "levantine",
                35,
            ),
        ],
        neighborhood_ids=["bur-dubai", "deira", "jumeirah", "umm-suqeim", "al-quoz", "al-mamzar"],
        rationale={
            "al-fahidi-historical-district": "Free, low-crowd heritage walk; matches culture.",
            "kite-beach": "Free public beach, quieter on weekday afternoons.",
        },
    )


def build_example_days() -> list[DayPlan]:
    hotel = "bur-dubai"
    return [
        DayPlan(
            day=1,
            theme="Old Dubai heritage",
            base_area="bur-dubai",
            day_cost_aed=230,
            slots=[
                slot(
                    "08:00",
                    "09:00",
                    M,
                    "Breakfast",
                    "arabian-tea-house",
                    leg(hotel, "arabian-tea-house", WALK, 10, 0.7, 0),
                ),
                slot(
                    "09:15",
                    "11:15",
                    A,
                    "Al Fahidi Historical District walk",
                    "al-fahidi-historical-district",
                    leg("arabian-tea-house", "al-fahidi-historical-district", WALK, 5, 0.3, 0),
                ),
                slot(
                    "11:30",
                    "13:00",
                    A,
                    "Al Shindagha Museum",
                    "al-shindagha-museum",
                    leg("al-fahidi-historical-district", "al-shindagha-museum", WALK, 15, 1.1, 0),
                ),
                slot(
                    "13:15",
                    "14:30",
                    M,
                    "Lunch",
                    "al-ustad-kabab",
                    leg("al-shindagha-museum", "al-ustad-kabab", TAXI, 10, 2.5, 15),
                ),
            ],
        ),
        DayPlan(
            day=2,
            theme="Deira souks and the Creek",
            base_area="deira",
            day_cost_aed=150,
            slots=[
                slot(
                    "09:00",
                    "09:15",
                    T_,
                    "Abra across Dubai Creek",
                    via=leg(hotel, "deira-spice-souk", ABRA, 10, 0.5, 1),
                ),
                slot("09:15", "10:15", A, "Spice Souk", "deira-spice-souk"),
                slot(
                    "10:20",
                    "11:50",
                    A,
                    "Gold Souk",
                    "deira-gold-souk",
                    leg("deira-spice-souk", "deira-gold-souk", WALK, 5, 0.3, 0),
                ),
                slot(
                    "12:00",
                    "13:00",
                    M,
                    "Lunch",
                    "deira-cafeteria",
                    leg("deira-gold-souk", "deira-cafeteria", WALK, 8, 0.5, 0),
                ),
            ],
        ),
        DayPlan(
            day=3,
            theme="Jumeirah beaches",
            base_area="jumeirah",
            day_cost_aed=320,
            slots=[
                slot(
                    "09:30",
                    "10:00",
                    T_,
                    "Taxi to Jumeirah Mosque",
                    via=leg(hotel, "jumeirah-mosque", TAXI, 20, 6.0, 30),
                ),
                slot("10:00", "11:15", A, "Jumeirah Mosque guided tour", "jumeirah-mosque"),
                slot(
                    "12:00",
                    "13:30",
                    M,
                    "Seafood lunch",
                    "jumeirah-seafood",
                    leg("jumeirah-mosque", "jumeirah-seafood", TAXI, 8, 1.5, 12),
                ),
                slot(
                    "16:00",
                    "18:30",
                    A,
                    "Kite Beach (late afternoon, cooler)",
                    "kite-beach",
                    leg("jumeirah-seafood", "kite-beach", TAXI, 20, 9.0, 30),
                ),
            ],
        ),
        DayPlan(
            day=4,
            theme="Art district and a quiet beach",
            base_area="al-quoz",
            day_cost_aed=260,
            slots=[
                slot(
                    "10:00",
                    "12:00",
                    A,
                    "Alserkal Avenue galleries",
                    "alserkal-avenue",
                    leg(hotel, "alserkal-avenue", TAXI, 25, 14.0, 40),
                ),
                slot(
                    "15:30",
                    "18:30",
                    A,
                    "Al Mamzar Beach Park",
                    "al-mamzar-beach",
                    leg("alserkal-avenue", "al-mamzar-beach", TAXI, 35, 28.0, 70),
                ),
                slot(
                    "18:45",
                    "20:00",
                    M,
                    "Dinner",
                    "al-mamzar-grill",
                    leg("al-mamzar-beach", "al-mamzar-grill", WALK, 10, 0.6, 0),
                ),
            ],
        ),
    ]


def build_example_budget(budget_aed: float = 5000) -> BudgetBreakdown:
    acc, food, trans, act, cont = (
        BudgetCategory.ACCOMMODATION,
        BudgetCategory.FOOD,
        BudgetCategory.TRANSPORT,
        BudgetCategory.ACTIVITIES,
        BudgetCategory.CONTINGENCY,
    )
    lines = [
        line(acc, "Budget hotel, Bur Dubai: 3 nights x AED 300", 900),
        line(acc, "Tourism Dirham: 3 nights", 30),
        *(line(food, f"Meals day {d}", 150, d) for d in range(1, 5)),
        line(trans, "Taxis and abra", 198),
        line(trans, "Airport transfers", 120),
        line(act, "Al Shindagha Museum", 15, 1),
        line(act, "Jumeirah Mosque tour", 35, 3),
        line(act, "Al Mamzar Beach Park entry", 5, 4),
    ]
    subtotal = sum(ln.amount_aed.value for ln in lines)
    lines.append(line(cont, "10% contingency", round(subtotal * 0.10, 2)))
    return BudgetBreakdown.from_lines(lines, budget_aed)


def build_example_review() -> ReviewReport:
    return ReviewReport.from_issues(
        [
            ReviewIssue(
                code="heat_note",
                severity=Severity.MINOR,
                message="Add sunscreen and water for the beach afternoons.",
                route_to=RouteTarget.ASSEMBLE,
                origin=IssueOrigin.LLM,
                day=3,
            )
        ],
        score=88,
        summary="Within budget, low-crowd choices, all three interests covered.",
    )


def build_example_itinerary() -> Itinerary:
    return Itinerary(
        run_id="example-run",
        request=build_example_request(),
        hotel=HotelArea(
            neighborhood_id="bur-dubai",
            tier=HotelTier.BUDGET,
            nightly_rate=estimated(PriceRange(min_aed=250, max_aed=350), FIXTURE_BASIS),
            metro_access=True,
            beach_access=False,
            rationale="Affordable, central, walkable to heritage sites, on the metro.",
        ),
        days=build_example_days(),
        budget=build_example_budget(),
        review=build_example_review(),
    )


# ------------------------------------------------------------------ pytest fixtures
@pytest.fixture
def example_request() -> TripRequest:
    return build_example_request()


@pytest.fixture
def example_research() -> ResearchResult:
    return build_example_research()


@pytest.fixture
def example_itinerary() -> Itinerary:
    return build_example_itinerary()


# ------------------------------------------------------------------ curated dataset (Phase 2)
@pytest.fixture(scope="session")
def seed_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real curated seed data built into a throwaway SQLite database."""
    from data.build_db import build

    db = tmp_path_factory.mktemp("data") / "dubai.sqlite"
    build(db)
    return db


@pytest.fixture(scope="session")
def providers(seed_db: Path) -> Iterator[DataProviders]:
    from data.providers import build_providers

    p = build_providers(seed_db)
    yield p
    p.close()


# ------------------------------------------------------------------ stub LLM (Phase 4)
@pytest.fixture
def fake_llm() -> ScriptedLLM:
    return ScriptedLLM()
