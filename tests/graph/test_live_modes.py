"""Phase 7 exit criteria on the whole graph, offline (HTTP mocked with respx):

- with the network down, a hybrid-mode run still completes, and the fallbacks are in the trace;
- with live sources answering, live values carry the ``api`` tag the UI shows as a badge.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.components.itinerary import BADGES, slot_rows
from core.config import DataMode, Settings
from core.runner import run
from core.tracing import TraceKind
from data.providers import DataProviders, build_providers
from data.providers.overpass import MergedPlaces
from schemas import Source

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "http"
START = date.today() + timedelta(days=3)  # inside Open-Meteo's forecast range
REQUEST = (
    f"3 days in Dubai starting {START.isoformat()}, budget AED 5000, culture, nature and "
    "beaches, mid-range hotel"
)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_mode=DataMode.HYBRID,
        cache_dir=tmp_path / "cache",
        min_request_interval_s=0,
    )


def _providers(seed_db: Path, settings: Settings) -> DataProviders:
    p = build_providers(seed_db, settings=settings)
    merged = p.places.providers[0]  # type: ignore[attr-defined]
    assert isinstance(merged, MergedPlaces)
    merged.live.background = False  # tests wait for the download instead of racing it
    return p


def _weather(request: httpx.Request) -> httpx.Response:
    """An Open-Meteo answer for exactly the dates asked."""
    q = request.url.params
    first, last = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return httpx.Response(
        200,
        json={
            "daily": {
                "time": [d.isoformat() for d in days],
                "temperature_2m_max": [31.5] * len(days),
                "temperature_2m_min": [22.0] * len(days),
                "precipitation_sum": [0.0] * len(days),
                "precipitation_probability_max": [5] * len(days),
            }
        },
    )


def test_hybrid_run_completes_with_the_network_down(seed_db: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    providers = _providers(seed_db, settings)
    with respx.mock() as router:
        router.route().mock(side_effect=httpx.ConnectError("network is down"))
        result = run(REQUEST, providers=providers, llm=None, settings=settings)
    it = result.itinerary
    assert it.review is not None and it.review.passed and it.budget is not None
    fallbacks = [e for e in result.trace if e.agent == "data" and e.kind is TraceKind.FALLBACK]
    assert fallbacks, "the fallbacks must be visible in the trace"
    assert any("overpass" in e.summary for e in fallbacks)
    assert any("open-meteo" in e.summary for e in fallbacks)
    assert all((e.payload or {}).get("node") for e in fallbacks)  # attributed to a graph node
    assert not any(pid.startswith("osm-") for pid in it.place_ids)  # all curated
    providers.close()


def test_live_values_are_tagged_api(seed_db: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    providers = _providers(seed_db, settings)
    with respx.mock() as router:
        router.post(settings.overpass_url).mock(
            return_value=httpx.Response(200, json=fixture("overpass"))
        )
        router.get(url__startswith=settings.osrm_car_url).mock(
            return_value=httpx.Response(200, json=fixture("osrm_route_car"))
        )
        router.get(url__startswith=settings.osrm_foot_url).mock(
            return_value=httpx.Response(200, json=fixture("osrm_route_foot"))
        )
        router.get(url__startswith=settings.open_meteo_url).mock(side_effect=_weather)
        result = run(REQUEST, providers=providers, llm=None, settings=settings)
        weather = providers.weather.forecast(START, 3)
        it = result.itinerary
        live_places = [pid for pid in it.place_ids if pid.startswith("osm-")]
        rows = [r for d in it.days for r in slot_rows(d, it, providers)]
    assert it.review is not None and it.review.passed
    assert all(w.source is Source.API for w in weather)
    assert live_places, "live OpenStreetMap places should reach the plan"
    legs = [s.leg for d in it.days for s in d.slots if s.leg]
    live_legs = [leg for leg in legs if leg.duration_min.source is Source.API]
    assert live_legs and all(
        leg.from_id.startswith("osm-") or leg.to_id.startswith("osm-") for leg in live_legs
    )  # hybrid: only legs touching live places are routed live
    assert BADGES[Source.API] in {r["source"] for r in rows}  # the UI shows the api badge
    calls = [e for e in result.trace if e.agent == "data" and e.kind is TraceKind.TOOL_CALL]
    assert 0 < len(calls) <= settings.external_call_budget
    providers.close()


@pytest.mark.parametrize("mode", [DataMode.LIVE])
def test_live_mode_routes_every_road_leg(seed_db: Path, tmp_path: Path, mode: DataMode) -> None:
    settings = _settings(tmp_path).model_copy(update={"data_mode": mode})
    providers = _providers(seed_db, settings)
    with respx.mock() as router:
        router.post(settings.overpass_url).mock(return_value=httpx.Response(503))
        router.get(url__startswith=settings.osrm_car_url).mock(
            return_value=httpx.Response(200, json=fixture("osrm_route_car"))
        )
        router.get(url__startswith=settings.osrm_foot_url).mock(
            return_value=httpx.Response(200, json=fixture("osrm_route_foot"))
        )
        router.get(url__startswith=settings.open_meteo_url).mock(side_effect=_weather)
        result = run(REQUEST, providers=providers, llm=None, settings=settings)
    legs = [s.leg for d in result.itinerary.days for s in d.slots if s.leg]
    taxi = [leg for leg in legs if leg.mode.value == "taxi"]
    assert taxi and all(leg.duration_min.source is Source.API for leg in taxi)
    providers.close()
