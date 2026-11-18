"""Streamlit UI (architecture.md §11.2): ``uv run streamlit run app/streamlit_app.py``.

The graph runs in a worker thread (``app/state.py``); this script polls its ``TraceBus`` and
redraws the live agent timeline and graph view until the run finishes, then shows the results.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# `streamlit run` puts only app/ on the import path. Add the project root so `core`, `agents`
# and `app.components` import even where the project itself isn't installed (Streamlit Cloud).
ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402

from agents.orchestrator import RequestError  # noqa: E402
from agents.skills.scripted import example_request  # noqa: E402
from app.components.budget import render_budget  # noqa: E402
from app.components.graph_view import render_graph_live, render_graph_replay  # noqa: E402
from app.components.itinerary import render_itinerary  # noqa: E402
from app.components.metrics import render_metrics  # noqa: E402
from app.components.review import render_review  # noqa: E402
from app.components.timeline import render_timeline  # noqa: E402
from app.state import RunHandle, RunOptions, start_run  # noqa: E402
from core.config import AGENT_ROLES, DataMode, Settings, get_settings  # noqa: E402
from core.llm import llm_configured, role_configured  # noqa: E402
from data.providers import DataProviders, get_providers  # noqa: E402

POLL_S = 0.25
LLM_ON = "Configured LLMs"
LLM_OFF = "No LLM (deterministic)"
DISCLAIMER = (
    "Prices and travel times are estimates from curated and open data (badges show which). "
    "Nothing is booked, and prices are not guaranteed."
)


MODE_HELP = {
    DataMode.LOCAL: "Curated dataset only: offline, free and reproducible.",
    DataMode.HYBRID: "Adds live OpenStreetMap places and Open-Meteo forecasts, and routes live "
    "places with OSRM, within a per-run call budget; falls back to local data on any failure.",
    DataMode.LIVE: "Like hybrid, and routes every taxi and walking leg live with OSRM (slower "
    "the first time; answers are cached).",
}


def mode_settings(settings: Settings, mode: DataMode) -> Settings:
    return settings.model_copy(update={"data_mode": mode})


@st.cache_resource(show_spinner="Loading the Dubai dataset…")
def providers(mode: str) -> DataProviders:
    return get_providers(mode_settings(get_settings(), DataMode(mode)))


def sidebar(settings: Settings) -> tuple[bool, int, DataMode]:
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
            "No API keys found. Add `GROQ_API_KEY` and `GOOGLE_API_KEY` to `.env` when running "
            "locally, or to the app's **Secrets** on Streamlit Cloud, then restart to use the LLMs."
        )
    with st.sidebar.expander("Agent models", expanded=can_llm):
        for role in AGENT_ROLES:
            icon = "🟢" if role_configured(role, settings) else "⚪"
            provider, _, name = settings.chat_model_id(role).partition(":")
            st.markdown(f"{icon} **{role}** · {provider}")
            st.caption(f"`{name}`" + ("" if role_configured(role, settings) else " · no key"))
    max_revisions = st.sidebar.slider("Max revisions", 0, 5, settings.max_revisions, key="max_rev")
    modes = [m.value for m in DataMode]
    mode = DataMode(
        st.sidebar.selectbox(
            "Data mode", modes, index=modes.index(settings.data_mode.value), key="data_mode"
        )
    )
    st.sidebar.caption(MODE_HELP[mode])
    return model == LLM_ON, max_revisions, mode


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
        render_itinerary(it, providers(handle.options.data_mode.value), result.unscheduled)
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
    use_llm, max_revisions, mode = sidebar(settings)
    text = st.text_area("Your trip", example_request().raw_text, height=110, key="request")
    if st.button("Plan my trip", type="primary", key="plan"):
        st.session_state["run"] = start_run(
            RunOptions(text, use_llm=use_llm, max_revisions=max_revisions, data_mode=mode),
            providers=providers(mode.value),
            settings=mode_settings(settings, mode),
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
