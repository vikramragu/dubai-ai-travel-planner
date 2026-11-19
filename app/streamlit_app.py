"""Streamlit UI (architecture.md §11.2): ``uv run streamlit run app/streamlit_app.py``.

"Plan first, details when you need them": travellers see the planning pages; a **Dev mode**
switch adds the technical views (settings, agent timeline, graph, metrics, travel data). The
background is a day/night Dubai skyline that follows Streamlit's own light/dark theme.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run` puts only app/ on the import path. Add the project root so `core`, `agents`
# and `app.components` import even where the project itself isn't installed (Streamlit Cloud).
ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402

from app.components.results import ai_status  # noqa: E402
from app.components.skyline import render_skyline  # noqa: E402
from app.pages import ask_agents, browse, plan_trip, saved, travel_data  # noqa: E402
from app.state import session  # noqa: E402
from core.config import AGENT_ROLES, DataMode, Settings, get_settings  # noqa: E402
from core.llm import llm_configured, role_configured  # noqa: E402

TITLE = "Dubai AI Travel Planner"
TAGLINE = "Plan first. Review details when you need them."
DISCLAIMER = (
    "Prices and travel times are estimates from curated and open data (badges show which). "
    "Nothing is booked, and prices are not guaranteed."
)
LLM_ON = "Configured LLMs"
LLM_OFF = "No LLM (deterministic)"
MODE_HELP = {
    DataMode.LOCAL: "Curated dataset only: offline, free and reproducible.",
    DataMode.HYBRID: "Adds live OpenStreetMap places and Open-Meteo forecasts, and routes live "
    "places with OSRM, within a per-run call budget; falls back to local data on any failure.",
    DataMode.LIVE: "Like hybrid, and routes every taxi and walking leg live with OSRM (slower "
    "the first time; answers are cached).",
}


def sidebar(settings: Settings) -> None:
    """The app name, the Dev mode switch, the AI status and (dev) the settings. The choices go
    to ``st.session_state["opts"]`` for the pages (``app.context.app_context``)."""
    st.sidebar.markdown(f"### 🧭 {TITLE}")
    st.sidebar.caption(TAGLINE)
    # The switch's own state can be dropped on some reruns, so the app keeps "dev" itself and
    # re-seeds the switch from it on every run.
    st.session_state.setdefault("dev", False)
    st.session_state["_dev"] = st.session_state["dev"]

    def _flip() -> None:
        st.session_state["dev"] = st.session_state["_dev"]

    dev = st.sidebar.toggle(
        "Dev mode", key="_dev", on_change=_flip, help="Show settings, agents, graph and metrics"
    )
    allowed, why = ai_status(settings, st.session_state)
    opts = {
        "use_llm": allowed,
        "max_revisions": settings.max_revisions,
        "mode": settings.data_mode.value,
    }
    if not dev:
        st.sidebar.caption(("✨ " if allowed else "⚙️ ") + why)
    else:
        st.sidebar.divider()
        can_llm = llm_configured(settings)
        model = st.sidebar.radio(
            "Model",
            [LLM_ON, LLM_OFF],
            index=0 if allowed else 1,
            disabled=not can_llm,  # nothing to call without a key
            help="Without an LLM every agent uses its deterministic fallback (free, reproducible).",
            key="model",
            persist_state="session",
        )
        st.sidebar.caption(why)
        if not can_llm:
            st.sidebar.caption(
                "No API keys found. Add `GROQ_API_KEY` and `GOOGLE_API_KEY` to `.env` when "
                "running locally, or to the app's **Secrets** on Streamlit Cloud, then restart."
            )
        with st.sidebar.expander("Agent models", expanded=False):
            for role in AGENT_ROLES:
                icon = "🟢" if role_configured(role, settings) else "⚪"
                provider, _, name = settings.chat_model_id(role).partition(":")
                st.markdown(f"{icon} **{role}** · {provider}")
                st.caption(f"`{name}`" + ("" if role_configured(role, settings) else " · no key"))
        max_revisions = st.sidebar.slider(
            "Max revisions", 0, 5, settings.max_revisions, key="max_rev", persist_state="session"
        )
        modes = [m.value for m in DataMode]
        mode = DataMode(
            st.sidebar.selectbox(
                "Data mode",
                modes,
                index=modes.index(settings.data_mode.value),
                key="data_mode",
                persist_state="session",
            )
        )
        st.sidebar.caption(MODE_HELP[mode])
        opts = {
            "use_llm": model == LLM_ON and allowed,
            "max_revisions": max_revisions,
            "mode": mode.value,
        }
    st.session_state["opts"] = opts


def main() -> None:
    st.set_page_config(page_title=TITLE, page_icon="🧭", layout="wide")
    render_skyline()
    settings = get_settings()
    session(st.session_state)
    sidebar(settings)
    pages = [
        st.Page(plan_trip.page, title="Plan a trip", icon="🗺️", url_path="plan", default=True),
        st.Page(ask_agents.page, title="Ask the agents", icon="💬", url_path="ask"),
        st.Page(browse.page, title="Browse Dubai", icon="🧭", url_path="browse"),
        st.Page(saved.page, title="Saved trips", icon="⭐", url_path="saved"),
    ]
    if st.session_state.get("dev"):
        pages.append(st.Page(travel_data.page, title="Travel data", icon="🛠️", url_path="data"))
    st.navigation(pages).run()
    st.divider()
    st.caption(f"⚠️ {DISCLAIMER}")


if __name__ == "__main__":  # `streamlit run` executes the script as __main__
    main()
