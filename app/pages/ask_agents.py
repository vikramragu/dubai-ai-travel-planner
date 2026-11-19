"""Ask the agents: describe the trip in your own words (architecture.md §11.2)."""

from __future__ import annotations

import streamlit as st

from agents.skills.scripted import example_request
from app.components.prechecks import cheapest_night, precheck
from app.components.results import count_ai_plan, run_slot
from app.context import app_context
from app.state import RunOptions, session, start_run

PAGE = "text"
EXAMPLES = {
    "Culture & beaches": example_request().raw_text,
    "Family weekend": "2 days in Dubai for a family of four with two kids, budget AED 4,000, "
    "beaches and family fun, avoid late nights",
    "Luxury 5 days": "5 days in Dubai, luxury hotel, budget AED 25,000, shopping, art and "
    "beaches, relaxed pace",
    "Budget culture trip": "3 days in Dubai on a budget of AED 1,800, museums, souks and local "
    "food, cheap hotel",
}


def _use_example() -> None:
    choice = st.session_state.get("example")
    if choice:
        st.session_state["request"] = EXAMPLES[choice]


def page() -> None:
    ctx = app_context()
    state = session(st.session_state)
    st.title("Ask the agents")
    st.caption(
        "Describe the trip in your own words: days, budget, who's travelling, what you like "
        "and what to avoid. Name places you must see."
    )
    st.session_state.setdefault("request", example_request().raw_text)
    st.pills(
        "Try an example",
        list(EXAMPLES),
        selection_mode="single",
        key="example",
        on_change=_use_example,
    )
    text = st.text_area("Your trip", key="request", persist_state="session", height=120)
    checks = precheck(text, night_aed=cheapest_night(ctx.providers))
    for check in checks:
        if check.level == "error":
            st.error(check.message)
        elif check.level == "warning":
            st.warning(check.message)
        else:
            st.caption(check.message)
    blocked = any(c.level == "error" for c in checks)
    if st.button("Plan my trip", type="primary", key="plan", disabled=blocked):
        handle = start_run(
            RunOptions(
                text, use_llm=ctx.use_llm, max_revisions=ctx.max_revisions, data_mode=ctx.mode
            ),
            providers=ctx.providers,
            settings=ctx.settings,
        )
        count_ai_plan(handle, state)
        state["runs"][PAGE] = handle
    run_slot(PAGE, providers=ctx.providers, settings=ctx.settings, dev=ctx.dev)
