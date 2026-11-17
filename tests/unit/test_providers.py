"""Local SQLite provider and the fallback chains."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path

import pytest

from core.config import Settings
from core.tracing import TraceEvent, TraceKind
from data.providers import (
    AttractionQuery,
    DataProviders,
    LocalProvider,
    ProviderError,
    RestaurantQuery,
    get_providers,
)
from data.providers.chained import ChainedPlacesProvider, ChainedRoutesProvider
from schemas import (
    Attraction,
    CrowdLevel,
    HotelTier,
    Interest,
    Leg,
    MealType,
    Neighborhood,
    PriceBand,
    Restaurant,
    Source,
    TaxiKind,
    TransportMode,
    TravelMatrix,
)
from schemas.pricing import FareProduct, ZoneBand


@pytest.fixture(scope="module")
def local(seed_db: Path) -> Iterator[LocalProvider]:
    provider = LocalProvider(seed_db)
    yield provider
    provider.close()


class TestPlaces:
    def test_interest_filter(self, local: LocalProvider) -> None:
        found = local.search_attractions(AttractionQuery(interests=[Interest.BEACH], limit=50))
        assert len(found) >= 5
        assert all("beach" in a.tags for a in found)

    def test_avoid_crowds(self, local: LocalProvider) -> None:
        q = AttractionQuery(max_crowd=CrowdLevel.MEDIUM, limit=100)
        found = local.search_attractions(q)
        assert found and all(a.crowd_level is not CrowdLevel.HIGH for a in found)
        everything = local.search_attractions(AttractionQuery(limit=100))
        assert any(a.crowd_level is CrowdLevel.HIGH for a in everything)

    def test_more_matched_interests_rank_first(self, local: LocalProvider) -> None:
        found = local.search_attractions(
            AttractionQuery(interests=[Interest.CULTURE, Interest.FOOD], limit=50)
        )
        first = set(found[0].tags)
        assert {"culture", "food"} <= first

    def test_free_indoor_and_neighborhood_filters(self, local: LocalProvider) -> None:
        free = local.search_attractions(AttractionQuery(free_only=True, limit=100))
        assert free and all(a.is_free for a in free)
        indoor = local.search_attractions(AttractionQuery(indoor_only=True, limit=100))
        assert indoor and all(a.indoor for a in indoor)
        ras = local.search_attractions(AttractionQuery(neighborhood_ids=["al-ras"], limit=100))
        assert ras and {a.neighborhood_id for a in ras} == {"al-ras"}

    def test_exclude_and_limit(self, local: LocalProvider) -> None:
        q = AttractionQuery(interests=[Interest.BEACH], limit=3, exclude_ids=["kite-beach"])
        found = local.search_attractions(q)
        assert len(found) == 3 and "kite-beach" not in {a.id for a in found}

    def test_near_and_radius(self, local: LocalProvider) -> None:
        fahidi = local.get_place("al-fahidi-historical-neighbourhood")
        assert fahidi is not None
        near = local.search_restaurants(
            RestaurantQuery(near=fahidi.location, radius_km=1.0, limit=100)
        )
        assert near
        assert all(fahidi.location.distance_km(r.location) <= 1.0 for r in near)

    def test_restaurant_filters(self, local: LocalProvider) -> None:
        found = local.search_restaurants(
            RestaurantQuery(
                cuisines=["Emirati"],
                price_bands=[PriceBand.CASUAL],
                meal_type=MealType.BREAKFAST,
                limit=50,
            )
        )
        assert found
        for r in found:
            assert "emirati" in r.cuisine
            assert r.price_band is PriceBand.CASUAL
            assert MealType.BREAKFAST in r.meal_types

    def test_get_place(self, local: LocalProvider) -> None:
        assert isinstance(local.get_place("etihad-museum"), Attraction)
        assert isinstance(local.get_place("ravi-satwa"), Restaurant)
        assert local.get_place("no-such-place") is None

    def test_neighborhoods(self, local: LocalProvider) -> None:
        hoods = local.neighborhoods()
        assert all(isinstance(h, Neighborhood) for h in hoods)
        assert "al-fahidi" in {h.id for h in hoods}


class TestRoutes:
    def test_neighborhood_pair_uses_stored_matrix(self, local: LocalProvider) -> None:
        leg = local.travel("al-rigga", "downtown", TransportMode.TAXI)
        reverse = local.travel("downtown", "al-rigga", TransportMode.TAXI)
        assert leg is not None and reverse is not None
        assert leg.duration_min.value == reverse.duration_min.value  # symmetric lookup
        assert leg.duration_min.source is Source.ESTIMATED

    def test_place_to_place(self, local: LocalProvider) -> None:
        leg = local.travel("deira-gold-souk", "deira-spice-souk", TransportMode.WALK)
        assert leg is not None and leg.duration_min.value <= 15
        assert leg.cost_aed.value == 0

    def test_options_that_do_not_exist(self, local: LocalProvider) -> None:
        assert local.travel("kite-beach", "kite-beach", TransportMode.TAXI) is None
        assert local.travel("al-rigga", "hatta", TransportMode.WALK) is None
        assert local.travel("kite-beach", "jumeirah-public-beach", TransportMode.METRO) is None
        with pytest.raises(ProviderError, match="unknown"):
            local.travel("kite-beach", "the-moon", TransportMode.TAXI)

    def test_taxi_cost_is_split_per_person(self, local: LocalProvider) -> None:
        solo = local.travel("al-rigga", "umm-suqeim", TransportMode.TAXI, party_size=1)
        pair = local.travel("al-rigga", "umm-suqeim", TransportMode.TAXI, party_size=2)
        five = local.travel("al-rigga", "umm-suqeim", TransportMode.TAXI, party_size=5)
        assert solo and pair and five
        assert pair.cost_aed.value == pytest.approx(solo.cost_aed.value / 2, abs=0.01)
        assert five.cost_aed.value == pytest.approx(solo.cost_aed.value * 2 / 5, abs=0.01)

    def test_metro_and_abra_fares(self, local: LocalProvider) -> None:
        metro = local.travel("al-rigga", "dubai-marina", TransportMode.METRO)
        assert metro is not None and metro.cost_aed.value == 7.5
        abra = local.travel("al-shindagha", "al-ras", TransportMode.ABRA)
        assert abra is not None and abra.cost_aed.value == 1.0

    def test_matrix(self, local: LocalProvider) -> None:
        m = local.matrix(["al-fahidi", "al-ras", "kite-beach"], [TransportMode.TAXI])
        assert m.lookup("al-ras", "al-fahidi", TransportMode.TAXI) is not None
        assert all(e.mode is TransportMode.TAXI for e in m.entries)
        assert local.matrix([], [TransportMode.TAXI]).entries == []


class TestWeatherHotelsPricing:
    def test_forecast_uses_climate_normals(self, local: LocalProvider) -> None:
        days = local.forecast(date(2026, 12, 30), 4)
        assert [d.date.month for d in days] == [12, 12, 1, 1]
        assert all(d.source is Source.ESTIMATED for d in days)
        assert days[0].temp_max_c > days[3].temp_max_c  # January is cooler

    def test_hotels(self, local: LocalProvider) -> None:
        budget = local.areas(HotelTier.BUDGET)
        assert len(budget) >= 5
        mins = [a.nightly_rate.value.min_aed for a in budget]
        assert mins == sorted(mins)
        assert local.fees(HotelTier.LUXURY).tourism_dirham_per_room_night == 20
        assert local.season(12).multiplier > local.season(7).multiplier
        assert len(local.areas()) > len(budget)

    def test_pricing(self, local: LocalProvider) -> None:
        fare = local.transit_fare(TransportMode.METRO, FareProduct.GOLD, ZoneBand.TWO_ZONES)
        assert fare is not None and fare.fare_aed == 10.0 and fare.source is Source.VERIFIED
        tariff = local.taxi_tariff(TaxiKind.STREET_DAY)
        assert tariff is not None and tariff.fare(1) == tariff.minimum_aed
        assert local.transit_fare(TransportMode.WALK, FareProduct.SILVER, ZoneBand.FLAT) is None

    def test_missing_database(self, tmp_path: Path) -> None:
        with pytest.raises(ProviderError, match="build_db"):
            LocalProvider(tmp_path / "missing.sqlite")


# ------------------------------------------------------------------ fallback chains
class _Broken:
    name = "broken"

    def neighborhoods(self) -> list[Neighborhood]:
        raise ProviderError("down")

    def search_attractions(self, query: AttractionQuery) -> list[Attraction]:
        raise TimeoutError("timed out")

    def search_restaurants(self, query: RestaurantQuery) -> list[Restaurant]:
        return []

    def get_place(self, place_id: str) -> Attraction | Restaurant | None:
        return None


class _BrokenRoutes:
    name = "osrm"

    def travel(
        self, from_id: str, to_id: str, mode: TransportMode, *, party_size: int = 1
    ) -> Leg | None:
        raise ProviderError("429 too many requests")

    def matrix(self, ids: Sequence[str], modes: Sequence[TransportMode]) -> TravelMatrix:
        return TravelMatrix()


class TestChains:
    def test_falls_back_and_emits_trace(self, local: LocalProvider) -> None:
        events: list[TraceEvent] = []
        chain = ChainedPlacesProvider([_Broken(), local], emit=events.append, run_id="r1")
        found = chain.search_attractions(AttractionQuery(interests=[Interest.BEACH]))
        assert found
        assert events and events[0].kind is TraceKind.FALLBACK
        assert events[0].run_id == "r1" and "broken" in events[0].summary

    def test_empty_results_fall_back(self, local: LocalProvider) -> None:
        events: list[TraceEvent] = []
        chain = ChainedPlacesProvider([_Broken(), local], emit=events.append)
        assert chain.search_restaurants(RestaurantQuery(limit=3))
        assert chain.get_place("etihad-museum") is not None
        assert len(events) == 2

    def test_routes_chain(self, local: LocalProvider) -> None:
        chain = ChainedRoutesProvider([_BrokenRoutes(), local])
        assert chain.travel("al-rigga", "downtown", TransportMode.TAXI) is not None
        assert chain.matrix(["al-rigga", "downtown"], [TransportMode.TAXI]).entries

    def test_last_provider_empty_answer_is_returned(self, local: LocalProvider) -> None:
        chain = ChainedRoutesProvider([local])
        assert chain.travel("al-rigga", "hatta", TransportMode.WALK) is None

    def test_all_failing_raises(self) -> None:
        chain = ChainedPlacesProvider([_Broken()])
        with pytest.raises(ProviderError, match="every provider failed"):
            chain.neighborhoods()

    def test_needs_a_provider(self) -> None:
        with pytest.raises(ValueError):
            ChainedPlacesProvider([])


def test_get_providers_builds_missing_database(tmp_path: Path) -> None:
    db = tmp_path / "auto.sqlite"
    providers = get_providers(Settings(db_path=db))
    assert db.exists()
    assert isinstance(providers, DataProviders)
    assert providers.places.get_place("kite-beach") is not None
    providers.close()
