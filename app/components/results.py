"""The shared results view for the two planning pages (architecture.md §11.2).

While planning: a progress view for travellers (and, in dev mode, the agent timeline and the
live graph). Afterwards: a summary with "Save this trip" and "Make it cheaper", the day cards
(with Remove / Swap on each stop) and the budget; in dev mode also Review & revisions, Agent
timeline, Graph and Metrics. A change request starts a revision of the plan in a worker thread
and shows its progress the same way.
"""

from __future__ import annotations

import time
from typing import Any

import streamlit as st

from agents.orchestrator import RequestError
from app.components.budget import render_budget
from app.components.graph_view import render_graph_live, render_graph_replay
from app.components.itinerary import area_names, render_itinerary
from app.components.metrics import render_metrics
from app.components.review import render_review
from app.components.timeline import render_timeline
from app.state import RunHandle, itinerary_json, make_saved, start_change
from core.config import Settings
from core.llm import llm_configured, quota_guard
from core.runner import ChangeKind, ChangeRequest
from core.tracing import TraceEvent
from data.providers import DataProviders

POLL_S = 0.25
STEPS = (
    ("parse", "Understanding your request"),
    ("destination", "Finding places for you"),
    ("logistics", "Choosing a hotel area"),
    ("assemble", "Building your days"),
    ("budget", "Checking the budget"),
    ("review", "Final review"),
)
NODE_STEP = {"orchestrate": "destination", "finalize": "review"}


# ------------------------------------------------------------------ AI allowance
def ai_status(settings: Settings, state: Any) -> tuple[bool, str]:
    """(can this visitor plan with the LLMs now?, a short explanation for the sidebar)."""
    if not llm_configured(settings):
        return False, "AI planning is off (no API keys)."
    status = quota_guard(settings).status()
    if status["paused"]:
        return False, f"AI planning paused ({status['reason']}); planning without AI."
    if not status["available"]:
        return False, "Today's AI allowance is used up; planning without AI until tomorrow."
    cap = settings.llm_plans_per_session
    used = int(state.get("ai_plans", 0))
    if cap and used >= cap:
        return False, f"You've used your {cap} AI plans for this session; planning without AI."
    left = f"{cap - used} AI plan(s) left in this session" if cap else "AI planning on"
    return True, f"{left} · {status['share']:.0%} of today's AI allowance used"


def count_ai_plan(handle: RunHandle, state: Any) -> None:
    if handle.options.use_llm:
        state["ai_plans"] = int(state.get("ai_plans", 0)) + 1


# ------------------------------------------------------------------ progress
def progress_steps(events: list[TraceEvent]) -> tuple[list[tuple[str, str]], int]:
    """(steps, index of the current step) from the run's events, for the traveller view."""
    current = 0
    for e in events:
        node = (e.payload or {}).get("node")
        if node:
            node = NODE_STEP.get(node, node)
            for i, (key, _) in enumerate(STEPS):
                if key == node:
                    current = max(current, i) if e.revision == 0 else i
    return list(STEPS), current


def render_progress(events: list[TraceEvent]) -> None:
    steps, current = progress_steps(events)
    revision = max((e.revision for e in events), default=0)
    st.markdown(
        "#### Planning your trip…"
        if not revision
        else f"#### Improving the plan (round {revision})…"
    )
    st.progress((current + 1) / len(steps))
    for i, (_, label) in enumerate(steps):
        mark = "✅" if i < current else ("🔄" if i == current else "⏳")
        st.markdown(f"{mark} {label}")


def show_live(handle: RunHandle, dev: bool) -> None:
    """Redraw progress while the worker thread runs, then rerun the page."""
    slot = st.empty()
    while handle.running:
        handle.pump()
        with slot.container():
            if dev:
                left, right = st.columns([3, 2])
                with left:
                    render_timeline(handle.events)
                with right:
                    render_graph_live(handle.events)
            else:
                render_progress(handle.events)
        time.sleep(POLL_S)
    handle.wait()
    st.rerun()


# ------------------------------------------------------------------ results
def _queue_change(page: str, kind: str, place_id: str | None = None) -> None:
    st.session_state.setdefault("pending_change", {})[page] = (kind, place_id)


def run_slot(page: str, *, providers: DataProviders, settings: Settings, dev: bool) -> None:
    """Everything below the planning form for one page: progress, result, changes."""
    runs: dict[str, RunHandle] = st.session_state["runs"]
    handle = runs.get(page)
    pending = st.session_state.get("pending_change", {}).pop(page, None)
    if pending and handle is not None and handle.result is not None and not handle.running:
        kind, place_id = pending
        change = ChangeRequest(ChangeKind(kind), place_id=place_id)
        allowed, _ = ai_status(settings, st.session_state)
        use = bool(st.session_state.get("opts", {}).get("use_llm", allowed)) and allowed
        handle = start_change(handle, change, providers=providers, settings=settings, use_llm=use)
        count_ai_plan(handle, st.session_state)
        runs[page] = handle
    if handle is None:
        return
    if handle.running:
        show_live(handle, dev)
        return
    handle.wait()
    show_result(handle, page, providers=providers, dev=dev)


def show_result(handle: RunHandle, page: str, *, providers: DataProviders, dev: bool) -> None:
    if handle.error is not None:
        if isinstance(handle.error, RequestError):
            st.error(f"Can't plan this request: {handle.error}")
        elif isinstance(handle.error, ValueError) and handle.change is not None:
            st.error(f"Couldn't apply that change: {handle.error}")
        else:
            st.exception(handle.error)
        return
    result = handle.result
    assert result is not None
    it = result.itinerary
    if handle.change is not None:
        st.info(
            f"Updated the plan: you asked to {handle.change.describe(_names(handle, providers))}."
        )
    if it.review is not None and it.review.passed and it.budget is not None:
        area = area_names(providers).get(it.hotel.neighborhood_id, it.hotel.neighborhood_id)
        st.success(
            f"**{len(it.days)}-day plan** · stay in {area} · "
            f"AED {it.budget.total:,.0f} of {it.budget.budget_aed:,.0f} · "
            f"review score {it.review.score:.0f}"
        )
    else:
        st.warning("The plan still has some unresolved issues (see the notes below).")
    actions = st.columns([1, 1, 1, 3])
    if actions[0].button("⭐ Save this trip", key=f"{page}-save"):
        st.session_state["saved"].append(make_saved(it, result))
        st.toast("Saved. Find it under Saved trips.")
    actions[1].button(
        "💸 Make it cheaper",
        key=f"{page}-cheaper",
        on_click=_queue_change,
        args=(page, "cheaper"),
        help="Ask the agents for a plan about 15% cheaper",
    )
    actions[2].download_button(
        "⬇️ Download",
        itinerary_json(it),
        file_name=f"dubai-trip-{it.run_id}.json",
        mime="application/json",
        key=f"{page}-download",
    )
    notes = [a for a in it.request.assumptions if dev or not a.startswith("No ")]
    if notes:
        with st.expander("Notes about this plan"):
            for a in notes:
                st.markdown(f"- {a}")
    names = ["Your plan", "Budget"]
    if dev:
        names += ["Review & revisions", "Agent timeline", "Graph", "Metrics"]
    tabs = st.tabs(names)
    with tabs[0]:
        render_itinerary(
            it,
            providers,
            result.unscheduled,
            rationale=result.rationale,
            on_change=lambda kind, pid: _queue_change(page, kind, pid),
            key=page,
            dev=dev,
        )
    with tabs[1]:
        render_budget(it.budget, it.alternatives)
    if dev:
        with tabs[2]:
            render_review(result.history, result.trace, result.stop_reason)
        with tabs[3]:
            render_timeline(result.trace, finished=True)
        with tabs[4]:
            render_graph_replay(result)
        with tabs[5]:
            render_metrics(result)


def _names(handle: RunHandle, providers: DataProviders) -> dict[str, str]:
    """The changed place's name (it is usually no longer in the new plan)."""
    pid = handle.change.place_id if handle.change else None
    place = providers.places.get_place(pid) if pid else None
    return {place.id: place.name} if place else {}
