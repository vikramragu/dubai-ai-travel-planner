"""Browse Dubai: every place on a map and in a filterable list, with "Add to my trip"."""

from __future__ import annotations

from typing import Any

import pydeck as pdk
import streamlit as st

from app.components.itinerary import ATTRIBUTION, BADGES
from app.context import app_context
from app.pages.plan_trip import INTEREST_LABELS
from app.state import session
from data.providers import AttractionQuery
from schemas import Attraction, CrowdLevel, Interest
from schemas.request import MAX_MUST_VISIT


def _price(a: Attraction) -> str:
    r = a.price.value
    return "free" if a.is_free else f"AED {r.min_aed:,.0f}-{r.max_aed:,.0f}"


def place_rows(places: list[Attraction]) -> list[dict[str, Any]]:
    return [
        {
            "id": a.id,
            "name": a.name,
            "area": a.neighborhood_id,
            "interests": ", ".join(t for t in a.tags if t in {i.value for i in Interest}),
            "price": "free"
            if a.is_free
            else f"AED {a.price.value.min_aed:,.0f}-{a.price.value.max_aed:,.0f}",
            "visit (min)": a.typical_duration_min,
            "crowds": a.crowd_level.value,
            "indoor": "yes" if a.indoor else "no",
            "source": BADGES[a.source],
        }
        for a in places
    ]


def add_to_trip(state: Any, ids: list[str]) -> tuple[list[str], int]:
    """Add place ids to the must-visit list (max 10); returns (the list, how many were added)."""
    current = list(state["must_visit"])
    added = 0
    for pid in ids:
        if pid not in current and len(current) < MAX_MUST_VISIT:
            current.append(pid)
            added += 1
    state["must_visit"] = current
    return current, added


def page() -> None:
    ctx = app_context()
    state = session(st.session_state)
    st.title("Browse Dubai")
    st.caption("Every place the planner knows. Select places and add them to your trip.")
    hoods = sorted(h.id for h in ctx.providers.places.neighborhoods())
    a, b = st.columns([3, 2])
    interests = a.pills(
        "Interests",
        list(INTEREST_LABELS),
        format_func=lambda i: INTEREST_LABELS[i],
        selection_mode="multi",
        key="browse-interests",
        persist_state="session",
    )
    areas = b.multiselect("Areas", hoods, key="browse-areas", persist_state="session")
    c1, c2, c3 = st.columns(3)
    free = c1.toggle("Free only", key="browse-free", persist_state="session")
    indoor = c2.toggle("Indoor only", key="browse-indoor", persist_state="session")
    quiet = c3.toggle("Not crowded", key="browse-quiet", persist_state="session")
    places = ctx.providers.places.search_attractions(
        AttractionQuery(
            interests=list(interests or []),
            neighborhood_ids=list(areas),
            free_only=free,
            indoor_only=indoor,
            max_crowd=CrowdLevel.MEDIUM if quiet else None,
            limit=500,
        )
    )
    places = sorted(places, key=lambda p: p.name)
    st.markdown(f"**{len(places)} places**")
    if places:
        points: list[dict[str, Any]] = [
            {
                "name": p.name,
                "lat": p.location.lat,
                "lon": p.location.lon,
                "color": [217, 119, 6] if p.id in state["must_visit"] else [3, 105, 161],
            }
            for p in places
        ]
        lat = sum(p["lat"] for p in points) / len(points)
        lon = sum(p["lon"] for p in points) / len(points)
        st.pydeck_chart(
            pdk.Deck(
                layers=[
                    pdk.Layer(
                        "ScatterplotLayer",
                        data=points,
                        get_position="[lon, lat]",
                        get_fill_color="color",
                        get_radius=150,
                        radius_min_pixels=5,
                        pickable=True,
                    )
                ],
                initial_view_state=pdk.ViewState(latitude=lat, longitude=lon, zoom=10),
                tooltip={"text": "{name}"},
            ),
            height=380,
        )
        table = st.dataframe(
            place_rows(places),
            hide_index=True,
            on_select="rerun",
            selection_mode="multi-row",
            column_config={"id": None},
            key="browse-table",
        )
        picked = [places[i].id for i in table.selection.rows]
        if st.button(
            f"Add {len(picked)} to my trip" if picked else "Select places to add them",
            disabled=not picked,
            type="primary",
        ):
            current, added = add_to_trip(state, picked)
            st.toast(f"Added {added}. Your trip has {len(current)} must-visit place(s).")
    _my_list(state, ctx)
    st.caption(ATTRIBUTION)


def _my_list(state: Any, ctx: Any) -> None:
    if not state["must_visit"]:
        return
    with st.container(border=True):
        st.markdown("**Your must-visit places** (pre-selected on Plan a trip)")
        for pid in list(state["must_visit"]):
            place = ctx.providers.places.get_place(pid)
            col, btn = st.columns([5, 1])
            col.markdown(f"- {place.name if place else pid}")
            if btn.button("Remove", key=f"mv-remove-{pid}"):
                state["must_visit"] = [p for p in state["must_visit"] if p != pid]
                st.rerun()
