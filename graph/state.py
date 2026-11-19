"""The graph's shared state and run context (architecture.md §6.1).

``PlannerState`` is the single source of truth: every node reads it and returns a partial
update. ``trace``, ``history`` and ``drafts_unscheduled`` use the ``add`` reducer, so the two
parallel branches (destination and logistics) can both append in the same superstep.

Things that are not data (providers, the LLM client, settings) travel in ``GraphContext``, the
LangGraph runtime context, so the checkpointer only stores plain, serialisable state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from operator import add
from typing import Annotated, TypedDict

from agents.skills.budget_engine import BudgetOptions, Plan
from core.config import Settings, get_settings
from core.llm import LLMClient
from core.tracing import TraceEvent
from data.providers import DataProviders
from schemas import (
    BudgetAlternative,
    BudgetBreakdown,
    Itinerary,
    ItinerarySnapshot,
    LogisticsResult,
    ResearchResult,
    ReviewReport,
    RouteTarget,
    TripRequest,
)


class PlannerState(TypedDict, total=False):
    raw_request: str
    request: TripRequest
    month: int | None  # travel month when no dates were given
    research: ResearchResult
    research_notes: list[str]  # Destination's assumptions (e.g. "limited data for nightlife")
    logistics: LogisticsResult
    plan: Plan  # scheduler + budget-engine result behind the draft (needed to revise it)
    draft: Itinerary  # the current draft, budget attached
    budget: BudgetBreakdown
    alternatives: list[BudgetAlternative]
    budget_notes: list[str]  # what the Budget agent changed and why
    review: ReviewReport
    revision: int  # 0 = first draft; n = n-th revision round
    revision_targets: list[RouteTarget]  # who must act this round; empty = finalize
    user_excluded: list[str]  # places the traveller removed or swapped out (kept out for good)
    budget_target: float | None  # "make it cheaper": the Budget agent's goal (not the budget)
    change_from: int  # revision a traveller's change started at (finalize picks from here)
    stop_reason: str  # passed | max_revisions | no_improvement
    final: Itinerary
    final_draft: int  # revision number of the draft ``finalize`` chose
    history: Annotated[list[ItinerarySnapshot], add]  # every reviewed draft (diff view)
    drafts_unscheduled: Annotated[list[list[str]], add]  # aligned with ``history``
    trace: Annotated[list[TraceEvent], add]


@dataclass
class GraphContext:
    """Per-run dependencies, passed as LangGraph's runtime context (not checkpointed)."""

    providers: DataProviders
    llm: LLMClient | None = None
    settings: Settings = field(default_factory=get_settings)
    run_id: str = "run"
    max_revisions: int = 3
    budget_options: BudgetOptions = field(default_factory=BudgetOptions)
