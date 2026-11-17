"""Conditional edges (architecture.md §6.3).

The review node has already decided *whether* to revise (``revision_targets`` empty means
finalize: passed, ``MAX_REVISIONS`` reached, or the same issues twice in a row). These
functions only map that decision onto graph nodes, so they are pure and easy to test.
"""

from __future__ import annotations

from graph.nodes import research_targets
from graph.state import PlannerState
from schemas import RouteTarget

FINALIZE = "finalize"
ORCHESTRATE = "orchestrate"
ASSEMBLE = "assemble"
BUDGET = "budget"
DESTINATION = "destination"
LOGISTICS = "logistics"

AFTER_REVIEW = [FINALIZE, ORCHESTRATE, ASSEMBLE, BUDGET]
AFTER_ORCHESTRATE = [DESTINATION, LOGISTICS]


def route_after_review(state: PlannerState) -> str:
    """finalize / budget (cost issues) / destination+logistics via orchestrate (content and
    hotel issues) / assemble (schedule issues). The most upstream target wins, because
    everything downstream of it re-runs anyway."""
    targets = set(state.get("revision_targets", []))
    if not targets:
        return FINALIZE
    if targets & {RouteTarget.DESTINATION, RouteTarget.LOGISTICS}:
        return ORCHESTRATE
    if RouteTarget.ASSEMBLE in targets:
        return ASSEMBLE
    return BUDGET


def route_after_orchestrate(state: PlannerState) -> list[str]:
    """Fan out: both research agents on the first round, only the targeted ones afterwards."""
    return research_targets(state)
