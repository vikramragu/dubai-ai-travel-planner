"""Build and compile the planner graph (architecture.md §6.2).

    START -> parse -> orchestrate -> (destination || logistics) -> assemble -> budget -> review
    review -> finalize -> END
           -> orchestrate   (destination / logistics issues; only the targeted agents re-run)
           -> assemble      (schedule issues)
           -> budget        (cost issues)

``uv run python -m graph.build`` writes the Mermaid diagram to ``docs/graph.mmd``.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from enum import Enum
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer
from pydantic import BaseModel

from graph import nodes
from graph.routing import (
    AFTER_ORCHESTRATE,
    AFTER_REVIEW,
    route_after_orchestrate,
    route_after_review,
)
from graph.state import GraphContext, PlannerState

# Modules whose classes may appear in the checkpointed state.
_STATE_MODULES = (
    "schemas",
    "agents.skills.scheduler",
    "agents.skills.budget_engine",
    "core.tracing",
)

PlannerGraph = CompiledStateGraph[PlannerState, GraphContext, PlannerState, PlannerState]


def _state_types() -> list[type]:
    """Our Pydantic models, dataclasses and enums, allow-listed for checkpoint deserialisation
    (LangGraph warns on, and will later block, types it hasn't been told about)."""
    modules = []
    for name in _STATE_MODULES:
        module = importlib.import_module(name)
        modules.append(module)
        if hasattr(module, "__path__"):
            modules += [
                importlib.import_module(f"{name}.{m.name}")
                for m in pkgutil.iter_modules(module.__path__)
            ]
    out = []
    for module in modules:
        for obj in vars(module).values():
            if (
                inspect.isclass(obj)
                and obj.__module__ == module.__name__
                and (issubclass(obj, (BaseModel, Enum)) or hasattr(obj, "__dataclass_fields__"))
            ):
                out.append(obj)
    return out


def make_checkpointer() -> InMemorySaver:
    """In-memory checkpointer that stores every superstep (replay and the review diff view)."""
    return InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=_state_types()))


def build_graph(checkpointer: Checkpointer | None = None) -> PlannerGraph:
    g: StateGraph[PlannerState, GraphContext, PlannerState, PlannerState] = StateGraph(
        PlannerState, context_schema=GraphContext
    )
    g.add_node("parse", nodes.parse)
    g.add_node("orchestrate", nodes.orchestrate)
    g.add_node("destination", nodes.destination)
    g.add_node("logistics", nodes.logistics)
    g.add_node("assemble", nodes.assemble)
    g.add_node("budget", nodes.budget)
    g.add_node("review", nodes.review)
    g.add_node("finalize", nodes.finalize)

    g.add_edge(START, "parse")
    g.add_edge("parse", "orchestrate")
    g.add_conditional_edges("orchestrate", route_after_orchestrate, AFTER_ORCHESTRATE)
    # Plain edges (not a join): on a revision round only one branch may run, and assemble must
    # still follow it. When both run in the same superstep, assemble runs once after them.
    g.add_edge("destination", "assemble")
    g.add_edge("logistics", "assemble")
    g.add_edge("assemble", "budget")
    g.add_edge("budget", "review")
    g.add_conditional_edges("review", route_after_review, AFTER_REVIEW)
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def mermaid() -> str:
    return build_graph().get_graph().draw_mermaid()


def main(argv: list[str]) -> int:  # pragma: no cover - CLI
    out = Path(argv[0]) if argv else Path("docs/graph.mmd")
    out.write_text(mermaid(), encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(main(sys.argv[1:]))


__all__ = ["PlannerGraph", "build_graph", "make_checkpointer", "mermaid"]
