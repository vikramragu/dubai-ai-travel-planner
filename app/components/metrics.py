"""Run metrics panel (architecture.md §8)."""

from __future__ import annotations

import streamlit as st

from core.runner import RunResult
from core.tracing import TraceKind


def render_metrics(result: RunResult) -> None:
    m = result.metrics
    row1 = st.columns(4)
    row1[0].metric("Wall time", f"{m.wall_time_s:.1f} s")
    row1[1].metric("LLM calls", m.llm_calls)
    row1[2].metric("Tokens in / out", f"{m.tokens_in:,} / {m.tokens_out:,}")
    row1[3].metric("Revisions", m.revisions)
    row2 = st.columns(4)
    row2[0].metric("Review score", f"{m.final_score:.0f}" if m.final_score is not None else "—")
    row2[1].metric("Issues found", m.issues_found)
    row2[2].metric("Issues resolved", m.issues_resolved)
    row2[3].metric("Stopped because", result.stop_reason.replace("_", " "))
    st.caption("Graph steps: " + " → ".join(result.steps))
    render_data_activity(result)


def data_activity(result: RunResult) -> tuple[list[str], list[str]]:
    """(network calls, fallbacks) the data layer logged during the run."""
    data = [e for e in result.trace if e.agent == "data"]
    calls = [e.summary for e in data if e.kind is TraceKind.TOOL_CALL]
    fallbacks = [e.summary for e in data if e.kind is TraceKind.FALLBACK]
    return calls, fallbacks


def render_data_activity(result: RunResult) -> None:
    calls, fallbacks = data_activity(result)
    st.markdown("**Live data**")
    if not calls and not fallbacks:
        st.caption("No live calls: every value came from the curated dataset or the cache.")
        return
    cols = st.columns(2)
    cols[0].metric("Network calls", len(calls))
    cols[1].metric("Fallbacks to local data", len(fallbacks))
    if calls or fallbacks:
        with st.expander("Calls and fallbacks"):
            for line in calls:
                st.markdown(f"- 🌐 {line}")
            for line in fallbacks:
                st.markdown(f"- ↩️ {line}")
