"""Graph view: the compiled graph's Mermaid diagram with the active node highlighted, and a
replay of the checkpointed supersteps (the shared state after each step)."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Any

import streamlit as st

from core.runner import RunResult
from core.tracing import TraceEvent
from graph.build import mermaid

MERMAID_JS = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
GRAPH_NODES = (
    "parse",
    "orchestrate",
    "destination",
    "logistics",
    "assemble",
    "budget",
    "review",
    "finalize",
)


@st.cache_data(show_spinner=False)
def base_diagram() -> str:
    return mermaid()


def active_nodes(events: list[TraceEvent]) -> list[str]:
    """Nodes running now: the node of the latest event, plus its parallel sibling when the
    latest events of both research agents are in the same round and neither has finished."""
    if not events:
        return []
    last = (events[-1].payload or {}).get("node")
    if last in ("destination", "logistics"):
        running = []
        for node in ("destination", "logistics"):
            mine = [e for e in events if (e.payload or {}).get("node") == node]
            if mine and mine[-1].revision == events[-1].revision and mine[-1].kind.value == "start":
                running.append(node)
        return running or [last]
    return [last] if last else []


def visited_nodes(events: list[TraceEvent]) -> list[str]:
    seen = {(e.payload or {}).get("node") for e in events}
    return [n for n in GRAPH_NODES if n in seen]


def highlighted(diagram: str, active: list[str], visited: list[str] | None = None) -> str:
    """The diagram with Mermaid classes for visited (green) and active (amber) nodes."""
    visited = visited or []
    lines = [
        diagram.rstrip(),
        "\tclassDef visited fill:#bbf7d0,stroke:#15803d,color:#14532d",
        "\tclassDef active fill:#fde68a,stroke:#b45309,stroke-width:3px,color:#78350f",
    ]
    done = [n for n in visited if n not in active]
    if done:
        lines.append(f"\tclass {','.join(done)} visited")
    if active:
        lines.append(f"\tclass {','.join(active)} active")
    return "\n".join(lines) + "\n"


def render_mermaid(code: str, *, height: int = 620) -> None:
    # Our own diagram text (HTML-escaped), never user or LLM input, so an iframe is safe here.
    st.iframe(
        f"""
<style>
.mermaid:not([data-processed]) {{ visibility: hidden; }}
/* fit the whole graph in the frame, however wide the column is */
.mermaid svg {{ display: block; margin: 0 auto; height: auto; max-height: {height - 24}px;
  width: auto !important; max-width: 100% !important; }}
</style>
<div class="mermaid">{html.escape(code)}</div>
<script src="{MERMAID_JS}"></script>
<script>mermaid.initialize({{startOnLoad: true, theme: "neutral"}});</script>
""",
        height=height,
    )


# ------------------------------------------------------------------ replay
@dataclass
class Step:
    index: int
    next_nodes: list[str]
    state: dict[str, Any] = field(default_factory=dict)


def state_summary(values: dict[str, Any]) -> dict[str, Any]:
    """The shared state in a few readable fields."""
    out: dict[str, Any] = {"revision": values.get("revision", 0)}
    if "request" in values:
        c = values["request"].constraints
        out["request"] = f"{c.days} days, AED {c.budget_aed:,.0f}, {c.hotel_tier.value} hotel"
    if "research" in values:
        r = values["research"]
        out["research"] = f"{len(r.attractions)} attractions, {len(r.restaurants)} restaurants"
    if "logistics" in values:
        out["hotel_area"] = values["logistics"].recommended_hotel.neighborhood_id
    if "draft" in values:
        d = values["draft"]
        out["draft"] = f"{len(d.days)} days, {len(d.place_ids)} stops"
    if "budget" in values:
        b = values["budget"]
        out["budget"] = f"AED {b.total:,.0f} of {b.budget_aed:,.0f}" + (
            "" if b.within_budget else " (over)"
        )
    if "review" in values:
        rv = values["review"]
        out["review"] = f"score {rv.score:.0f}, {'passed' if rv.passed else 'needs revision'}"
    if values.get("revision_targets"):
        out["revision_targets"] = [t.value for t in values["revision_targets"]]
    out["drafts_in_history"] = len(values.get("history", []))
    out["trace_events"] = len(values.get("trace", []))
    return out


def replay_steps(result: RunResult) -> list[Step]:
    snapshots = list(result.state_history())[::-1]  # oldest first
    return [
        Step(i, list(s.next), state_summary(dict(s.values)))
        for i, s in enumerate(snapshots)
        if s.values or s.next
    ]


def render_graph_live(events: list[TraceEvent]) -> None:
    render_mermaid(highlighted(base_diagram(), active_nodes(events), visited_nodes(events)))


def render_graph_replay(result: RunResult) -> None:
    steps = replay_steps(result)
    if not steps:
        st.info("No checkpoints recorded.")
        return
    i = st.slider("Superstep", 0, len(steps) - 1, len(steps) - 1, key="replay_step")
    step = steps[i]
    visited = [n for s in steps[:i] for n in s.next_nodes if n in GRAPH_NODES]  # already ran
    left, right = st.columns([3, 2])
    with left:
        render_mermaid(highlighted(base_diagram(), step.next_nodes, list(dict.fromkeys(visited))))
    with right:
        nxt = ", ".join(step.next_nodes) or "nothing (run finished)"
        st.markdown(f"**Checkpoint {i}** — next: `{nxt}`")
        st.caption("Shared state (PlannerState) saved by the checkpointer at this step")
        st.code(json.dumps(step.state, indent=1, ensure_ascii=False), language="json")
