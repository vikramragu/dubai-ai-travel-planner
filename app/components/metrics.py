"""Run metrics panel (architecture.md §8)."""

from __future__ import annotations

import streamlit as st

from core.runner import RunResult


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
