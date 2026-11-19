"""What every page needs for this visit: settings for the chosen data mode, the providers,
whether dev mode is on, and whether this visitor plans with the LLMs right now."""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from core.config import DataMode, Settings, get_settings
from data.providers import DataProviders, get_providers


@dataclass
class AppContext:
    settings: Settings  # for the chosen data mode
    providers: DataProviders
    dev: bool
    use_llm: bool
    max_revisions: int
    mode: DataMode


@st.cache_resource(show_spinner="Loading the Dubai dataset…")
def providers_for(mode: str) -> DataProviders:
    return get_providers(get_settings().model_copy(update={"data_mode": DataMode(mode)}))


def app_context() -> AppContext:
    """Read the sidebar's choices (``streamlit_app.sidebar`` stores them in the session)."""
    base = get_settings()
    opts = st.session_state.get("opts", {})
    mode = DataMode(opts.get("mode", base.data_mode.value))
    return AppContext(
        settings=base.model_copy(update={"data_mode": mode}),
        providers=providers_for(mode.value),
        dev=bool(st.session_state.get("dev", False)),
        use_llm=bool(opts.get("use_llm", False)),
        max_revisions=int(opts.get("max_revisions", base.max_revisions)),
        mode=mode,
    )
