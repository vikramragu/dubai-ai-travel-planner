"""Live agent timeline: one card per agent with its status, latest summary and events."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import streamlit as st

from core.tracing import TraceEvent, TraceKind

AGENTS = ("orchestrator", "destination", "logistics", "budget", "review")
LABELS = {
    "orchestrator": "Orchestrator",
    "destination": "Destination research",
    "logistics": "Logistics",
    "budget": "Budget",
    "review": "Review",
}
DONE_KINDS = {TraceKind.OUTPUT, TraceKind.REVIEW, TraceKind.END}
ICONS = {"waiting": "⏳", "running": "🔄", "revising": "🔁", "done": "✅", "error": "⚠️"}


class Status(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    REVISING = "revising"
    DONE = "done"


@dataclass
class AgentCard:
    agent: str
    status: Status = Status.WAITING
    summary: str = ""
    revision: int = 0
    errors: int = 0
    events: list[TraceEvent] = field(default_factory=list)


def agent_cards(events: list[TraceEvent], *, finished: bool = False) -> dict[str, AgentCard]:
    """Each agent's status from its events. An agent is running from its ``start`` event until
    it outputs; in a revision round (revision > 0) it is "revising". Once the run is over,
    every agent that did anything is done."""
    cards = {a: AgentCard(a) for a in AGENTS}
    for e in events:
        card = cards.get(e.agent)
        if card is None:
            continue
        card.events.append(e)
        card.revision = e.revision
        if e.kind is TraceKind.ERROR:
            card.errors += 1
            continue
        if e.kind is TraceKind.START:
            card.status = Status.REVISING if e.revision else Status.RUNNING
        elif e.kind is TraceKind.REVISION:
            card.status = Status.REVISING
        elif e.kind in DONE_KINDS:
            card.status = Status.DONE
        if e.kind is not TraceKind.START or not card.summary:
            card.summary = e.summary
    if finished:
        for card in cards.values():
            if card.events:
                card.status = Status.DONE
    return cards


def _card(card: AgentCard) -> None:
    with st.container(border=True):
        badge = ICONS[card.status.value]
        rev = f" · revision {card.revision}" if card.revision else ""
        st.markdown(f"**{badge} {LABELS[card.agent]}** — {card.status.value}{rev}")
        st.caption(card.summary or "Not started")
        if card.errors:
            st.caption(f"{ICONS['error']} {card.errors} LLM fallback(s) to deterministic code")
        if card.events:
            with st.expander(f"{len(card.events)} event(s)"):
                for e in card.events:
                    st.markdown(f"`rev {e.revision}` `{e.kind.value}` {e.summary}")
                    shown = {k: v for k, v in (e.payload or {}).items() if k != "node"}
                    if shown:
                        st.json(shown, expanded=False)


def render_timeline(events: list[TraceEvent], *, finished: bool = False) -> None:
    """Orchestrator, then Destination and Logistics side by side (they run in parallel),
    then Budget and Review."""
    cards = agent_cards(events, finished=finished)
    _card(cards["orchestrator"])
    left, right = st.columns(2)
    with left:
        _card(cards["destination"])
    with right:
        _card(cards["logistics"])
    _card(cards["budget"])
    _card(cards["review"])
    routes = [e for e in events if e.agent == "router"]
    if routes:
        st.markdown("**Routing decisions**")
        for e in routes:
            st.markdown(f"- `rev {e.revision}` {e.summary}")
