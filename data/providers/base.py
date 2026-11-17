"""Provider interfaces (architecture.md §9.1).

Each data need has a small protocol. Phase 2 implements all of them on top of the curated
SQLite database (``local.py``); Phase 7 adds live providers (Overpass, OSRM, Open-Meteo) that
sit in front of the local one in a chain (``chained.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from pydantic import Field, PositiveInt

from schemas import (
    Attraction,
    CrowdLevel,
    DayWeather,
    HotelArea,
    HotelFees,
    HotelSeason,
    HotelTier,
    Interest,
    LatLon,
    Leg,
    MealType,
    Neighborhood,
    PriceBand,
    Restaurant,
    TaxiKind,
    TaxiTariff,
    TransitFare,
    TransportMode,
    TravelMatrix,
)
from schemas.base import StrictModel
from schemas.pricing import FareProduct, ZoneBand


class ProviderError(RuntimeError):
    """A provider could not answer (network error, quota, bad response)."""


class AttractionQuery(StrictModel):
    """Filters for attraction search. Empty filters match everything."""

    interests: list[Interest] = Field(default_factory=list)  # match any
    tags: list[str] = Field(default_factory=list)  # match any (in addition to interests)
    neighborhood_ids: list[str] = Field(default_factory=list)
    near: LatLon | None = None
    radius_km: float | None = Field(default=None, gt=0)
    max_crowd: CrowdLevel | None = None
    free_only: bool = False
    indoor_only: bool = False
    exclude_ids: list[str] = Field(default_factory=list)
    limit: PositiveInt = 20


class RestaurantQuery(StrictModel):
    cuisines: list[str] = Field(default_factory=list)  # match any
    price_bands: list[PriceBand] = Field(default_factory=list)
    meal_type: MealType | None = None
    neighborhood_ids: list[str] = Field(default_factory=list)
    near: LatLon | None = None
    radius_km: float | None = Field(default=None, gt=0)
    max_crowd: CrowdLevel | None = None
    exclude_ids: list[str] = Field(default_factory=list)
    limit: PositiveInt = 20


@runtime_checkable
class PlacesProvider(Protocol):
    name: str

    def neighborhoods(self) -> list[Neighborhood]: ...
    def search_attractions(self, query: AttractionQuery) -> list[Attraction]: ...
    def search_restaurants(self, query: RestaurantQuery) -> list[Restaurant]: ...
    def get_place(self, place_id: str) -> Attraction | Restaurant | None: ...


@runtime_checkable
class RoutesProvider(Protocol):
    name: str

    def travel(
        self, from_id: str, to_id: str, mode: TransportMode, *, party_size: int = 1
    ) -> Leg | None:
        """One leg between two places/neighborhoods; ``None`` if the mode is not an option."""
        ...

    def matrix(
        self, neighborhood_ids: Sequence[str], modes: Sequence[TransportMode]
    ) -> TravelMatrix: ...


@runtime_checkable
class WeatherProvider(Protocol):
    name: str

    def forecast(self, start: date, days: int) -> list[DayWeather]: ...


@runtime_checkable
class HotelProvider(Protocol):
    name: str

    def areas(self, tier: HotelTier | None = None) -> list[HotelArea]: ...
    def fees(self, tier: HotelTier) -> HotelFees: ...
    def season(self, month: int) -> HotelSeason: ...


@runtime_checkable
class PricingProvider(Protocol):
    name: str

    def transit_fare(
        self, mode: TransportMode, product: FareProduct, zone_band: ZoneBand
    ) -> TransitFare | None: ...
    def taxi_tariff(self, kind: TaxiKind) -> TaxiTariff | None: ...
