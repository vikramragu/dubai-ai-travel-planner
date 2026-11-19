"""Graph nodes: thin wrappers that read the state, call one agent and return an update.

Each node builds its own :class:`AgentContext`, so parallel nodes never share an event list.
Every trace event goes to the LangGraph stream writer as it happens (``stream_mode="custom"``)
and into ``state["trace"]`` when the node returns.
"""

from __future__ import annotations

import itertools
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from agents.base import AgentContext
from agents.budget import BudgetAgent, BudgetRevision
from agents.destination import DestinationAgent
from agents.logistics import LogisticsAgent
from agents.orchestrator import Orchestrator
from agents.review import ReviewAgent
from agents.skills.budget_engine import Plan
from core.tracing import TraceKind
from data.providers.runtime import set_node_sink
from graph.state import GraphContext, PlannerState
from schemas import (
    BudgetAlternative,
    BudgetBreakdown,
    Itinerary,
    ItinerarySnapshot,
    ReviewIssue,
    ReviewReport,
    RouteTarget,
    TripRequest,
)

Update = dict[str, Any]
ROUTER = "router"


def _ctx(state: PlannerState, runtime: Runtime[GraphContext], node: str) -> AgentContext:
    c = runtime.context
    writer = get_stream_writer()
    ctx = AgentContext(
        providers=c.providers,
        llm=c.llm,
        settings=c.settings,
        run_id=c.run_id,
        revision=state.get("revision", 0),
        events=[],
        on_event=writer,
        node=node,
    )

    # Network calls and fallbacks in the data layer go to this node's trace (data.providers).
    def sink(kind: TraceKind, summary: str, payload: dict[str, Any] | None) -> None:
        ctx.emit("data", kind, summary, payload)

    set_node_sink(sink)
    return ctx


def _blocking(state: PlannerState) -> list[ReviewIssue]:
    review = state.get("review")
    return [i for i in review.issues if i.blocks_pass] if review else []


def _for(state: PlannerState, target: RouteTarget) -> list[ReviewIssue]:
    return [i for i in _blocking(state) if i.route_to is target]


def _assumptions(state: PlannerState) -> list[str]:
    plan = state.get("plan")
    return [
        *state["request"].assumptions,
        *state.get("research_notes", []),
        *(plan.schedule.notes if plan is not None else []),  # e.g. a must-visit that didn't fit
        *state.get("budget_notes", []),
    ]


# ------------------------------------------------------------------ nodes
def parse(state: PlannerState, runtime: Runtime[GraphContext]) -> Update:
    ctx = _ctx(state, runtime, "parse")
    given = state.get("request")
    if given is not None:  # the trip form: exact values, no parsing (and no LLM call)
        c = given.constraints
        ctx.emit(
            "orchestrator",
            TraceKind.OUTPUT,
            f"Request from the trip form: {c.days} days, AED {c.budget_aed:,.0f}, "
            f"{c.travelers} traveller(s)",
            {"constraints": c.model_dump(mode="json"), "parser": "form"},
        )
        return {
            "request": given,
            "month": None,
            "revision": 0,
            "revision_targets": [],
            "trace": ctx.events,
        }
    parsed = Orchestrator(ctx, runtime.context.budget_options).parse(state["raw_request"])
    return {
        "request": parsed.request,
        "month": parsed.month,
        "revision": 0,
        "revision_targets": [],
        "trace": ctx.events,
    }


def orchestrate(state: PlannerState, runtime: Runtime[GraphContext]) -> Update:
    """Decide which research agents run this round (both first, then only the targeted)."""
    ctx = _ctx(state, runtime, "orchestrate")
    agents = research_targets(state)
    ctx.emit(
        "orchestrator",
        TraceKind.ROUTE,
        ("Round 1" if not state.get("revision") else f"Revision {state['revision']}")
        + f": running {' and '.join(agents)}",
        {"agents": agents},
    )
    return {"trace": ctx.events}


def destination(state: PlannerState, runtime: Runtime[GraphContext]) -> Update:
    ctx = _ctx(state, runtime, "destination")
    found = DestinationAgent(ctx).research(
        state["request"],
        month=state.get("month"),
        issues=_for(state, RouteTarget.DESTINATION),
        exclude_ids=frozenset(state.get("user_excluded", [])),
    )
    return {"research": found.research, "research_notes": found.assumptions, "trace": ctx.events}


def logistics(state: PlannerState, runtime: Runtime[GraphContext]) -> Update:
    ctx = _ctx(state, runtime, "logistics")
    previous = state.get("logistics")
    result = LogisticsAgent(ctx).plan(
        state["request"],
        month=state.get("month"),
        issues=_for(state, RouteTarget.LOGISTICS),
        current_area=previous.recommended_hotel.neighborhood_id if previous else None,
    )
    return {"logistics": result, "trace": ctx.events}


def assemble(state: PlannerState, runtime: Runtime[GraphContext]) -> Update:
    ctx = _ctx(state, runtime, "assemble")
    issues = _blocking(state)
    exclude: frozenset[str] = frozenset(state.get("user_excluded", []))
    if RouteTarget.ASSEMBLE in state.get("revision_targets", []):
        exclude |= frozenset(i.place_id for i in issues if i.place_id) | detours(
            state["draft"], _for(state, RouteTarget.ASSEMBLE)
        )
    plan = Orchestrator(ctx, runtime.context.budget_options).assemble(
        state["request"],
        state["research"],
        state["logistics"],
        month=state.get("month"),
        exclude_ids=exclude,
        issues=issues,
    )
    return {"plan": plan, "draft": plan.itinerary, "trace": ctx.events}


def budget(state: PlannerState, runtime: Runtime[GraphContext]) -> Update:
    """Cost the draft. On a revision round an over-budget plan is also revised."""
    ctx = _ctx(state, runtime, "budget")
    options = runtime.context.budget_options
    agent = BudgetAgent(ctx, options)
    plan, request = state["plan"], state["request"]
    evaluation = agent.evaluate(plan, request)
    alternatives: list[BudgetAlternative] = list(evaluation.alternatives)
    notes = list(state.get("budget_notes", []))
    target = state.get("budget_target")
    update: Update = {}
    if target is not None and plan.budget.total > target:
        # "Make it cheaper": cut towards the traveller's target, best effort. The trip's real
        # budget stays the budget, so a plan that gets close but not all the way still passes.
        issues = _for(state, RouteTarget.BUDGET) or _blocking(state)
        revision = _cut_to(agent, plan, request, target, issues)
        update["budget_target"] = None  # one-shot
        got = revision.plan.budget.total
        notes.append(
            f"Cut the plan to AED {got:,.0f} (you asked for about AED {target:,.0f})"
            if got > target + 1
            else f"Cut the plan to AED {got:,.0f} (target AED {target:,.0f})"
        )
    elif not plan.budget.within_budget and state.get("revision", 0) > 0:
        issues = _for(state, RouteTarget.BUDGET) or _blocking(state)
        revision = agent.revise(plan, request, issues)
    else:
        revision = None
    if revision is not None:
        plan = revision.plan
        applied = {a.id for a in revision.applied}
        known = {a.id for a in alternatives}
        alternatives = [a.model_copy(update={"applied": a.id in applied}) for a in alternatives] + [
            a for a in revision.applied if a.id not in known
        ]
        notes += revision.notes
    return {
        **update,
        "plan": plan,
        "draft": plan.itinerary,
        "budget": plan.budget,
        "alternatives": alternatives,
        "budget_notes": list(dict.fromkeys(notes)),
        "trace": ctx.events,
    }


def _cut_to(
    agent: BudgetAgent,
    plan: Plan,
    request: TripRequest,
    target: float,
    issues: list[ReviewIssue],
) -> BudgetRevision:
    """Run the Budget agent with ``target`` as the budget, then put the trip's real budget
    (and request) back on the result."""
    c = request.constraints.model_copy(update={"budget_aed": target})
    aim = request.model_copy(update={"constraints": c})
    start = Plan(plan.inputs, plan.schedule, BudgetBreakdown.from_lines(plan.budget.lines, target))
    revision = agent.revise(start, aim, issues)
    done = revision.plan
    schedule = done.schedule
    schedule.itinerary = schedule.itinerary.model_copy(update={"request": request})
    real = BudgetBreakdown.from_lines(done.budget.lines, request.constraints.budget_aed)
    revision.plan = Plan(done.inputs, schedule, real)
    # "still over budget" would refer to the target, not the trip's budget: the caller says
    # how close the cut got instead
    revision.notes = [n for n in revision.notes if "over budget" not in n]
    return revision


def review(state: PlannerState, runtime: Runtime[GraphContext]) -> Update:
    """Review the draft, store it in ``history`` and decide whether to revise."""
    ctx = _ctx(state, runtime, "review")
    revision = state.get("revision", 0)
    draft = with_outcome(state["draft"], _assumptions(state), state.get("alternatives", []))
    report = ReviewAgent(ctx).review(
        draft.model_copy(update={"revisions": revision}),
        month=state.get("month"),
    )
    reviewed = with_outcome(draft, [], [], report, revision)
    snapshot = ItinerarySnapshot(
        revision=revision, itinerary=reviewed, note=f"score {report.score:.0f}: {report.summary}"
    )
    history = [*state.get("history", []), snapshot]
    targets, reason = decide(report, history, revision, runtime.context.max_revisions)
    update: Update = {
        "review": report,
        "history": [snapshot],
        "drafts_unscheduled": [list(state["plan"].schedule.unscheduled)],
        "revision_targets": targets,
        "stop_reason": reason,
    }
    if targets:
        update["revision"] = revision + 1
        ctx.revision = revision + 1
        ctx.emit(
            ROUTER,
            TraceKind.ROUTE,
            f"Revision {revision + 1}: sending issues to "
            + ", ".join(sorted(t.value for t in targets)),
            {"targets": [t.value for t in targets]},
        )
    else:
        ctx.emit(ROUTER, TraceKind.ROUTE, f"Finalize ({reason.replace('_', ' ')})")
    update["trace"] = ctx.events
    return update


def finalize(state: PlannerState, runtime: Runtime[GraphContext]) -> Update:
    """The reviewed draft, or, when the loop was capped, the best-scoring draft in history."""
    ctx = _ctx(state, runtime, "finalize")
    history = state["history"]
    reason = state.get("stop_reason", "passed")
    # After a traveller's change, only the drafts made for it count: the plan from before the
    # change is never handed back, even if it scored higher.
    since = state.get("change_from", 0)
    pool = [i for i in range(len(history)) if history[i].revision >= since] or [len(history) - 1]
    if reason == "passed":
        chosen = len(history) - 1
    else:  # best score; the later draft wins a tie
        chosen = max(pool, key=lambda i: (_score(history[i].itinerary), i))
    final = history[chosen].itinerary.model_copy(update={"revisions": state.get("revision", 0)})
    if reason != "passed":
        open_issues = [
            i.code for i in (final.review.issues if final.review else []) if i.blocks_pass
        ]
        note = (
            f"Stopped revising ({reason.replace('_', ' ')}); showing draft "
            f"{history[chosen].revision} with unresolved: {', '.join(open_issues) or 'none'}"
        )
        request = final.request.model_copy(
            update={"assumptions": [*final.request.assumptions, note]}
        )
        final = final.model_copy(update={"request": request})
    # one LLM call for the day themes, on the plan the traveller gets (not on every draft)
    final = Orchestrator(ctx, runtime.context.budget_options).theme_days(final)
    ctx.emit(
        "orchestrator",
        TraceKind.END,
        f"Final itinerary: draft {history[chosen].revision}, score {_score(final):.0f}",
        {"draft": history[chosen].revision, "stop_reason": reason},
    )
    return {"final": final, "final_draft": history[chosen].revision, "trace": ctx.events}


# ------------------------------------------------------------------ decisions (pure)
def research_targets(state: PlannerState) -> list[str]:
    """Agents ``orchestrate`` fans out to: both on the first round, then only those targeted."""
    if not state.get("revision"):
        return [RouteTarget.DESTINATION.value, RouteTarget.LOGISTICS.value]
    targets = state.get("revision_targets", [])
    return [t.value for t in (RouteTarget.DESTINATION, RouteTarget.LOGISTICS) if t in targets]


def issue_keys(report: ReviewReport | None) -> frozenset[tuple[str, str | None, int | None]]:
    if report is None:
        return frozenset()
    return frozenset((i.code, i.place_id, i.day) for i in report.issues if i.blocks_pass)


def decide(
    report: ReviewReport,
    history: list[ItinerarySnapshot],
    revision: int,
    max_revisions: int,
) -> tuple[list[RouteTarget], str]:
    """Who must act next (empty: finalize) and why the loop stops."""
    if report.passed:
        return [], "passed"
    if revision >= max_revisions:
        return [], "max_revisions"
    if len(history) >= 2 and issue_keys(history[-2].itinerary.review) == issue_keys(report):
        return [], "no_improvement"  # the same issues two rounds in a row
    order = list(RouteTarget)
    return sorted(report.revision_targets, key=order.index), ""


def _score(it: Itinerary) -> float:
    return it.review.score if it.review else 0.0


def detours(itinerary: Itinerary, issues: list[ReviewIssue]) -> frozenset[str]:
    """For each day with too much travel, the stop reached by its longest leg (dropping it
    shortens the day the most)."""
    heavy = {i.day for i in issues if i.code == "day_transit_heavy" and i.day}
    out = set()
    for day in itinerary.days:
        if day.day not in heavy:
            continue
        worst: tuple[int, str] | None = None
        for leg_slot, nxt in itertools.pairwise(day.slots):
            if leg_slot.leg is None or nxt.kind.value != "activity" or nxt.place_id is None:
                continue
            candidate = (leg_slot.leg.duration_min.value, nxt.place_id)
            if worst is None or candidate > worst:
                worst = candidate
        if worst is not None:
            out.add(worst[1])
    return frozenset(out)


def with_outcome(
    itinerary: Itinerary,
    assumptions: list[str],
    alternatives: list[BudgetAlternative],
    review: ReviewReport | None = None,
    revisions: int | None = None,
) -> Itinerary:
    """The draft with the run's assumptions, alternatives and (optionally) its review."""
    update: dict[str, Any] = {"review": review}
    if assumptions:
        request = itinerary.request.model_copy(
            update={"assumptions": list(dict.fromkeys(assumptions))}
        )
        update["request"] = request
    if alternatives:
        update["alternatives"] = alternatives
    if revisions is not None:
        update["revisions"] = revisions
    return itinerary.model_copy(update=update)
