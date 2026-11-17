"""Itinerary view: day tabs with a slot timeline, a route map and provenance badges."""

from __future__ import annotations

from typing import Any

import pydeck as pdk
import streamlit as st

from data.providers import DataProviders
from schemas import CostLine, DayPlan, Itinerary, LatLon, Slot, SlotKind, Source

BADGES = {Source.VERIFIED: "✅ verified", Source.API: "🌐 api", Source.ESTIMATED: "≈ estimated"}
MD_BADGES = {
    Source.VERIFIED: ":green-badge[verified]",
    Source.API: ":blue-badge[api]",
    Source.ESTIMATED: ":orange-badge[estimated]",
}
KIND_ICONS = {
    SlotKind.ACTIVITY: "📍",
    SlotKind.MEAL: "🍽️",
    SlotKind.TRANSIT: "🚕",
    SlotKind.REST: "☕",
}
MODE_ICONS = {"walk": "🚶", "taxi": "🚕", "metro": "🚇", "abra": "⛴️", "tram": "🚋", "bus": "🚌"}
ATTRIBUTION = (
    "Place data © OpenStreetMap contributors (ODbL) for places tagged verified with an OSM "
    "link; fares from RTA; travel times are heuristic estimates. Base map © CARTO, "
    "© OpenStreetMap contributors."
)


def _cost_index(itinerary: Itinerary) -> dict[tuple[int | None, str], CostLine]:
    if itinerary.budget is None:
        return {}
    return {(ln.day, ln.ref_id): ln for ln in itinerary.budget.lines if ln.ref_id}


def _place_source(slot: Slot, providers: DataProviders) -> Source | None:
    if slot.place_id is None:
        return None
    place = providers.places.get_place(slot.place_id)
    return place.source if place else None


def slot_rows(day: DayPlan, itinerary: Itinerary, providers: DataProviders) -> list[dict[str, Any]]:
    """One row per slot: time, what, how you get there, cost for the party, provenance."""
    costs = _cost_index(itinerary)
    rows = []
    for s in day.slots:
        row: dict[str, Any] = {
            "time": f"{s.start:%H:%M}-{s.end:%H:%M}",
            "what": f"{KIND_ICONS[s.kind]} {s.title}",
            "minutes": s.duration_min,
            "cost_aed": None,
            "source": "",
        }
        if s.kind is SlotKind.TRANSIT and s.leg is not None:
            line = costs.get((day.day, f"{s.leg.from_id}->{s.leg.to_id}"))
            row["what"] = f"{MODE_ICONS.get(s.leg.mode.value, '➡️')} {s.title}"
            row["cost_aed"] = line.amount_aed.value if line else None
            row["source"] = BADGES[s.leg.duration_min.source]
        elif s.place_id is not None:
            line = costs.get((day.day, s.place_id))
            row["cost_aed"] = line.amount_aed.value if line else 0.0
            src = _place_source(s, providers)
            row["source"] = BADGES[src] if src else ""
        rows.append(row)
    return rows


def location_of(place_or_area: str, providers: DataProviders) -> LatLon | None:
    place = providers.places.get_place(place_or_area)
    if place is not None:
        return place.location
    for hood in providers.places.neighborhoods():
        if hood.id == place_or_area:
            return hood.location
    return None


def route_points(
    day: DayPlan, itinerary: Itinerary, providers: DataProviders
) -> list[dict[str, Any]]:
    """The day's stops in order, starting and ending at the hotel area."""
    hotel = itinerary.hotel.neighborhood_id
    ids = [hotel, *day.place_ids, hotel]
    points = []
    for order, pid in enumerate(ids):
        loc = location_of(pid, providers)
        if loc is None:
            continue
        place = providers.places.get_place(pid)
        name = place.name if place else f"Hotel area: {pid}"
        kind = "hotel" if pid == hotel and place is None else "stop"
        points.append(
            {"order": order, "id": pid, "name": name, "kind": kind, "lat": loc.lat, "lon": loc.lon}
        )
    return points


def _map(points: list[dict[str, Any]]) -> pdk.Deck:
    path = [[p["lon"], p["lat"]] for p in points]
    for p in points:
        p["color"] = [180, 83, 9] if p["kind"] == "hotel" else [3, 105, 161]
    lat = sum(p["lat"] for p in points) / len(points)
    lon = sum(p["lon"] for p in points) / len(points)
    return pdk.Deck(
        layers=[
            pdk.Layer(
                "PathLayer",
                data=[{"path": path}],
                get_path="path",
                get_color=[3, 105, 161, 160],
                width_min_pixels=3,
            ),
            pdk.Layer(
                "ScatterplotLayer",
                data=points,
                get_position="[lon, lat]",
                get_fill_color="color",
                get_radius=120,
                radius_min_pixels=6,
                pickable=True,
            ),
        ],
        initial_view_state=pdk.ViewState(latitude=lat, longitude=lon, zoom=11),
        tooltip={"text": "{order}. {name}"},
    )


def render_itinerary(
    itinerary: Itinerary, providers: DataProviders, unscheduled: list[str]
) -> None:
    hotel = itinerary.hotel
    rate = hotel.nightly_rate
    st.markdown(
        f"**Hotel area:** {hotel.neighborhood_id} ({hotel.tier.value}) · AED "
        f"{rate.value.min_aed:,.0f}-{rate.value.max_aed:,.0f}/night {MD_BADGES[rate.source]}"
    )
    if hotel.rationale:
        st.caption(hotel.rationale)
    tabs = st.tabs([f"Day {d.day}" for d in itinerary.days])
    for tab, day in zip(tabs, itinerary.days, strict=True):
        with tab:
            date = f" · {day.date:%a %d %b}" if day.date else ""
            st.markdown(f"#### {day.theme}{date}")
            st.caption(f"Base area: {day.base_area} · day cost AED {day.day_cost_aed:,.0f}")
            st.dataframe(
                slot_rows(day, itinerary, providers),
                hide_index=True,
                width="stretch",
                column_config={
                    "cost_aed": st.column_config.NumberColumn("cost (AED)", format="%.0f"),
                    "minutes": st.column_config.NumberColumn("min"),
                },
            )
            points = route_points(day, itinerary, providers)
            if len(points) >= 2:
                st.pydeck_chart(_map(points), height=360)
    if unscheduled:
        st.caption("Picked but didn't fit the schedule: " + ", ".join(unscheduled))
    st.caption(ATTRIBUTION)
