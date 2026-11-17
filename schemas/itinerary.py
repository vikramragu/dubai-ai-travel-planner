"""Research output, day plans and the final itinerary (architecture.md §4.4)."""

from __future__ import annotations

import datetime as dt
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Self

from pydantic import Field, NonNegativeInt, PositiveInt, model_validator

from schemas.base import StrictModel
from schemas.budget import BudgetAlternative, BudgetBreakdown
from schemas.common import AED, Slug
from schemas.logistics import Leg
from schemas.places import Attraction, HotelArea, Restaurant
from schemas.request import TripRequest
from schemas.review import ReviewReport


class ResearchResult(StrictModel):
    """Output of the Destination Research agent."""

    attractions: list[Attraction] = Field(default_factory=list)
    restaurants: list[Restaurant] = Field(default_factory=list)
    neighborhood_ids: list[Slug] = Field(default_factory=list)
    rationale: dict[str, str] = Field(default_factory=dict)  # place id -> why it was picked

    @model_validator(mode="after")
    def _unique_and_known(self) -> Self:
        ids = [p.id for p in self.attractions] + [p.id for p in self.restaurants]
        if len(ids) != len(set(ids)):
            raise ValueError("place ids must be unique across attractions and restaurants")
        unknown = set(self.rationale) - set(ids)
        if unknown:
            raise ValueError(f"rationale references unknown place ids: {sorted(unknown)}")
        return self

    @property
    def place_ids(self) -> set[str]:
        return {p.id for p in self.attractions} | {p.id for p in self.restaurants}


class SlotKind(StrEnum):
    ACTIVITY = "activity"
    MEAL = "meal"
    TRANSIT = "transit"
    REST = "rest"


class Slot(StrictModel):
    """A block of time within one day. Slots do not cross midnight."""

    start: time
    end: time
    kind: SlotKind
    title: str = Field(min_length=1)
    place_id: Slug | None = None
    notes: str = ""
    leg: Leg | None = None  # how you get here from the previous slot

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.end <= self.start:
            raise ValueError("slot end must be after start (slots do not cross midnight)")
        if self.kind in (SlotKind.ACTIVITY, SlotKind.MEAL) and self.place_id is None:
            raise ValueError(f"{self.kind.value} slots need a place_id")
        if self.kind is SlotKind.TRANSIT and self.leg is None:
            raise ValueError("transit slots need a leg")
        return self

    @property
    def duration_min(self) -> int:
        start = datetime.combine(dt.date.min, self.start)
        end = datetime.combine(dt.date.min, self.end)
        return int((end - start).total_seconds() // 60)


class DayPlan(StrictModel):
    day: PositiveInt
    theme: str = Field(min_length=1)
    date: dt.date | None = None
    base_area: Slug  # neighborhood id the day centres on
    slots: list[Slot] = Field(min_length=1)
    day_cost_aed: AED = 0.0

    @model_validator(mode="after")
    def _ordered_no_overlap(self) -> Self:
        for prev, nxt in zip(self.slots, self.slots[1:], strict=False):
            if nxt.start < prev.end:
                raise ValueError(
                    f"day {self.day}: slot '{nxt.title}' starts before '{prev.title}' ends"
                )
        return self

    @property
    def place_ids(self) -> list[str]:
        return [s.place_id for s in self.slots if s.place_id is not None]

    def minutes_of(self, *kinds: SlotKind) -> int:
        return sum(s.duration_min for s in self.slots if s.kind in kinds)


class Itinerary(StrictModel):
    """The structured plan. ``budget`` and ``review`` are filled in by later graph nodes."""

    run_id: str = Field(min_length=1)
    request: TripRequest
    hotel: HotelArea
    days: list[DayPlan] = Field(min_length=1)
    budget: BudgetBreakdown | None = None
    alternatives: list[BudgetAlternative] = Field(default_factory=list)
    review: ReviewReport | None = None
    revisions: NonNegativeInt = 0

    @model_validator(mode="after")
    def _matches_request(self) -> Self:
        c = self.request.constraints
        if len(self.days) != c.days:
            raise ValueError(
                f"itinerary has {len(self.days)} days but the request asks for {c.days}"
            )
        if [d.day for d in self.days] != list(range(1, c.days + 1)):
            raise ValueError("days must be numbered 1..n in order")
        if c.start_date is not None:
            for d in self.days:
                expected = c.start_date + timedelta(days=d.day - 1)
                if d.date is not None and d.date != expected:
                    raise ValueError(f"day {d.day} date should be {expected.isoformat()}")
        # Exact: both are copies of the traveller's budget, not computed totals.
        if self.budget is not None and self.budget.budget_aed != c.budget_aed:
            raise ValueError("budget.budget_aed must match the request budget")
        return self

    @property
    def place_ids(self) -> list[str]:
        return [pid for d in self.days for pid in d.place_ids]

    @property
    def is_final(self) -> bool:
        return self.budget is not None and self.review is not None


class ItinerarySnapshot(StrictModel):
    """One draft in the revision history (used for the review diff view)."""

    revision: NonNegativeInt
    itinerary: Itinerary
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    note: str = ""
