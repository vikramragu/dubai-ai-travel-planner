"""Data providers and the factory that wires them into fallback chains (architecture.md §9.2).

``DATA_MODE`` picks the chains; the local provider is always the last link, so a run never
fails because a live source is down:

============  ===========================  ===============================  ==================
mode          places                       routes                           weather
============  ===========================  ===============================  ==================
``local``     local                        local (stored OSRM routes)       local (climate)
``hybrid``    curated + live OSM extras    local, then live OSRM for live   Open-Meteo, local
              (Overpass), local            places; per-run call budget
``live``      same as hybrid               live OSRM for every road leg,    Open-Meteo, local
                                           local; no call budget
============  ===========================  ===============================  ==================

Each live source can be switched off with ``USE_OVERPASS`` / ``USE_OSRM`` / ``USE_OPEN_METEO``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from core.config import DataMode, Settings, get_settings
from data.providers.base import (
    AttractionQuery,
    HotelProvider,
    PlacesProvider,
    PricingProvider,
    ProviderError,
    RestaurantQuery,
    RoutesProvider,
    WeatherProvider,
)
from data.providers.chained import (
    ChainedPlacesProvider,
    ChainedRoutesProvider,
    ChainedWeatherProvider,
    Emit,
)
from data.providers.http import HttpClient
from data.providers.local import LocalProvider
from data.providers.open_meteo import OpenMeteoProvider
from data.providers.osrm import EstimatedRoutesProvider, OSRMRoutesProvider
from data.providers.overpass import MergedPlaces, OverpassPlacesProvider
from schemas import LatLon

__all__ = [
    "AttractionQuery",
    "DataProviders",
    "HotelProvider",
    "LocalProvider",
    "PlacesProvider",
    "PricingProvider",
    "ProviderError",
    "RestaurantQuery",
    "RoutesProvider",
    "WeatherProvider",
    "build_providers",
    "get_providers",
]


@dataclass(frozen=True)
class DataProviders:
    places: PlacesProvider
    routes: RoutesProvider
    weather: WeatherProvider
    hotels: HotelProvider
    pricing: PricingProvider
    mode: DataMode = DataMode.LOCAL
    http: HttpClient | None = None

    def close(self) -> None:
        """Close the database connection(s) and the HTTP client."""
        for p in {id(x): x for x in (self.hotels, self.pricing)}.values():
            close = getattr(p, "close", None)
            if callable(close):
                close()
        if self.http is not None:
            self.http.close()


def build_providers(
    db_path: Path,
    *,
    settings: Settings | None = None,
    emit: Emit | None = None,
    run_id: str = "offline",
    http: HttpClient | None = None,
) -> DataProviders:
    """Wire the provider chains for one database and data mode (``settings.data_mode``)."""
    local = LocalProvider(db_path)
    mode = settings.data_mode if settings is not None else DataMode.LOCAL
    if settings is None or mode is DataMode.LOCAL:
        return DataProviders(
            places=ChainedPlacesProvider([local], emit=emit, run_id=run_id),
            routes=ChainedRoutesProvider([local], emit=emit, run_id=run_id),
            weather=ChainedWeatherProvider([local], emit=emit, run_id=run_id),
            hotels=local,
            pricing=local,
        )

    client = http or HttpClient(settings)
    places_chain: list[PlacesProvider] = [local]
    if settings.use_overpass:
        live = OverpassPlacesProvider(
            client,
            settings,
            curated=local.search_attractions(AttractionQuery(limit=1000)),
            neighborhoods=local.neighborhoods(),
        )
        places_chain = [MergedPlaces(live, local), local]
    places = ChainedPlacesProvider(places_chain, emit=emit, run_id=run_id)

    hoods = {h.id: h for h in local.neighborhoods()}

    def locate(pid: str) -> tuple[LatLon, str | None] | None:
        if pid in hoods:
            return hoods[pid].location, pid
        place = places.get_place(pid)
        return None if place is None else (place.location, place.neighborhood_id)

    # The last link estimates any leg the others can't (live places when OSRM is down).
    estimate = EstimatedRoutesProvider(
        locate=locate, station=lambda h: hoods[h].metro_station if h in hoods else None,
        leg_cost=local.leg_cost,
    )  # fmt: skip
    routes_chain: list[RoutesProvider] = [local, estimate]
    if settings.use_osrm:
        osrm = OSRMRoutesProvider(client, settings, locate=locate, leg_cost=local.leg_cost)
        # hybrid: stored routes first, live OSRM for places the stored data doesn't know;
        # live: OSRM for every road leg.
        routes_chain = (
            [local, osrm, estimate] if mode is DataMode.HYBRID else [osrm, local, estimate]
        )
    weather_chain: list[WeatherProvider] = [local]
    if settings.use_open_meteo:
        weather_chain = [OpenMeteoProvider(client, settings, climate=local), local]
    return DataProviders(
        places=places,
        routes=ChainedRoutesProvider(routes_chain, emit=emit, run_id=run_id),
        weather=ChainedWeatherProvider(weather_chain, emit=emit, run_id=run_id),
        hotels=local,
        pricing=local,
        mode=mode,
        http=client,
    )


_CACHE: dict[tuple[str, str], DataProviders] = {}
_LOCK = threading.Lock()


def get_providers(settings: Settings | None = None) -> DataProviders:
    """Shared providers for the configured database and data mode; builds the database from
    the seed if it is missing."""
    settings = settings or get_settings()
    if not settings.db_path.exists():
        from data.build_db import build  # local import: building is rare

        build(settings.db_path)
    relevant = settings.model_dump_json(
        include={
            "data_mode", "use_overpass", "overpass_url", "use_osrm", "osrm_car_url",
            "osrm_foot_url", "use_open_meteo", "open_meteo_url", "http_timeout_s",
            "http_user_agent", "min_request_interval_s", "cache_dir", "cache_ttl_places_h",
            "cache_ttl_routes_h", "cache_ttl_weather_h",
        }
    )  # fmt: skip
    key = (str(settings.db_path.resolve()), relevant)
    with _LOCK:
        if key not in _CACHE:
            _CACHE[key] = build_providers(settings.db_path, settings=settings)
        return _CACHE[key]
