"""Streamlit UI (architecture.md §11.2): ``uv run streamlit run app/streamlit_app.py``.

The graph runs in a worker thread (``app/state.py``); this script polls its ``TraceBus`` and
redraws the live agent timeline and graph view until the run finishes, then shows the results.
"""

from __future__ import annotations

import time

import streamlit as st

from agents.orchestrator import RequestError
from agents.skills.scripted import example_request
from app.components.budget import render_budget
from app.components.graph_view import render_graph_live, render_graph_replay
from app.components.itinerary import render_itinerary
from app.components.metrics import render_metrics
from app.components.review import render_review
from app.components.timeline import render_timeline
from app.state import RunHandle, RunOptions, start_run
from core.config import AGENT_ROLES, DataMode, Settings, get_settings
from core.llm import llm_configured, role_configured
from data.providers import DataProviders, get_providers

POLL_S = 0.25
LLM_ON = "Configured LLMs"
LLM_OFF = "No LLM (deterministic)"
DISCLAIMER = (
    "Prices and travel times are estimates from curated and open data (badges show which). "
    "Nothing is booked, and prices are not guaranteed."
)


@st.cache_resource(show_spinner="Loading the Dubai dataset…")
def providers() -> DataProviders:
    return get_providers()


def sidebar(settings: Settings) -> tuple[bool, int]:
    st.sidebar.header("Options")
    can_llm = llm_configured(settings)
    model = st.sidebar.radio(
        "Model",
        [LLM_ON, LLM_OFF],
        index=0 if can_llm else 1,
        disabled=not can_llm,  # nothing to call without a key
        help="Without an LLM every agent uses its deterministic fallback (free, reproducible).",
        key="model",
    )
    if not can_llm:
        st.sidebar.caption(
            "No API keys found. Add `GROQ_API_KEY` and `GOOGLE_API_KEY` to `.env` and restart "
            "to use the LLMs."
        )
    with st.sidebar.expander("Agent models", expanded=can_llm):
        for role in AGENT_ROLES:
            icon = "🟢" if role_configured(role, settings) else "⚪"
            provider, _, name = settings.chat_model_id(role).partition(":")
            st.markdown(f"{icon} **{role}** · {provider}")
            st.caption(f"`{name}`" + ("" if role_configured(role, settings) else " · no key"))
    max_revisions = st.sidebar.slider("Max revisions", 0, 5, settings.max_revisions, key="max_rev")
    mode = st.sidebar.selectbox("Data mode", [m.value for m in DataMode], index=0, key="data_mode")
    if mode != DataMode.LOCAL.value:
        st.sidebar.info("Live data providers arrive in Phase 7; the local dataset is used.")
    return model == LLM_ON, max_revisions


def show_live(handle: RunHandle) -> None:
    """Redraw the timeline and graph while the worker thread runs."""
    st.subheader("Agents at work")
    slot = st.empty()
    while handle.running:
        handle.pump()
        with slot.container():
            left, right = st.columns([3, 2])
            with left:
                render_timeline(handle.events)
            with right:
                render_graph_live(handle.events)
        time.sleep(POLL_S)
    handle.wait()
    st.rerun()


def show_result(handle: RunHandle) -> None:
    if handle.error is not None:
        if isinstance(handle.error, RequestError):
            st.error(f"Can't plan this request: {handle.error}")
        else:
            st.exception(handle.error)
        return
    result = handle.result
    assert result is not None
    it = result.itinerary
    if it.review is not None and it.review.passed and it.budget is not None:
        st.success(
            f"{len(it.days)}-day plan · AED {it.budget.total:,.0f} of "
            f"{it.budget.budget_aed:,.0f} · review score {it.review.score:.0f} · "
            f"{result.metrics.revisions} revision(s)"
        )
    else:
        st.warning("The plan still has unresolved issues; see Review & revisions.")
    if it.request.assumptions:
        with st.expander("Assumptions"):
            for a in it.request.assumptions:
                st.markdown(f"- {a}")
    tabs = st.tabs(
        ["Itinerary", "Budget", "Review & revisions", "Agent timeline", "Graph", "Metrics"]
    )
    with tabs[0]:
        render_itinerary(it, providers(), result.unscheduled)
    with tabs[1]:
        render_budget(it.budget, it.alternatives)
    with tabs[2]:
        render_review(result.history, result.trace, result.stop_reason)
    with tabs[3]:
        render_timeline(result.trace, finished=True)
    with tabs[4]:
        render_graph_replay(result)
    with tabs[5]:
        render_metrics(result)


def main() -> None:
    st.set_page_config(page_title="Dubai AI Travel Planner", page_icon="🧭", layout="wide")
    settings = get_settings()
    st.title("Dubai AI Travel Planner")
    st.caption(
        "Five agents on a LangGraph graph: research and logistics in parallel, then "
        "assembly, budget and review, with targeted revisions."
    )
    use_llm, max_revisions = sidebar(settings)
    text = st.text_area("Your trip", example_request().raw_text, height=110, key="request")
    if st.button("Plan my trip", type="primary", key="plan"):
        st.session_state["run"] = start_run(
            RunOptions(text, use_llm=use_llm, max_revisions=max_revisions),
            providers=providers(),
            settings=settings,
        )
    handle: RunHandle | None = st.session_state.get("run")
    if handle is not None:
        if handle.running:
            show_live(handle)
        else:
            handle.wait()
            show_result(handle)
    st.divider()
    st.caption(f"⚠️ {DISCLAIMER}")


if __name__ == "__main__":  # `streamlit run` executes the script as __main__
    main()
