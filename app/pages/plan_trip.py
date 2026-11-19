"""Plan a trip: a form, no typing (architecture.md §11.2)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import streamlit as st

from agents.orchestrator import form_request
from app.components.results import count_ai_plan, run_slot
from app.context import app_context
from app.state import RunOptions, session, start_run
from data.providers import AttractionQuery
from schemas import Avoidance, HotelTier, Interest, Pace, TripConstraints

PAGE = "form"
TIERS = {"Budget": HotelTier.BUDGET, "Mid-range": HotelTier.MID, "Luxury": HotelTier.LUXURY}
INTEREST_LABELS = {
    Interest.CULTURE: "🕌 Culture",
    Interest.FOOD: "🍽️ Local food",
    Interest.BEACH: "🏖️ Beaches",
    Interest.NATURE: "🌴 Nature",
    Interest.FAMILY: "👨‍👩‍👧 Family",
    Interest.SHOPPING: "🛍️ Shopping",
    Interest.ART: "🎨 Art",
    Interest.ADVENTURE: "🧗 Adventure",
    Interest.NIGHTLIFE: "🌙 Nightlife",
    Interest.RELAXATION: "🧘 Relaxation",
}
AVOID_LABELS = {
    Avoidance.CROWDS: "Crowds",
    Avoidance.LONG_WALKS: "Long walks",
    Avoidance.EARLY_MORNINGS: "Early mornings",
    Avoidance.LATE_NIGHTS: "Late nights",
}


def build_constraints(
    *,
    start: date,
    days: int,
    budget: float,
    adults: int,
    children: int,
    tier: str,
    interests: list[Interest],
    pace: Pace,
    avoid: list[Avoidance],
    vegetarian: bool,
    must_visit: list[str],
) -> TripConstraints:
    """The form's values as a validated request (raises ``ValueError`` with a message)."""
    if not interests:
        raise ValueError("Pick at least one interest.")
    travelers = adults + children
    if travelers > 10:
        raise ValueError("The planner handles up to 10 travellers.")
    return TripConstraints(
        days=days,
        budget_aed=budget,
        travelers=travelers,
        children=children,
        rooms=max(1, math.ceil(adults / 2)),
        start_date=start,
        interests=interests,
        hotel_tier=TIERS[tier],
        avoid=avoid,
        pace=pace,
        dietary=["vegetarian"] if vegetarian else [],
        must_visit=must_visit,
    )


def page() -> None:
    ctx = app_context()
    state = session(st.session_state)
    st.title("Plan a trip")
    st.caption("Pick what you like; the agents choose the places, the hotel area and the route.")
    places = ctx.providers.places.search_attractions(AttractionQuery(limit=1000))
    names = {a.id: a.name for a in sorted(places, key=lambda a: a.name)}
    with st.form("trip-form", border=False):
        a, b, c = st.columns(3)
        today = date.today()
        start = a.date_input("Start date", value=today + timedelta(days=14), min_value=today)
        days = b.number_input("Days", min_value=1, max_value=7, value=4)
        budget = c.number_input(
            "Budget (AED, excluding flights)",
            min_value=300,
            max_value=200_000,
            value=5000,
            step=500,
        )
        a, b, c = st.columns(3)
        adults = a.number_input("Adults", min_value=1, max_value=10, value=2)
        children = b.number_input("Children", min_value=0, max_value=9, value=0)
        tier = c.selectbox("Hotel", list(TIERS), index=0)
        interests = st.pills(
            "Interests",
            list(INTEREST_LABELS),
            format_func=lambda i: INTEREST_LABELS[i],
            selection_mode="multi",
            default=[Interest.CULTURE, Interest.FOOD, Interest.BEACH],
        )
        a, b = st.columns(2)
        pace = a.segmented_control(
            "Pace", list(Pace), format_func=lambda p: p.value.capitalize(), default=Pace.BALANCED
        )
        avoid = b.multiselect("Avoid", list(AVOID_LABELS), format_func=lambda a: AVOID_LABELS[a])
        must = st.multiselect(
            "Must-visit places",
            list(names),
            default=[p for p in state["must_visit"] if p in names],
            format_func=lambda pid: names[pid],
            max_selections=10,
            help="Always included in the plan. Find more under Browse Dubai.",
        )
        vegetarian = st.checkbox("Vegetarian meals")
        submitted = st.form_submit_button("Plan my trip", type="primary")
    if submitted:
        try:
            constraints = build_constraints(
                start=start,
                days=int(days),
                budget=float(budget),
                adults=int(adults),
                children=int(children),
                tier=str(tier),
                interests=list(interests or []),
                pace=pace or Pace.BALANCED,
                avoid=list(avoid),
                vegetarian=vegetarian,
                must_visit=list(must),
            )
        except ValueError as exc:
            st.error(str(exc).splitlines()[0])
        else:
            state["must_visit"] = list(must)
            req = form_request(constraints)
            handle = start_run(
                RunOptions(
                    req.raw_text,
                    use_llm=ctx.use_llm,
                    max_revisions=ctx.max_revisions,
                    data_mode=ctx.mode,
                    request=req,
                ),
                providers=ctx.providers,
                settings=ctx.settings,
            )
            count_ai_plan(handle, state)
            state["runs"][PAGE] = handle
    run_slot(PAGE, providers=ctx.providers, settings=ctx.settings, dev=ctx.dev)
