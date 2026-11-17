"""Climate normals and per-day weather (research.md §2.8)."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import Field, NonNegativeFloat

from schemas.provenance import Provenance


class HeatLevel(StrEnum):
    COMFORTABLE = "comfortable"  # max < 30 °C
    WARM = "warm"  # 30-35 °C
    HOT = "hot"  # 35-40 °C: outdoor slots only early morning or after sunset
    EXTREME = "extreme"  # >= 40 °C: avoid outdoor slots in daylight

    @classmethod
    def from_max_temp(cls, temp_max_c: float) -> HeatLevel:
        if temp_max_c < 30:
            return cls.COMFORTABLE
        if temp_max_c < 35:
            return cls.WARM
        if temp_max_c < 40:
            return cls.HOT
        return cls.EXTREME


class ClimateMonth(Provenance):
    """Long-term monthly averages for Dubai."""

    month: int = Field(ge=1, le=12)
    temp_max_c: float
    temp_min_c: float
    precipitation_mm: NonNegativeFloat  # average monthly total
    rainy_days: NonNegativeFloat  # average days with >= 1 mm
    humidity_pct: float = Field(ge=0, le=100)

    @property
    def heat(self) -> HeatLevel:
        return HeatLevel.from_max_temp(self.temp_max_c)


class DayWeather(Provenance):
    """Weather for one trip day: a forecast (``api``) or a climate normal (``estimated``)."""

    date: dt.date
    temp_max_c: float
    temp_min_c: float
    precipitation_mm: NonNegativeFloat
    rain_probability_pct: float | None = Field(default=None, ge=0, le=100)

    @property
    def heat(self) -> HeatLevel:
        return HeatLevel.from_max_temp(self.temp_max_c)
