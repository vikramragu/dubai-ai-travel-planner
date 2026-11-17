"""Trip request, constraints and place models."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from schemas import (
    Attraction,
    Avoidance,
    CrowdLevel,
    HotelTier,
    Interest,
    LatLon,
    MealType,
    Neighborhood,
    PriceBand,
    PriceRange,
    ResearchResult,
    Restaurant,
    Source,
    TripConstraints,
    TripRequest,
    estimated,
)


def constraints(**overrides: Any) -> TripConstraints:
    data: dict[str, Any] = {"days": 4, "budget_aed": 5000, "interests": ["culture"]}
    data.update(overrides)
    return TripConstraints.model_validate(data)


class TestTripConstraints:
    def test_example_request(self, example_request: TripRequest) -> None:
        c = example_request.constraints
        assert c.days == 4
        assert c.budget_excludes == ["flights"]
        assert c.hotel_tier is HotelTier.BUDGET
        assert c.avoids_crowds
        assert c.night_count == 3

    @pytest.mark.parametrize("days", [0, 8])
    def test_days_range(self, days: int) -> None:
        with pytest.raises(ValidationError):
            constraints(days=days)

    @pytest.mark.parametrize("budget", [0, -100])
    def test_budget_positive(self, budget: float) -> None:
        with pytest.raises(ValidationError):
            constraints(budget_aed=budget)

    def test_needs_an_interest(self) -> None:
        with pytest.raises(ValidationError):
            constraints(interests=[])

    def test_unknown_interest_rejected(self) -> None:
        with pytest.raises(ValidationError):
            constraints(interests=["skydiving-on-mars"])

    def test_dedupes_lists(self) -> None:
        c = constraints(interests=["food", "food", "beach"], avoid=["crowds", "crowds"])
        assert c.interests == [Interest.FOOD, Interest.BEACH]
        assert c.avoid == [Avoidance.CROWDS]

    def test_rooms_not_more_than_travelers(self) -> None:
        with pytest.raises(ValidationError):
            constraints(travelers=1, rooms=2)

    def test_explicit_nights(self) -> None:
        assert constraints(nights=4).night_count == 4
        with pytest.raises(ValidationError):
            constraints(days=2, nights=3)

    def test_end_date(self) -> None:
        assert constraints(start_date=date(2026, 12, 1)).end_date == date(2026, 12, 4)
        assert constraints().end_date is None

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            constraints(currency="USD")

    def test_request_needs_text(self) -> None:
        with pytest.raises(ValidationError):
            TripRequest(raw_text="  ", constraints=constraints())


class TestPlaces:
    def test_neighborhood(self) -> None:
        n = Neighborhood(id="bur-dubai", name="Bur Dubai", location=LatLon(lat=25.26, lon=55.30))
        assert not n.beach_access

    def test_research_fixture_is_valid(self, example_research: ResearchResult) -> None:
        assert len(example_research.attractions) == 8
        assert "kite-beach" in example_research.place_ids

    def test_free_attraction(self, example_research: ResearchResult) -> None:
        by_id = {a.id: a for a in example_research.attractions}
        assert by_id["kite-beach"].is_free
        assert not by_id["al-shindagha-museum"].is_free

    def test_tags_normalised(self, example_research: ResearchResult) -> None:
        a = example_research.attractions[0].model_copy(update={"tags": [" Cultural ", "cultural"]})
        assert Attraction.model_validate(a.model_dump()).tags == ["cultural"]

    def test_place_bad_id_rejected(self, example_research: ResearchResult) -> None:
        data = example_research.attractions[0].model_dump()
        data["id"] = "Not A Slug"
        with pytest.raises(ValidationError):
            Attraction.model_validate(data)

    def test_restaurant_needs_cuisine_and_meals(self) -> None:
        base: dict[str, Any] = {
            "id": "r1",
            "name": "R",
            "category": "restaurant",
            "neighborhood_id": "deira",
            "location": {"lat": 25.27, "lon": 55.30},
            "crowd_level": CrowdLevel.LOW,
            "price": estimated(PriceRange.fixed(30)),
            "source": Source.ESTIMATED,
            "price_band": PriceBand.STREET,
        }
        with pytest.raises(ValidationError):
            Restaurant.model_validate({**base, "cuisine": [], "meal_types": [MealType.LUNCH]})
        r = Restaurant.model_validate({**base, "cuisine": ["indian"], "meal_types": ["lunch"]})
        assert r.typical_duration_min == 60


class TestResearchResult:
    def test_duplicate_ids_rejected(self, example_research: ResearchResult) -> None:
        dup = example_research.attractions[0]
        with pytest.raises(ValidationError, match="unique"):
            ResearchResult(attractions=[dup, dup])

    def test_rationale_must_reference_known_ids(self) -> None:
        with pytest.raises(ValidationError, match="unknown"):
            ResearchResult(rationale={"ghost-place": "made up"})
