"""Compact views of tool results for LLM prompts.

The full records carry coordinates, provenance references and per-weekday opening windows
(~11k tokens for all attractions). The LLM only needs a few fields to rank and explain, and it
may only cite these fields (there are no descriptions or ratings in the data).
"""

from __future__ import annotations

from typing import Any

from schemas import (
    Attraction,
    HotelArea,
    Leg,
    OpeningHours,
    PriceRange,
    Restaurant,
    Source,
    SourcedValue,
    Weekday,
)

_DAY = {
    Weekday.MON: "Mon",
    Weekday.TUE: "Tue",
    Weekday.WED: "Wed",
    Weekday.THU: "Thu",
    Weekday.FRI: "Fri",
    Weekday.SAT: "Sat",
    Weekday.SUN: "Sun",
}


def hours_summary(hours: OpeningHours | None) -> str:
    """'always open', 'unknown', or e.g. 'Mon-Thu,Sat-Sun 08:30-20:30; Fri 14:30-20:30'."""
    if hours is None:
        return "unknown"
    if hours.always_open:
        return "always open"
    groups: dict[str, list[Weekday]] = {}
    for day in Weekday:
        windows = hours.weekly.get(day, [])
        key = ",".join(f"{w.opens:%H:%M}-{w.closes:%H:%M}" for w in windows) or "closed"
        groups.setdefault(key, []).append(day)
    parts = []
    for key, days in groups.items():
        if key == "closed":
            continue
        names = [_DAY[d] for d in days]
        label = "daily" if len(days) == 7 else ",".join(names)
        parts.append(f"{label} {key}")
    return "; ".join(parts) or "closed"


def price_summary(price: SourcedValue[PriceRange]) -> str:
    r = price.value
    if r.is_free:
        return "free"
    amount = (
        f"AED {r.min_aed:,.0f}"
        if r.min_aed == r.max_aed
        else (f"AED {r.min_aed:,.0f}-{r.max_aed:,.0f}")
    )
    tag = "verified" if price.source is Source.VERIFIED else price.source.value
    return f"{amount} ({tag})"


def attraction_view(a: Attraction) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "area": a.neighborhood_id,
        "category": a.category,
        "tags": [t for t in a.tags if t != "free"],
        "crowd": a.crowd_level.value,
        "price_pp": price_summary(a.price),
        "duration_min": a.typical_duration_min,
        "indoor": a.indoor,
        "hours": hours_summary(a.opening_hours),
    }


def restaurant_view(r: Restaurant) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "area": r.neighborhood_id,
        "cuisine": r.cuisine,
        "band": r.price_band.value,
        "price_pp": price_summary(r.price),
        "meals": [m.value for m in r.meal_types],
        "crowd": r.crowd_level.value,
        "hours": hours_summary(r.opening_hours),
    }


def hotel_view(h: HotelArea) -> dict[str, Any]:
    return {
        "area": h.neighborhood_id,
        "tier": h.tier.value,
        "nightly": price_summary(h.nightly_rate),
        "metro": h.metro_access,
        "beach": h.beach_access,
        "notes": h.rationale,
    }


def leg_view(leg: Leg) -> dict[str, Any]:
    return {
        "from": leg.from_id,
        "to": leg.to_id,
        "mode": leg.mode.value,
        "minutes": leg.duration_min.value,
        "km": leg.distance_km.value,
        "cost_pp_aed": leg.cost_aed.value,
    }
