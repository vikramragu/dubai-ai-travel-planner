"""Tool functions the agents will call (Phase 4 wraps them as LangChain tools)."""

from __future__ import annotations

from datetime import date

import pytest

import tools
from data.providers import DataProviders
from schemas import CrowdLevel, HotelTier, Interest, MealType, Source, TransportMode


def test_search_attractions_for_the_example_request(providers: DataProviders) -> None:
    found = tools.search_attractions(
        [Interest.CULTURE, Interest.BEACH], avoid_crowds=True, limit=30, providers=providers
    )
    assert len(found) >= 10
    assert all(a.crowd_level is not CrowdLevel.HIGH for a in found)
    assert {"culture", "beach"} <= {t for a in found for t in a.tags}


def test_search_restaurants_near_a_place(providers: DataProviders) -> None:
    found = tools.search_restaurants(
        cuisines=["emirati", "arabic"],
        meal_type=MealType.LUNCH,
        near_place_id="al-fahidi-historical-neighbourhood",
        radius_km=2,
        providers=providers,
    )
    assert len(found) >= 3
    with pytest.raises(tools.ToolError, match="unknown place"):
        tools.search_restaurants(near_place_id="nope", providers=providers)


def test_hotel_areas_scale_with_season(providers: DataProviders) -> None:
    base = tools.get_hotel_areas(HotelTier.BUDGET, providers=providers)
    december = tools.get_hotel_areas(HotelTier.BUDGET, month=12, providers=providers)
    july = tools.get_hotel_areas(HotelTier.BUDGET, month=7, providers=providers)
    assert len(base) == len(december) >= 5
    for b, d, j in zip(base, december, july, strict=True):
        assert j.nightly_rate.value.max_aed < b.nightly_rate.value.max_aed
        assert d.nightly_rate.value.max_aed > b.nightly_rate.value.max_aed
        assert "month 12" in (d.nightly_rate.reference or "")


def test_hotel_fees(providers: DataProviders) -> None:
    fees = tools.hotel_fees(HotelTier.MID, providers=providers)
    assert fees.total_per_night(500) == pytest.approx(500 * 1.22 + 15)


def test_travel_time_options_sorted(providers: DataProviders) -> None:
    legs = tools.travel_time("al-fahidi", "al-ras", providers=providers)
    modes = {leg.mode for leg in legs}
    assert {TransportMode.TAXI, TransportMode.ABRA} <= modes
    assert TransportMode.WALK not in modes  # Dubai Creek is in the way
    durations = [leg.duration_min.value for leg in legs]
    assert durations == sorted(durations)
    one = tools.travel_time("al-fahidi", "al-ras", TransportMode.TAXI, providers=providers)
    assert [leg.mode for leg in one] == [TransportMode.TAXI]
    assert tools.travel_time("al-ras", "al-ras", providers=providers) == []
    with pytest.raises(tools.ToolError):
        tools.travel_time("al-ras", "al-fahidi", party_size=0, providers=providers)


def test_travel_matrix(providers: DataProviders) -> None:
    m = tools.travel_matrix(["al-rigga", "al-karama", "jumeirah"], providers=providers)
    assert m.lookup("jumeirah", "al-rigga", TransportMode.TAXI) is not None


def test_transit_fare(providers: DataProviders) -> None:
    taxi = tools.transit_fare(TransportMode.TAXI, 10, providers=providers)
    assert taxi.value == pytest.approx(5 + 2.45 * 10)
    assert taxi.source is Source.VERIFIED  # official tariff applied to a given distance
    night = tools.transit_fare(TransportMode.TAXI, 10, night=True, providers=providers)
    assert night.value > taxi.value
    metro = tools.transit_fare(TransportMode.METRO, 20, providers=providers)
    assert metro.value == 7.5 and metro.source is Source.ESTIMATED  # zone band is inferred
    assert tools.transit_fare(TransportMode.WALK, 3, providers=providers).value == 0
    assert tools.transit_fare(TransportMode.ABRA, 1, providers=providers).value == 1
    assert tools.transit_fare(TransportMode.RIDE_HAIL, 1, providers=providers).value == 13
    with pytest.raises(tools.ToolError):
        tools.transit_fare(TransportMode.TAXI, -1, providers=providers)


def test_weather_forecast(providers: DataProviders) -> None:
    days = tools.weather_forecast(date(2026, 12, 10), 4, providers=providers)
    assert len(days) == 4 and days[0].heat.value == "comfortable"
    with pytest.raises(tools.ToolError):
        tools.weather_forecast(date(2026, 12, 10), 0, providers=providers)


def test_price_lookup_and_get_place(providers: DataProviders) -> None:
    price = tools.price_lookup("al-shindagha-museum", providers=providers)
    assert price.source is Source.VERIFIED and price.value.min_aed == 50
    assert tools.get_place("kite-beach", providers=providers).is_free  # type: ignore[union-attr]
    with pytest.raises(tools.ToolError):
        tools.get_place("nope", providers=providers)
    assert len(tools.list_neighborhoods(providers=providers)) >= 15
