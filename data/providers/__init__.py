"""Data providers and the factory that wires them into fallback chains.

``get_providers()`` returns the providers the tools use. In Phase 2 every chain contains only
the local SQLite provider (``DATA_MODE`` has no effect yet); Phase 7 adds live providers in
front of it for ``hybrid`` / ``live`` modes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.config import Settings, get_settings
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
from data.providers.local import LocalProvider

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

    def close(self) -> None:
        """Close the underlying database connection(s)."""
        for p in {id(x): x for x in (self.hotels, self.pricing)}.values():
            close = getattr(p, "close", None)
            if callable(close):
                close()


def build_providers(
    db_path: Path, *, emit: Emit | None = None, run_id: str = "offline"
) -> DataProviders:
    """Wire the provider chains for one database (live providers join in Phase 7)."""
    local = LocalProvider(db_path)
    return DataProviders(
        places=ChainedPlacesProvider([local], emit=emit, run_id=run_id),
        routes=ChainedRoutesProvider([local], emit=emit, run_id=run_id),
        weather=ChainedWeatherProvider([local], emit=emit, run_id=run_id),
        hotels=local,
        pricing=local,
    )


@lru_cache(maxsize=1)
def _default(db_path: Path) -> DataProviders:
    return build_providers(db_path)


def get_providers(settings: Settings | None = None) -> DataProviders:
    """Shared providers for the configured database; builds it from the seed if missing."""
    settings = settings or get_settings()
    if not settings.db_path.exists():
        from data.build_db import build  # local import: building is rare

        build(settings.db_path)
    return _default(settings.db_path)
