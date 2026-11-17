"""Neighborhoods, attractions, restaurants and hotel areas (architecture.md §4.3)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator

from schemas.base import StrictModel
from schemas.common import CrowdLevel, HotelTier, LatLon, OpeningHours, PriceRange, Slug
from schemas.provenance import Source, SourcedValue

__all__ = [
    "Attraction",
    "HotelArea",
    "MealType",
    "Neighborhood",
    "Place",
    "PriceBand",
    "PriceRange",
    "Restaurant",
]


class MealType(StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class PriceBand(StrEnum):
    STREET = "street"  # cafeterias, shawarma, karak
    CASUAL = "casual"  # Emirati/Levantine restaurants, food halls
    MID = "mid"  # sit-down restaurants in malls and hotels
    FINE = "fine"


class Neighborhood(StrictModel):
    id: Slug
    name: str = Field(min_length=1)
    location: LatLon
    vibe: str = ""
    metro_station: str | None = None
    beach_access: bool = False


class Place(StrictModel):
    """Common fields for anything the itinerary can visit."""

    id: Slug
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    neighborhood_id: Slug
    location: LatLon
    tags: list[str] = Field(default_factory=list)  # "cultural", "outdoor", "family", ...
    crowd_level: CrowdLevel
    opening_hours: OpeningHours | None = None  # None = unknown
    typical_duration_min: int = Field(ge=10, le=720)
    price: SourcedValue[PriceRange]  # per person
    source: Source  # where the record as a whole came from
    reference: str | None = None

    @field_validator("tags")
    @classmethod
    def _normalise_tags(cls, tags: list[str]) -> list[str]:
        return list(dict.fromkeys(t.strip().lower() for t in tags if t.strip()))


class Attraction(Place):
    indoor: bool = False  # used for heat-aware scheduling

    @property
    def is_free(self) -> bool:
        return self.price.value.is_free


class Restaurant(Place):
    typical_duration_min: int = Field(default=60, ge=15, le=240)
    cuisine: list[str] = Field(min_length=1)
    meal_types: list[MealType] = Field(min_length=1)
    price_band: PriceBand


class HotelArea(StrictModel):
    neighborhood_id: Slug
    tier: HotelTier
    nightly_rate: SourcedValue[PriceRange]  # per room per night, before fees
    metro_access: bool
    beach_access: bool
    rationale: str = ""
