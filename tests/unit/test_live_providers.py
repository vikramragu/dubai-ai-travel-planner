"""Phase 7: live providers (OSRM, Open-Meteo, Overpass), the HTTP layer and the fallback chains.

Every test runs offline: HTTP is answered by ``respx`` from recorded responses in
``tests/fixtures/http`` (real responses, trimmed), or made to fail on purpose (timeouts, 429,
502, connection errors) to check the fallbacks.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.config import DataMode, Settings
from core.tracing import TraceKind
from data import heuristics
from data.providers import build_providers
from data.providers.base import AttractionQuery, NotApplicable, ProviderError, RestaurantQuery
from data.providers.chained import ChainedRoutesProvider
from data.providers.http import HttpClient
from data.providers.open_meteo import OpenMeteoProvider
from data.providers.osrm import OSRMRoutesProvider
from data.providers.overpass import OverpassPlacesProvider
from data.providers.runtime import data_run, node_events
from schemas import Interest, LatLon, Source, TransportMode

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "http"
FAHIDI = LatLon(lat=25.2637, lon=55.2972)
BEACH = LatLon(lat=25.1580, lon=55.2000)
SHINDAGHA = LatLon(lat=25.2660, lon=55.2895)
RAS = LatLon(lat=25.2705, lon=55.2985)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_mode=DataMode.HYBRID,
        cache_dir=tmp_path / "cache",
        min_request_interval_s=0,
        external_call_budget=40,
    )


@pytest.fixture
def http(settings: Settings) -> Iterator[HttpClient]:
    client = HttpClient(settings)
    yield client
    client.close()


def _fixed_cost(mode: TransportMode, km: float, party: int) -> Any:
    from schemas import estimated

    return estimated(round(km, 2), "test")


# ------------------------------------------------------------------ HTTP layer
class TestHttpClient:
    def test_caches_and_sends_a_generic_user_agent(self, http: HttpClient) -> None:
        with respx.mock() as router:
            route = router.get("https://example.org/x").mock(
                return_value=httpx.Response(200, json={"ok": 1})
            )
            assert http.get_json("test", "https://example.org/x", ttl_h=1) == {"ok": 1}
            assert http.get_json("test", "https://example.org/x", ttl_h=1) == {"ok": 1}
        assert route.call_count == 1  # the second answer came from the cache
        ua = route.calls[0].request.headers["user-agent"]
        assert ua.startswith("dubai-travel-planner") and "@" not in ua

    @pytest.mark.parametrize(
        "failure",
        [
            httpx.Response(429, text="slow down"),
            httpx.Response(502, text="bad gateway"),
            httpx.ReadTimeout("timed out"),
            httpx.ConnectError("no network"),
            httpx.Response(200, text="<html>not json</html>"),
        ],
    )
    def test_failures_become_provider_errors(self, http: HttpClient, failure: Any) -> None:
        with respx.mock() as router:
            route = router.get("https://example.org/x")
            route.mock(side_effect=failure) if isinstance(failure, Exception) else route.mock(
                return_value=failure
            )
            with pytest.raises(ProviderError):
                http.get_json("test", "https://example.org/x", ttl_h=1)

    def test_budget_and_trace_events(self, http: HttpClient) -> None:
        events: list[tuple[TraceKind, str]] = []
        with (
            respx.mock() as router,
            data_run(budget=2),
            node_events(lambda k, s, p: events.append((k, s))),
        ):
            router.get(url__regex=r"https://example.org/\d").mock(
                return_value=httpx.Response(200, json={})
            )
            http.get_json("test", "https://example.org/1", ttl_h=1)
            http.get_json("test", "https://example.org/2", ttl_h=1)
            with pytest.raises(ProviderError, match="budget"):
                http.get_json("test", "https://example.org/3", ttl_h=1)
            http.get_json("test", "https://example.org/1", ttl_h=1)  # cached: still free
        kinds = [k for k, _ in events]
        assert kinds == [TraceKind.TOOL_CALL, TraceKind.TOOL_CALL, TraceKind.FALLBACK]
        assert "HTTP 200" in events[0][1]


# ------------------------------------------------------------------ OSRM
class TestOSRM:
    @staticmethod
    def provider(http: HttpClient, settings: Settings) -> OSRMRoutesProvider:
        where = {
            "a": (FAHIDI, "al-fahidi"),
            "b": (BEACH, "umm-suqeim"),
            "c": (RAS, "al-ras"),
            "d": (SHINDAGHA, "al-shindagha"),
        }
        return OSRMRoutesProvider(http, settings, locate=where.get, leg_cost=_fixed_cost)

    def test_taxi_leg_is_live(self, http: HttpClient, settings: Settings) -> None:
        body = fixture("osrm_route_car")
        with respx.mock() as router:
            router.get(url__startswith=settings.osrm_car_url).mock(
                return_value=httpx.Response(200, json=body)
            )
            leg = self.provider(http, settings).travel("a", "b", TransportMode.TAXI)
        assert leg is not None and leg.duration_min.source is Source.API
        route = body["routes"][0]
        expected = heuristics.road_leg(
            TransportMode.TAXI,
            route["duration"] / 60,
            route["distance"] / 1000,
            FAHIDI.distance_km(BEACH),
        )
        assert expected is not None
        assert (leg.duration_min.value, leg.distance_km.value) == expected
        assert "OSRM" in (leg.duration_min.reference or "")

    def test_walking_rules_still_apply(self, http: HttpClient, settings: Settings) -> None:
        with respx.mock() as router:
            router.get(url__startswith=settings.osrm_foot_url).mock(
                return_value=httpx.Response(200, json=fixture("osrm_route_foot"))
            )
            p = self.provider(http, settings)
            assert p.travel("a", "d", TransportMode.WALK) is not None  # same side, short
            assert p.travel("a", "c", TransportMode.WALK) is None  # across the creek

    def test_other_modes_are_not_osrms_job(self, http: HttpClient, settings: Settings) -> None:
        with pytest.raises(NotApplicable):
            self.provider(http, settings).travel("a", "b", TransportMode.METRO)

    def test_matrix_from_one_table_call(self, http: HttpClient, settings: Settings) -> None:
        with respx.mock() as router:
            route = router.get(url__startswith=settings.osrm_car_url).mock(
                return_value=httpx.Response(200, json=fixture("osrm_table_car"))
            )
            m = self.provider(http, settings).matrix(["a", "b", "c"], [TransportMode.TAXI])
        assert route.call_count == 1 and len(m.entries) == 3
        assert all(e.duration_min.source is Source.API for e in m.entries)


# ------------------------------------------------------------------ Open-Meteo
class TestOpenMeteo:
    TODAY = date(2026, 11, 18)

    @staticmethod
    def climate() -> Any:
        from schemas import DayWeather

        class Climate:
            def forecast(self, start: date, days: int) -> list[DayWeather]:
                return [
                    DayWeather(
                        date=start + timedelta(days=i),
                        temp_max_c=27,
                        temp_min_c=19,
                        precipitation_mm=0.3,
                        source=Source.ESTIMATED,
                        reference="climate normal",
                    )
                    for i in range(days)
                ]

        return Climate()

    def test_forecast_then_climate_beyond_the_range(
        self, http: HttpClient, settings: Settings
    ) -> None:
        p = OpenMeteoProvider(http, settings, climate=self.climate(), today=self.TODAY)
        start = date(2026, 11, 20)  # the fixture covers 20-23 Nov; ask for 20 days
        with respx.mock() as router:
            router.get(url__startswith=settings.open_meteo_url).mock(
                return_value=httpx.Response(200, json=fixture("open_meteo"))
            )
            days = p.forecast(start, 20)
        assert [d.source for d in days[:4]] == [Source.API] * 4
        assert days[0].temp_max_c == fixture("open_meteo")["daily"]["temperature_2m_max"][0]
        assert all(d.source is Source.ESTIMATED for d in days[16:])  # past the 16-day horizon

    def test_trips_outside_the_range_use_climate_quietly(
        self, http: HttpClient, settings: Settings
    ) -> None:
        p = OpenMeteoProvider(http, settings, climate=self.climate(), today=self.TODAY)
        with pytest.raises(NotApplicable):
            p.forecast(date(2027, 3, 1), 4)


# ------------------------------------------------------------------ Overpass
class TestOverpass:
    @staticmethod
    def provider(http: HttpClient, settings: Settings, providers: Any) -> OverpassPlacesProvider:
        return OverpassPlacesProvider(
            http,
            settings,
            curated=providers.places.search_attractions(AttractionQuery(limit=1000)),
            neighborhoods=providers.places.neighborhoods(),
            background=False,
        )

    def test_live_extras_skip_curated_duplicates(
        self, http: HttpClient, settings: Settings, providers: Any
    ) -> None:
        p = self.provider(http, settings, providers)
        with respx.mock() as router:
            route = router.post(settings.overpass_url).mock(
                return_value=httpx.Response(200, json=fixture("overpass"))
            )
            found = p.search_attractions(
                AttractionQuery(interests=[Interest.CULTURE, Interest.NATURE], limit=20)
            )
            p.search_attractions(AttractionQuery(interests=[Interest.ART], limit=20))
        assert route.call_count == 1  # one city-wide download, cached; searches filter it
        names = {a.name for a in found}
        assert "Dubai Moving Image Museum" in names  # English name preferred over Arabic
        assert "Al Mamzar Park" not in names  # the curated Al Mamzar Beach Park covers it
        assert all(a.source is Source.API and a.id.startswith("osm-") for a in found)
        assert all(a.price.source is Source.ESTIMATED for a in found)  # OSM has no prices
        assert p.get_place(found[0].id) == found[0]  # live ids resolve afterwards

    def test_leaves_curated_work_to_the_local_provider(
        self, http: HttpClient, settings: Settings, providers: Any
    ) -> None:
        p = self.provider(http, settings, providers)
        with pytest.raises(NotApplicable):
            p.search_restaurants(RestaurantQuery())
        with pytest.raises(NotApplicable):
            p.search_attractions(AttractionQuery(near=FAHIDI, radius_km=1))
        with pytest.raises(NotApplicable):
            p.get_place("kite-beach")

    def test_background_download_never_blocks_a_run(
        self, http: HttpClient, settings: Settings, providers: Any
    ) -> None:
        p = self.provider(http, settings, providers)
        p.background = True
        query = AttractionQuery(interests=[Interest.CULTURE], limit=10)
        with respx.mock() as router:
            router.post(settings.overpass_url).mock(
                return_value=httpx.Response(200, json=fixture("overpass"))
            )
            with pytest.raises(ProviderError, match="still downloading"):
                p.search_attractions(query)
            p.wait_for_download(10)
            assert p.search_attractions(query)  # now from the cache

    def test_a_failed_download_is_not_retried_at_once(
        self, http: HttpClient, settings: Settings, providers: Any
    ) -> None:
        p = self.provider(http, settings, providers)
        p.background = True
        query = AttractionQuery(interests=[Interest.CULTURE], limit=10)
        with respx.mock() as router:
            route = router.post(settings.overpass_url).mock(return_value=httpx.Response(504))
            with pytest.raises(ProviderError):
                p.search_attractions(query)
            p.wait_for_download(10)
            with pytest.raises(ProviderError, match="failed recently"):
                p.search_attractions(query)
        assert route.call_count == 1


# ------------------------------------------------------------------ chains and modes
def test_unhandled_travel_means_not_available() -> None:
    class Nobody:
        name = "nobody"

        def travel(self, *a: Any, **k: Any) -> Any:
            raise NotApplicable("no")

        def matrix(self, *a: Any) -> Any:
            raise NotApplicable("no")

    assert ChainedRoutesProvider([Nobody()]).travel("x", "y", TransportMode.METRO) is None


def test_stored_osrm_routes_back_local_legs(providers: Any) -> None:
    leg = providers.routes.travel(
        "al-fahidi-historical-neighbourhood", "kite-beach", TransportMode.TAXI
    )
    assert leg is not None and leg.duration_min.source is Source.ESTIMATED
    assert (leg.duration_min.reference or "").startswith("OSRM route")
    assert providers.routes.travel("al-fahidi", "al-ras", TransportMode.WALK) is None  # creek


def test_traffic_factor_fades_on_long_highway_trips() -> None:
    city = heuristics.road_leg(TransportMode.TAXI, 10, 5, 4)
    highway = heuristics.road_leg(TransportMode.TAXI, 40, 50, 40)
    assert city == (heuristics.TAXI_PICKUP_MIN + 14, 5)  # 10 min x 1.35
    assert highway is not None and highway[0] < heuristics.TAXI_PICKUP_MIN + 40 * 1.35


def test_local_mode_makes_no_network_calls(seed_db: Path, tmp_path: Path) -> None:
    s = Settings(_env_file=None, data_mode=DataMode.LOCAL, cache_dir=tmp_path)
    p = build_providers(seed_db, settings=s)
    with respx.mock(assert_all_called=False) as router:
        router.route().mock(side_effect=AssertionError("network call in local mode"))
        assert p.places.search_attractions(AttractionQuery(interests=[Interest.CULTURE]))
        assert p.routes.travel("al-fahidi", "kite-beach", TransportMode.TAXI) is not None
        assert p.weather.forecast(date.today(), 3)
    assert p.http is None
    p.close()
