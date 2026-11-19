"""Itinerary view: day tabs with numbered stop cards, the legs between them, weather, a route
map with matching numbered pins, provenance badges, "Why this?" and change buttons."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pydeck as pdk
import streamlit as st

from data.providers import DataProviders
from schemas import CostLine, DayPlan, HeatLevel, Itinerary, LatLon, Slot, SlotKind, Source

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


HEAT_BADGES = {
    HeatLevel.COMFORTABLE: "☀️",
    HeatLevel.WARM: "🌤️",
    HeatLevel.HOT: "🥵",
    HeatLevel.EXTREME: "🔥",
}
ChangeCallback = Callable[[str, str], None]  # (kind: "remove" | "swap", place id)


def _map(points: list[dict[str, Any]]) -> pdk.Deck:
    """The day's route with numbered pins matching the cards (H = the hotel area)."""
    path = [[p["lon"], p["lat"]] for p in points]
    last = len(points) - 1
    for i, p in enumerate(points):
        hotel = p["kind"] == "hotel"
        p["color"] = [180, 83, 9] if hotel else [3, 105, 161]
        p["label"] = "H" if hotel else str(p["order"])
        if hotel and i == last:
            p["label"] = ""  # the hotel appears once (start and end are the same place)
    # centre on the bounding box (not the average), so an outlying stop stays in view
    lat = (max(p["lat"] for p in points) + min(p["lat"] for p in points)) / 2
    lon = (max(p["lon"] for p in points) + min(p["lon"] for p in points)) / 2
    return pdk.Deck(
        map_style=None,  # Streamlit picks a light or dark base map to match the theme
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
                get_radius=160,
                radius_min_pixels=11,
                pickable=True,
            ),
            pdk.Layer(
                "TextLayer",
                data=points,
                get_position="[lon, lat]",
                get_text="label",
                get_color=[255, 255, 255],
                get_size=13,
                font_weight=700,
            ),
        ],
        initial_view_state=pdk.ViewState(latitude=lat, longitude=lon, zoom=fit_zoom(points)),
        tooltip={"text": "{label}. {name}"},
    )


def fit_zoom(points: list[dict[str, Any]]) -> float:
    """A zoom level that shows the whole day (close stops zoom in, so pins don't overlap)."""
    span = max(
        max(p["lat"] for p in points) - min(p["lat"] for p in points),
        max(p["lon"] for p in points) - min(p["lon"] for p in points),
    )
    for limit, zoom in ((0.015, 14.0), (0.03, 13.0), (0.06, 12.0), (0.12, 11.0), (0.25, 10.0)):
        if span <= limit:
            return zoom
    return 9.0


def area_names(providers: DataProviders) -> dict[str, str]:
    """Neighborhood id -> display name ("al-ras" -> "Deira Souks (Al Ras)")."""
    return {h.id: h.name for h in providers.places.neighborhoods()}


def weather_badge(day: DayPlan, providers: DataProviders) -> str:
    """☀️ 27°C (forecast) or (typical), from the weather provider for the day's date."""
    if day.date is None:
        return ""
    try:
        w = providers.weather.forecast(day.date, 1)[0]
    except Exception:  # weather is decoration here; never break the plan view
        return ""
    kind = "forecast" if w.source is Source.API else "typical"
    return f"{HEAT_BADGES[w.heat]} {w.temp_max_c:.0f}°C ({kind})"


def _card(
    n: int,
    slot: Slot,
    day: DayPlan,
    costs: dict[tuple[int | None, str], CostLine],
    providers: DataProviders,
    rationale: dict[str, str],
    on_change: ChangeCallback | None,
    key: str,
) -> None:
    assert slot.place_id is not None
    place = providers.places.get_place(slot.place_id)
    line = costs.get((day.day, slot.place_id))
    cost = line.amount_aed.value if line else 0.0
    badge = MD_BADGES[place.source] if place else ""
    area = (
        f" · {area_names(providers).get(place.neighborhood_id, place.neighborhood_id)}"
        if place
        else ""
    )
    with st.container(border=True):
        left, right = st.columns([5, 2])
        with left:
            st.markdown(
                f"**{n}** · `{slot.start:%H:%M}-{slot.end:%H:%M}` {KIND_ICONS[slot.kind]} "
                f"**{slot.title}**"
            )
            price = "free" if cost == 0 else f"AED {cost:,.0f}"
            st.caption(f"{price}{area} · {badge}")
        with right:
            why = rationale.get(slot.place_id) or (
                "Chosen by the scheduler to fit the day near the previous stop."
                if slot.kind is SlotKind.MEAL
                else ""
            )
            with st.popover("Why this?", width="stretch"):
                st.write(why or "Picked to match your interests in this area.")
                if place and place.opening_hours is None:
                    st.caption("Opening hours unknown: check before you go.")
            if on_change is not None and slot.kind is SlotKind.ACTIVITY:
                with st.popover("Change", width="stretch"):
                    st.button(
                        "🔁 Swap for another place",
                        key=f"{key}-swap-{day.day}-{n}",
                        on_click=on_change,
                        args=("swap", slot.place_id),
                        width="stretch",
                    )
                    st.button(
                        "🗑️ Remove this stop",
                        key=f"{key}-remove-{day.day}-{n}",
                        on_click=on_change,
                        args=("remove", slot.place_id),
                        width="stretch",
                    )


def render_itinerary(
    itinerary: Itinerary,
    providers: DataProviders,
    unscheduled: list[str],
    *,
    rationale: dict[str, str] | None = None,
    on_change: ChangeCallback | None = None,
    key: str = "plan",
    dev: bool = False,
) -> None:
    """Day tabs: one card per stop and meal (numbered like the map pins), the legs between
    them, the day's weather and cost; "Why this?" and change buttons on each stop."""
    rationale = rationale or {}
    areas = area_names(providers)
    hotel = itinerary.hotel
    rate = hotel.nightly_rate
    with st.container(border=True):
        st.markdown(
            f"🏨 **Hotel area: {areas.get(hotel.neighborhood_id, hotel.neighborhood_id)}** "
            f"({hotel.tier.value}) · AED "
            f"{rate.value.min_aed:,.0f}-{rate.value.max_aed:,.0f}/night {MD_BADGES[rate.source]}"
        )
        if hotel.rationale:
            st.caption(f"Why this area? {hotel.rationale}")
    costs = _cost_index(itinerary)
    tabs = st.tabs([f"Day {d.day}" for d in itinerary.days])
    for tab, day in zip(tabs, itinerary.days, strict=True):
        with tab:
            date = f" · {day.date:%a %d %b}" if day.date else ""
            st.markdown(f"#### {day.theme}{date}")
            weather = weather_badge(day, providers)
            st.caption(
                " · ".join(
                    x
                    for x in (
                        weather,
                        f"day cost AED {day.day_cost_aed:,.0f}",
                        areas.get(day.base_area, day.base_area),
                    )
                    if x
                )
            )
            cards, map_col = st.columns([3, 2])
            with cards:
                n = 0
                for slot in day.slots:
                    if slot.place_id is not None:
                        n += 1
                        _card(n, slot, day, costs, providers, rationale, on_change, key)
                    elif slot.kind is SlotKind.TRANSIT and slot.leg is not None:
                        leg = slot.leg
                        icon = MODE_ICONS.get(leg.mode.value, "➡️")
                        fare = costs.get((day.day, f"{leg.from_id}->{leg.to_id}"))
                        paid = f" · AED {fare.amount_aed.value:,.0f}" if fare else ""
                        st.caption(f"{icon} {slot.title} · {leg.duration_min.value} min{paid}")
                    elif slot.kind is SlotKind.REST:
                        st.caption(f"☕ Free time `{slot.start:%H:%M}-{slot.end:%H:%M}`")
            with map_col:
                points = route_points(day, itinerary, providers)
                if len(points) >= 2:
                    st.pydeck_chart(_map(points), height=420)
            if dev:
                with st.expander("Table view (dev)"):
                    st.dataframe(slot_rows(day, itinerary, providers), hide_index=True)
    if unscheduled:
        names = []
        for pid in unscheduled:
            place = providers.places.get_place(pid)
            names.append(place.name if place else pid)
        st.caption("Picked but didn't fit the schedule: " + ", ".join(names))
    st.caption(ATTRIBUTION)
