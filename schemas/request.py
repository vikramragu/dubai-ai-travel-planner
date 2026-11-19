"""The traveller's request after parsing (architecture.md §4.2)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Self

from pydantic import Field, PositiveFloat, field_validator, model_validator

from schemas.base import StrictModel
from schemas.common import Avoidance, HotelTier, Interest, Pace, Slug

MIN_DAYS = 1
MAX_DAYS = 7
MAX_MUST_VISIT = 10


class TripConstraints(StrictModel):
    days: int = Field(ge=MIN_DAYS, le=MAX_DAYS)
    budget_aed: PositiveFloat
    budget_excludes: list[str] = Field(default_factory=lambda: ["flights"])
    travelers: int = Field(default=1, ge=1, le=10)  # everyone, children included
    children: int = Field(default=0, ge=0, le=9)  # fewer than travelers: at least one adult
    rooms: int = Field(default=1, ge=1, le=10)
    nights: int | None = Field(default=None, ge=0, le=MAX_DAYS)
    start_date: date | None = None
    interests: list[Interest] = Field(min_length=1)
    hotel_tier: HotelTier = HotelTier.MID
    avoid: list[Avoidance] = Field(default_factory=list)
    pace: Pace = Pace.BALANCED
    dietary: list[str] = Field(default_factory=list)
    must_visit: list[Slug] = Field(default_factory=list, max_length=MAX_MUST_VISIT)  # place ids

    @field_validator("interests", "avoid", "budget_excludes", "dietary", "must_visit")
    @classmethod
    def _dedupe(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.rooms > self.travelers:
            raise ValueError("rooms cannot exceed travelers")
        if self.children >= self.travelers:
            raise ValueError("at least one traveler must be an adult (children < travelers)")
        if self.nights is not None and self.nights > self.days:
            raise ValueError("nights cannot exceed days")
        return self

    @property
    def night_count(self) -> int:
        """Hotel nights: explicit ``nights`` or ``days - 1`` (architecture.md §5.3)."""
        return self.nights if self.nights is not None else self.days - 1

    @property
    def end_date(self) -> date | None:
        return self.start_date + timedelta(days=self.days - 1) if self.start_date else None

    @property
    def avoids_crowds(self) -> bool:
        return Avoidance.CROWDS in self.avoid

    @property
    def adults(self) -> int:
        return self.travelers - self.children


class TripRequest(StrictModel):
    raw_text: str = Field(min_length=1)
    constraints: TripConstraints
    assumptions: list[str] = Field(default_factory=list)  # what the parser had to assume
