"""Review & revisions: the issues found each round, where each was routed, and what changed
between draft n and draft n+1."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import streamlit as st

from core.tracing import TraceEvent
from schemas import BudgetCategory, Itinerary, ItinerarySnapshot

SEVERITY_ICONS = {"blocker": "⛔", "major": "🟠", "minor": "🔹"}


def issue_rows(itinerary: Itinerary) -> list[dict[str, Any]]:
    if itinerary.review is None:
        return []
    return [
        {
            "severity": f"{SEVERITY_ICONS[i.severity.value]} {i.severity.value}",
            "code": i.code,
            "routed to": i.route_to.value,
            "day": i.day,
            "place": i.place_id,
            "from": i.origin.value,
            "message": i.message,
        }
        for i in itinerary.review.issues
    ]


@dataclass
class DayChange:
    day: int
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)  # same place, different start time
    theme: tuple[str, str] | None = None

    @property
    def empty(self) -> bool:
        return not (self.removed or self.added or self.moved or self.theme)


@dataclass
class DraftDiff:
    before: int
    after: int
    hotel: tuple[str, str] | None = None
    days: list[DayChange] = field(default_factory=list)
    costs: dict[str, tuple[float, float]] = field(default_factory=dict)  # category: (old, new)
    score: tuple[float, float] | None = None

    @property
    def total(self) -> tuple[float, float] | None:
        return self.costs.get("total")


def diff_drafts(a: ItinerarySnapshot, b: ItinerarySnapshot) -> DraftDiff:
    """Slot and cost changes from draft ``a`` to draft ``b``."""
    old, new = a.itinerary, b.itinerary
    diff = DraftDiff(a.revision, b.revision)
    oh = f"{old.hotel.neighborhood_id} ({old.hotel.tier.value})"
    nh = f"{new.hotel.neighborhood_id} ({new.hotel.tier.value})"
    if oh != nh:
        diff.hotel = (oh, nh)
    for od, nd in zip(old.days, new.days, strict=False):
        before = {s.place_id: s.start for s in od.slots if s.place_id}
        after = {s.place_id: s.start for s in nd.slots if s.place_id}
        change = DayChange(
            od.day,
            removed=[p for p in before if p not in after],
            added=[p for p in after if p not in before],
            moved=[p for p in before if p in after and before[p] != after[p]],
            theme=(od.theme, nd.theme) if od.theme != nd.theme else None,
        )
        if not change.empty:
            diff.days.append(change)
    if old.budget is not None and new.budget is not None:
        for c in [*BudgetCategory, "total"]:
            key = c.value if isinstance(c, BudgetCategory) else c
            o, n = getattr(old.budget, key), getattr(new.budget, key)
            if round(o, 2) != round(n, 2):
                diff.costs[key] = (o, n)
    if old.review is not None and new.review is not None and old.review.score != new.review.score:
        diff.score = (old.review.score, new.review.score)
    return diff


def _render_diff(diff: DraftDiff) -> None:
    st.markdown(f"**Draft {diff.before} → draft {diff.after}**")
    if diff.score:
        st.markdown(f"- Review score: {diff.score[0]:.0f} → **{diff.score[1]:.0f}**")
    if diff.hotel:
        st.markdown(f"- Hotel area: ~~{diff.hotel[0]}~~ → **{diff.hotel[1]}**")
    for name, (o, n) in diff.costs.items():
        sign = "+" if n > o else "-"
        st.markdown(f"- {name.capitalize()}: AED {o:,.0f} → **{n:,.0f}** ({sign}{abs(n - o):,.0f})")
    for d in diff.days:
        parts = []
        if d.theme:
            parts.append(f"theme “{d.theme[1]}”")
        if d.removed:
            parts.append("removed " + ", ".join(f"~~{p}~~" for p in d.removed))
        if d.added:
            parts.append("added " + ", ".join(f"**{p}**" for p in d.added))
        if d.moved:
            parts.append(f"{len(d.moved)} stop(s) re-timed")
        st.markdown(f"- Day {d.day}: " + "; ".join(parts))
    if not (diff.score or diff.hotel or diff.costs or diff.days):
        st.caption("No changes.")


def render_review(
    history: list[ItinerarySnapshot], trace: list[TraceEvent], stop_reason: str
) -> None:
    # "Revision n: sending issues to ..." events (the finalize decision has no targets)
    routes = {
        e.revision: e.summary
        for e in trace
        if e.agent == "router" and e.payload and "targets" in e.payload
    }
    for i, snap in enumerate(history):
        r = snap.itinerary.review
        label = "First draft" if snap.revision == 0 else f"Revision {snap.revision}"
        verdict = "passed" if r and r.passed else "needs revision"
        with st.expander(f"{label}: score {r.score:.0f}, {verdict}" if r else label, expanded=True):
            if r:
                st.caption(r.summary)
            rows = issue_rows(snap.itinerary)
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            else:
                st.markdown("No issues.")
            decision = routes.get(snap.revision + 1) if i + 1 < len(history) else None
            if decision:
                st.markdown(f"**Router:** {decision}")
            if i + 1 < len(history):
                _render_diff(diff_drafts(snap, history[i + 1]))
    if stop_reason != "passed":
        st.warning(f"The loop stopped ({stop_reason.replace('_', ' ')}); the best draft is shown.")
