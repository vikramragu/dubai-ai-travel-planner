"""Small shared types: ids, money, coordinates, price ranges, opening hours and enums."""

from __future__ import annotations

import math
from datetime import date, time
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, NonNegativeFloat, StringConstraints, model_validator

from schemas.base import StrictModel

# Stable ids used across the dataset, tools and LLM output, e.g. "al-fahidi-historical-district".
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=80)]

# Amounts in UAE dirhams.
AED = NonNegativeFloat

# Generous bounding box around the Emirate of Dubai, including Hatta and the desert.
DUBAI_LAT_RANGE = (24.6, 25.5)
DUBAI_LON_RANGE = (54.8, 56.4)

_EARTH_RADIUS_KM = 6371.0


# ---------------------------------------------------------------------------- enums
class Interest(StrEnum):
    CULTURE = "culture"
    FOOD = "food"
    BEACH = "beach"
    NATURE = "nature"
    ADVENTURE = "adventure"
    SHOPPING = "shopping"
    ART = "art"
    NIGHTLIFE = "nightlife"
    FAMILY = "family"
    RELAXATION = "relaxation"


class Avoidance(StrEnum):
    CROWDS = "crowds"
    LONG_WALKS = "long_walks"
    EARLY_MORNINGS = "early_mornings"
    LATE_NIGHTS = "late_nights"


class HotelTier(StrEnum):
    BUDGET = "budget"
    MID = "mid"
    LUXURY = "luxury"


class Pace(StrEnum):
    RELAXED = "relaxed"
    BALANCED = "balanced"
    PACKED = "packed"

    @property
    def activity_hours(self) -> int:
        """Target activity hours per day (architecture.md §5.2)."""
        return {Pace.RELAXED: 6, Pace.BALANCED: 8, Pace.PACKED: 10}[self]


class CrowdLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"

    @classmethod
    def from_date(cls, d: date) -> Weekday:
        return list(cls)[d.weekday()]

    @property
    def previous(self) -> Weekday:
        days = list(Weekday)
        return days[(days.index(self) - 1) % 7]


# ---------------------------------------------------------------------------- geo
class LatLon(StrictModel):
    """A coordinate inside Dubai (the MVP scope)."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def _inside_dubai(self) -> Self:
        lat_ok = DUBAI_LAT_RANGE[0] <= self.lat <= DUBAI_LAT_RANGE[1]
        lon_ok = DUBAI_LON_RANGE[0] <= self.lon <= DUBAI_LON_RANGE[1]
        if not (lat_ok and lon_ok):
            raise ValueError(f"coordinate ({self.lat}, {self.lon}) is outside Dubai")
        return self

    def distance_km(self, other: LatLon) -> float:
        """Great-circle (haversine) distance."""
        p1, p2 = math.radians(self.lat), math.radians(other.lat)
        dp = p2 - p1
        dl = math.radians(other.lon - self.lon)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------- money
class PriceRange(StrictModel):
    """A per-person (or per-night, per-ride) price range in AED. ``0-0`` means free."""

    min_aed: AED = 0.0
    max_aed: AED = 0.0

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min_aed > self.max_aed:
            raise ValueError("min_aed must not exceed max_aed")
        return self

    @classmethod
    def fixed(cls, amount: float) -> PriceRange:
        return cls(min_aed=amount, max_aed=amount)

    @property
    def midpoint(self) -> float:
        return (self.min_aed + self.max_aed) / 2

    @property
    def is_free(self) -> bool:
        return self.max_aed == 0


# ---------------------------------------------------------------------------- hours
def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


class TimeWindow(StrictModel):
    """An opening window. ``closes <= opens`` means it closes after midnight (e.g. 16:00-01:00)."""

    opens: time
    closes: time

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if self.opens == self.closes:
            raise ValueError("opens and closes must differ")
        return self

    @property
    def overnight(self) -> bool:
        return self.closes <= self.opens

    def contains(self, start: time, end: time) -> bool:
        """Whether a same-day slot ``start``-``end`` fits entirely inside this window."""
        o, c = _minutes(self.opens), _minutes(self.closes)
        if self.overnight:
            c += 24 * 60
        s, e = _minutes(start), _minutes(end)
        return o <= s and e <= c

    def contains_after_midnight(self, start: time, end: time) -> bool:
        """Whether a slot fits in the part of an overnight window that runs past midnight.

        For a Friday 18:00-02:00 window this is Saturday 00:00-02:00.
        """
        return self.overnight and _minutes(end) <= _minutes(self.closes)


class OpeningHours(StrictModel):
    """Weekly opening hours. Use ``always_open`` for beaches, districts and walks."""

    always_open: bool = False
    weekly: dict[Weekday, list[TimeWindow]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.always_open and self.weekly:
            raise ValueError("always_open places must not list weekly windows")
        if not self.always_open and not any(self.weekly.values()):
            raise ValueError("give at least one opening window or set always_open")
        return self

    def is_open(self, day: Weekday, start: time, end: time) -> bool:
        """Whether a same-day slot fits in an opening window.

        Includes the previous day's overnight windows: a place open Friday 18:00-02:00 is
        open on Saturday at 00:30.
        """
        if self.always_open:
            return True
        if any(w.contains(start, end) for w in self.weekly.get(day, [])):
            return True
        return any(w.contains_after_midnight(start, end) for w in self.weekly.get(day.previous, []))
