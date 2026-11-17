"""Cost lines, the category-wise budget breakdown and cheaper alternatives."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Self

from pydantic import Field, NonNegativeFloat, PositiveFloat, PositiveInt, model_validator

from schemas.base import StrictModel
from schemas.common import AED, Slug
from schemas.provenance import Source, SourcedValue

# Rounding tolerance when checking that stored totals match the lines.
_TOLERANCE = 0.01


class BudgetCategory(StrEnum):
    ACCOMMODATION = "accommodation"
    FOOD = "food"
    TRANSPORT = "transport"
    ACTIVITIES = "activities"
    CONTINGENCY = "contingency"


class CostLine(StrictModel):
    category: BudgetCategory
    description: str = Field(min_length=1)
    amount_aed: SourcedValue[NonNegativeFloat]  # for the whole party
    day: PositiveInt | None = None
    ref_id: str | None = None  # place id, leg "from->to", hotel area id, ...


def _sum(lines: Iterable[CostLine], category: BudgetCategory) -> float:
    return round(sum(ln.amount_aed.value for ln in lines if ln.category is category), 2)


class BudgetBreakdown(StrictModel):
    """Category totals must equal the sum of their lines; build with :meth:`from_lines`."""

    budget_aed: PositiveFloat
    lines: list[CostLine]
    accommodation: AED
    food: AED
    transport: AED
    activities: AED
    contingency: AED
    total: AED
    within_budget: bool

    @classmethod
    def from_lines(cls, lines: list[CostLine], budget_aed: float) -> BudgetBreakdown:
        totals = {c: _sum(lines, c) for c in BudgetCategory}
        total = round(sum(totals.values()), 2)
        return cls(
            budget_aed=budget_aed,
            lines=lines,
            accommodation=totals[BudgetCategory.ACCOMMODATION],
            food=totals[BudgetCategory.FOOD],
            transport=totals[BudgetCategory.TRANSPORT],
            activities=totals[BudgetCategory.ACTIVITIES],
            contingency=totals[BudgetCategory.CONTINGENCY],
            total=total,
            within_budget=total <= budget_aed,
        )

    @model_validator(mode="after")
    def _totals_match_lines(self) -> Self:
        for category in BudgetCategory:
            stored: float = getattr(self, category.value)
            if abs(stored - _sum(self.lines, category)) > _TOLERANCE:
                raise ValueError(f"{category.value} total does not match its cost lines")
        categories = (
            self.accommodation + self.food + self.transport + self.activities + self.contingency
        )
        if abs(self.total - categories) > _TOLERANCE:
            raise ValueError("total must equal the sum of the category totals")
        # Exact comparison, same as from_lines: being 1 fils over budget is still over.
        if self.within_budget != (self.total <= self.budget_aed):
            raise ValueError("within_budget is inconsistent with total and budget_aed")
        return self

    @property
    def remaining_aed(self) -> float:
        """Positive when under budget, negative when over."""
        return round(self.budget_aed - self.total, 2)

    def total_by_source(self, source: Source) -> float:
        return round(
            sum(ln.amount_aed.value for ln in self.lines if ln.amount_aed.source is source), 2
        )


class AlternativeKind(StrEnum):
    CHEAPER_HOTEL = "cheaper_hotel"
    CHEAPER_TRANSPORT = "cheaper_transport"
    FREE_ATTRACTION = "free_attraction"
    CHEAPER_FOOD = "cheaper_food"
    DROP_ACTIVITY = "drop_activity"


class BudgetAlternative(StrictModel):
    """A cheaper swap proposed when the plan is over budget (architecture.md §5.3)."""

    id: Slug
    kind: AlternativeKind
    description: str = Field(min_length=1)
    saving_aed: PositiveFloat
    replaces_id: str | None = None
    replacement_id: str | None = None
    day: PositiveInt | None = None
    applied: bool = False
