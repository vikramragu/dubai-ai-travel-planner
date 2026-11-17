"""Trip calendar: the weekday and month of each trip day.

Opening hours depend on the weekday (Friday hours often differ) and heat, seasonal
attractions and hotel prices depend on the month. When the request has no ``start_date``
we assume a trip starting on the first Monday of ``month`` (default: December, the demo
season), and say so in the itinerary assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from schemas import TripConstraints, Weekday

DEFAULT_MONTH = 12  # cool season; seasonal attractions are open (research.md §5)
REFERENCE_YEAR = 2026
SEASONAL_MONTHS = frozenset({10, 11, 12, 1, 2, 3, 4, 5})  # Global Village, Miracle Garden


@dataclass(frozen=True)
class TripDay:
    day: int  # 1-based
    date: date  # real date, or the assumed reference date
    weekday: Weekday
    month: int
    assumed: bool  # True when no start_date was given


def reference_start(month: int) -> date:
    d = date(REFERENCE_YEAR, month, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)  # first Monday


def trip_calendar(constraints: TripConstraints, month: int | None = None) -> list[TripDay]:
    start = constraints.start_date
    assumed = start is None
    if start is None:
        start = reference_start(month or DEFAULT_MONTH)
    out = []
    for i in range(constraints.days):
        d = start + timedelta(days=i)
        out.append(TripDay(i + 1, d, Weekday.from_date(d), d.month, assumed))
    return out


def trip_month(constraints: TripConstraints, month: int | None = None) -> int:
    """Month used for hotel prices: the month of the first night."""
    return trip_calendar(constraints, month)[0].month
