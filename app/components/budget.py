"""Budget view: stacked bar by category against the budget line, cost lines, alternatives and
the verified vs estimated split."""

from __future__ import annotations

from typing import Any

import altair as alt
import streamlit as st

from schemas import BudgetAlternative, BudgetBreakdown, BudgetCategory, Source

CATEGORY_COLORS = {
    BudgetCategory.ACCOMMODATION: "#2563eb",
    BudgetCategory.FOOD: "#16a34a",
    BudgetCategory.TRANSPORT: "#f59e0b",
    BudgetCategory.ACTIVITIES: "#9333ea",
    BudgetCategory.CONTINGENCY: "#64748b",
}


def category_rows(budget: BudgetBreakdown) -> list[dict[str, Any]]:
    return [
        {"plan": "Trip total", "category": c.value, "aed": getattr(budget, c.value)}
        for c in BudgetCategory
    ]


def source_split(budget: BudgetBreakdown) -> dict[str, float]:
    """AED by provenance (contingency is part of the estimated share)."""
    return {s.value: round(budget.total_by_source(s), 2) for s in Source}


def line_rows(budget: BudgetBreakdown) -> list[dict[str, Any]]:
    return [
        {
            "category": ln.category.value,
            "day": ln.day,
            "description": ln.description,
            "aed": ln.amount_aed.value,
            "source": ln.amount_aed.source.value,
        }
        for ln in budget.lines
    ]


def alternative_rows(alternatives: list[BudgetAlternative]) -> list[dict[str, Any]]:
    return [
        {
            "applied": a.applied,
            "saving_aed": a.saving_aed,
            "kind": a.kind.value.replace("_", " "),
            "description": a.description,
        }
        for a in sorted(alternatives, key=lambda a: (not a.applied, -a.saving_aed))
    ]


def chart(budget: BudgetBreakdown) -> alt.LayerChart:
    rows = category_rows(budget)
    order = [c.value for c in BudgetCategory]
    bars = (
        alt.Chart(alt.Data(values=rows))
        .mark_bar(height=36)
        .encode(
            x=alt.X("sum(aed):Q", title="AED"),
            y=alt.Y("plan:N", title=None),
            color=alt.Color(
                "category:N",
                sort=order,
                scale=alt.Scale(domain=order, range=[CATEGORY_COLORS[c] for c in BudgetCategory]),
            ),
            order=alt.Order("category_index:Q"),
            tooltip=["category:N", alt.Tooltip("aed:Q", format=",.0f")],
        )
        .transform_calculate(category_index=f"indexof({order}, datum.category)")
    )
    line = (
        alt.Chart(alt.Data(values=[{"budget": budget.budget_aed}]))
        .mark_rule(color="#dc2626", strokeWidth=3, strokeDash=[6, 3])
        .encode(x="budget:Q", tooltip=[alt.Tooltip("budget:Q", title="budget", format=",.0f")])
    )
    return (bars + line).properties(height=110)


def render_budget(budget: BudgetBreakdown | None, alternatives: list[BudgetAlternative]) -> None:
    if budget is None:
        st.info("No budget yet.")
        return
    status = "within budget ✅" if budget.within_budget else "over budget ❌"
    st.markdown(
        f"**Total AED {budget.total:,.0f}** of AED {budget.budget_aed:,.0f} — {status} "
        f"(red line = budget)"
    )
    st.altair_chart(chart(budget), width="stretch")
    cols = st.columns(len(BudgetCategory))
    for col, c in zip(cols, BudgetCategory, strict=True):
        col.metric(c.value.capitalize(), f"AED {getattr(budget, c.value):,.0f}")
    split = source_split(budget)
    st.markdown(
        f":green-badge[verified] AED {split['verified']:,.0f} · "
        f":blue-badge[api] AED {split['api']:,.0f} · "
        f":orange-badge[estimated] AED {split['estimated']:,.0f}"
    )
    with st.expander(f"Cost lines ({len(budget.lines)})"):
        st.dataframe(
            line_rows(budget),
            hide_index=True,
            width="stretch",
            column_config={"aed": st.column_config.NumberColumn("AED", format="%.2f")},
        )
    if alternatives:
        st.markdown("**Cheaper alternatives** (savings measured by re-planning)")
        st.dataframe(
            alternative_rows(alternatives),
            hide_index=True,
            width="stretch",
            column_config={
                "applied": st.column_config.CheckboxColumn("applied"),
                "saving_aed": st.column_config.NumberColumn("saves (AED)", format="%.0f"),
            },
        )
