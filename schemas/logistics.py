"""Legs, transport options and the neighborhood travel matrix."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, NonNegativeFloat, NonNegativeInt, model_validator

from schemas.base import StrictModel
from schemas.common import Slug
from schemas.places import HotelArea
from schemas.provenance import SourcedValue


class TransportMode(StrEnum):
    METRO = "metro"
    TRAM = "tram"
    BUS = "bus"
    TAXI = "taxi"
    RIDE_HAIL = "ride_hail"
    WALK = "walk"
    ABRA = "abra"


class Leg(StrictModel):
    """One move between two places (or a place and the hotel area)."""

    from_id: Slug
    to_id: Slug
    mode: TransportMode
    duration_min: SourcedValue[NonNegativeInt]
    distance_km: SourcedValue[NonNegativeFloat]
    cost_aed: SourcedValue[NonNegativeFloat]  # per person

    @model_validator(mode="after")
    def _distinct_ends(self) -> Self:
        if self.from_id == self.to_id:
            raise ValueError("a leg must connect two different places")
        return self


class TransportOption(StrictModel):
    """A transport mode recommended for this trip, with a short explanation."""

    mode: TransportMode
    summary: str = Field(min_length=1)
    operating_hours: str | None = None
    notes: str = ""


class TravelMatrixEntry(StrictModel):
    from_id: Slug
    to_id: Slug
    mode: TransportMode
    duration_min: SourcedValue[NonNegativeInt]
    distance_km: SourcedValue[NonNegativeFloat]


class TravelMatrix(StrictModel):
    """Neighborhood-to-neighborhood travel times per mode (research.md §2.4)."""

    entries: list[TravelMatrixEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_keys(self) -> Self:
        keys = [(e.from_id, e.to_id, e.mode) for e in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate (from_id, to_id, mode) entries in travel matrix")
        return self

    def lookup(
        self, from_id: str, to_id: str, mode: TransportMode, *, symmetric: bool = True
    ) -> TravelMatrixEntry | None:
        """Find an entry; if ``symmetric``, fall back to the reverse direction."""
        reverse: TravelMatrixEntry | None = None
        for e in self.entries:
            if e.mode is not mode:
                continue
            if e.from_id == from_id and e.to_id == to_id:
                return e
            if symmetric and e.from_id == to_id and e.to_id == from_id:
                reverse = e
        return reverse


class LogisticsResult(StrictModel):
    """Output of the Logistics agent's parallel phase (architecture.md §5.1)."""

    hotel_areas: list[HotelArea] = Field(min_length=1)  # ranked, best first
    transport_options: list[TransportOption] = Field(default_factory=list)
    travel_matrix: TravelMatrix
    notes: str = ""

    @property
    def recommended_hotel(self) -> HotelArea:
        return self.hotel_areas[0]
