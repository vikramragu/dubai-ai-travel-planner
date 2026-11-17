"""Legs, travel matrix, budget breakdown and review report."""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from schemas import (
    AlternativeKind,
    BudgetAlternative,
    BudgetBreakdown,
    BudgetCategory,
    CostLine,
    HotelArea,
    HotelTier,
    Leg,
    LogisticsResult,
    PriceRange,
    ReviewIssue,
    ReviewReport,
    RouteTarget,
    Severity,
    Source,
    TransportMode,
    TravelMatrix,
    TravelMatrixEntry,
    estimated,
    verified,
)


def leg(a: str = "a", b: str = "b", minutes: int = 10, cost: float = 5.0) -> Leg:
    return Leg(
        from_id=a,
        to_id=b,
        mode=TransportMode.TAXI,
        duration_min=estimated(minutes),
        distance_km=estimated(3.0),
        cost_aed=estimated(cost),
    )


def entry(a: str, b: str, mode: TransportMode = TransportMode.METRO) -> TravelMatrixEntry:
    return TravelMatrixEntry(
        from_id=a, to_id=b, mode=mode, duration_min=estimated(20), distance_km=estimated(8.0)
    )


class TestLogistics:
    def test_leg_valid(self) -> None:
        assert leg().cost_aed.value == 5.0

    def test_leg_needs_distinct_ends(self) -> None:
        with pytest.raises(ValidationError, match="different"):
            leg("a", "a")

    @pytest.mark.parametrize(("minutes", "cost"), [(-1, 5.0), (10, -0.5)])
    def test_leg_non_negative(self, minutes: int, cost: float) -> None:
        with pytest.raises(ValidationError):
            leg(minutes=minutes, cost=cost)

    def test_matrix_lookup(self) -> None:
        m = TravelMatrix(
            entries=[entry("deira", "jumeirah"), entry("deira", "jumeirah", TransportMode.TAXI)]
        )
        assert m.lookup("deira", "jumeirah", TransportMode.METRO) is not None
        assert m.lookup("jumeirah", "deira", TransportMode.METRO) is not None  # symmetric
        assert m.lookup("jumeirah", "deira", TransportMode.METRO, symmetric=False) is None
        assert m.lookup("deira", "jumeirah", TransportMode.BUS) is None

    def test_matrix_rejects_duplicates(self) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            TravelMatrix(entries=[entry("a", "b"), entry("a", "b")])

    def test_logistics_result_needs_hotel(self) -> None:
        with pytest.raises(ValidationError):
            LogisticsResult(hotel_areas=[], travel_matrix=TravelMatrix())
        hotel = HotelArea(
            neighborhood_id="deira",
            tier=HotelTier.BUDGET,
            nightly_rate=estimated(PriceRange(min_aed=200, max_aed=300)),
            metro_access=True,
            beach_access=False,
        )
        result = LogisticsResult(hotel_areas=[hotel], travel_matrix=TravelMatrix())
        assert result.recommended_hotel is hotel


def cost(category: BudgetCategory, amount: float, source: Source = Source.ESTIMATED) -> CostLine:
    value = (
        estimated(amount)
        if source is Source.ESTIMATED
        else verified(amount, "https://www.rta.ae", date(2026, 11, 1))
    )
    return CostLine(category=category, description=f"{category.value} line", amount_aed=value)


class TestBudget:
    def test_from_lines_totals(self) -> None:
        lines = [
            cost(BudgetCategory.ACCOMMODATION, 900),
            cost(BudgetCategory.FOOD, 400),
            cost(BudgetCategory.FOOD, 200),
            cost(BudgetCategory.TRANSPORT, 150, Source.VERIFIED),
        ]
        b = BudgetBreakdown.from_lines(lines, budget_aed=2000)
        assert (b.accommodation, b.food, b.transport, b.activities) == (900, 600, 150, 0)
        assert b.total == 1650
        assert b.within_budget
        assert b.remaining_aed == 350
        assert b.total_by_source(Source.VERIFIED) == 150
        assert b.total_by_source(Source.ESTIMATED) == 1500

    def test_over_budget(self) -> None:
        b = BudgetBreakdown.from_lines([cost(BudgetCategory.ACCOMMODATION, 6000)], budget_aed=5000)
        assert not b.within_budget
        assert b.remaining_aed == -1000

    def test_inconsistent_totals_rejected(self) -> None:
        good = BudgetBreakdown.from_lines([cost(BudgetCategory.FOOD, 100)], budget_aed=500)
        with pytest.raises(ValidationError, match="food"):
            BudgetBreakdown.model_validate({**good.model_dump(), "food": 80})
        with pytest.raises(ValidationError, match="total"):
            BudgetBreakdown.model_validate({**good.model_dump(), "total": 90})
        with pytest.raises(ValidationError, match="within_budget"):
            BudgetBreakdown.model_validate({**good.model_dump(), "within_budget": False})

    @given(
        st.lists(
            st.tuples(st.sampled_from(list(BudgetCategory)), st.floats(0, 5000)),
            max_size=30,
        ),
        st.floats(1, 50_000),
    )
    def test_total_equals_sum_of_lines(
        self, items: list[tuple[BudgetCategory, float]], budget: float
    ) -> None:
        b = BudgetBreakdown.from_lines([cost(c, a) for c, a in items], budget_aed=budget)
        assert b.total == pytest.approx(sum(a for _, a in items), abs=0.05)
        assert b.within_budget == (b.total <= budget)

    def test_alternative_saving_positive(self) -> None:
        BudgetAlternative(
            id="metro-day-2",
            kind=AlternativeKind.CHEAPER_TRANSPORT,
            description="Take the metro instead of taxis on day 2",
            saving_aed=120,
            day=2,
        )
        with pytest.raises(ValidationError):
            BudgetAlternative(
                id="x", kind=AlternativeKind.CHEAPER_FOOD, description="no saving", saving_aed=0
            )


def issue(severity: Severity, route: RouteTarget = RouteTarget.BUDGET) -> ReviewIssue:
    return ReviewIssue(code="test_issue", severity=severity, message="m", route_to=route)


class TestReview:
    def test_minor_only_passes(self) -> None:
        r = ReviewReport.from_issues([issue(Severity.MINOR)], score=90)
        assert r.passed
        assert r.revision_targets == set()

    @pytest.mark.parametrize("severity", [Severity.BLOCKER, Severity.MAJOR])
    def test_blocking_issue_fails(self, severity: Severity) -> None:
        r = ReviewReport.from_issues([issue(severity, RouteTarget.DESTINATION)], score=60)
        assert not r.passed
        assert r.revision_targets == {RouteTarget.DESTINATION}

    def test_cannot_pass_with_blocker(self) -> None:
        with pytest.raises(ValidationError, match="passed"):
            ReviewReport(issues=[issue(Severity.BLOCKER)], score=95, passed=True)

    def test_cannot_fail_without_issues(self) -> None:
        with pytest.raises(ValidationError, match="passed"):
            ReviewReport(issues=[], score=95, passed=False)

    def test_score_range(self) -> None:
        with pytest.raises(ValidationError):
            ReviewReport.from_issues([], score=101)

    def test_issue_code_format(self) -> None:
        with pytest.raises(ValidationError):
            ReviewIssue(
                code="Over Budget",
                severity=Severity.MINOR,
                message="m",
                route_to=RouteTarget.BUDGET,
            )

    def test_by_severity(self) -> None:
        r = ReviewReport.from_issues([issue(Severity.MINOR), issue(Severity.MAJOR)], score=70)
        assert len(r.by_severity(Severity.MAJOR)) == 1
