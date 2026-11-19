"""Saved trips: plans kept for this session, side by side; download and upload trip files."""

from __future__ import annotations

from typing import Any

import streamlit as st

from app.components.itinerary import render_itinerary
from app.context import app_context
from app.state import SavedTrip, itinerary_json, load_itinerary, make_saved, session
from schemas import BudgetCategory, Itinerary


def compare_rows(trips: list[SavedTrip]) -> list[dict[str, Any]]:
    """One row per measure, one column per trip."""

    def stops(it: Itinerary) -> int:
        return sum(1 for d in it.days for s in d.slots if s.kind.value == "activity")

    rows: list[dict[str, Any]] = []
    measures: list[tuple[str, Any]] = [
        ("Days", lambda it: it.request.constraints.days),
        ("Travellers", lambda it: it.request.constraints.travelers),
        ("Hotel area", lambda it: f"{it.hotel.neighborhood_id} ({it.hotel.tier.value})"),
        ("Stops", stops),
        ("Total (AED)", lambda it: round(it.budget.total) if it.budget else None),
        *(
            (
                c.value.capitalize() + " (AED)",
                lambda it, c=c: round(getattr(it.budget, c.value)) if it.budget else None,
            )
            for c in BudgetCategory
        ),
        ("Review score", lambda it: it.review.score if it.review else None),
    ]
    for label, get in measures:
        rows.append({"": label, **{t.name: get(t.itinerary) for t in trips}})
    return rows


def page() -> None:
    ctx = app_context()
    state = session(st.session_state)
    st.title("Saved trips")
    st.caption(
        "Trips saved in this browser session. Download a trip to keep it; upload it later to "
        "see it again (there are no accounts)."
    )
    saved: list[SavedTrip] = state["saved"]
    upload = st.file_uploader("Open a downloaded trip", type="json", key="upload")
    if upload is not None and upload.file_id != state.get("last_upload"):
        state["last_upload"] = upload.file_id
        try:
            saved.append(make_saved(load_itinerary(upload.getvalue())))
            st.toast("Trip added.")
        except ValueError as exc:
            st.error(str(exc))
    if not saved:
        st.info("No saved trips yet. Plan a trip and press ⭐ Save this trip.")
        return
    if len(saved) >= 2:
        picked = st.multiselect(
            "Compare",
            [t.id for t in saved],
            default=[t.id for t in saved[-2:]],
            format_func=lambda i: next(t.name for t in saved if t.id == i),
        )
        chosen = [t for t in saved if t.id in picked]
        if len(chosen) >= 2:
            st.dataframe(compare_rows(chosen), hide_index=True)
    for trip in reversed(saved):
        with st.container(border=True):
            a, b, c = st.columns([4, 1, 1])
            a.markdown(f"**{trip.name}**")
            a.caption(f"saved {trip.saved_at:%H:%M}" + ("" if trip.result else " · from a file"))
            b.download_button(
                "Download",
                itinerary_json(trip.itinerary),
                file_name=f"dubai-trip-{trip.id}.json",
                mime="application/json",
                key=f"dl-{trip.id}",
            )
            if c.button("Delete", key=f"del-{trip.id}"):
                state["saved"] = [t for t in saved if t.id != trip.id]
                st.rerun()
            with st.expander("Show the plan"):
                render_itinerary(
                    trip.itinerary,
                    ctx.providers,
                    [],
                    rationale=trip.result.rationale if trip.result else {},
                    key=f"saved-{trip.id}",
                )
