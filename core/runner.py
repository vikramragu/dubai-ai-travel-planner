"""Run the LangGraph planner (architecture.md §6, §8).

``run(text)`` streams the compiled graph with ``stream_mode=["updates", "custom"]``:

- ``custom`` chunks are the agents' :class:`TraceEvent`s as they happen; they go to
  ``on_event`` and the optional :class:`TraceBus` (the UI's live timeline).
- ``updates`` chunks say which node finished; they become ``RunResult.steps``.

Without an API key every agent uses its deterministic fallback, so runs are reproducible and
free. The checkpointer keeps every superstep; ``RunResult.state_history()`` replays them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import StateSnapshot

from agents.skills.budget_engine import BudgetOptions
from core.config import DataMode, Settings, get_settings
from core.llm import LLMClient, make_protected_llm
from core.tracing import RunMetrics, TraceBus, TraceEvent, TraceKind
from data.providers import DataProviders, get_providers
from data.providers.runtime import data_run
from schemas import (
    IssueOrigin,
    Itinerary,
    ItinerarySnapshot,
    ReviewIssue,
    ReviewReport,
    RouteTarget,
    Severity,
    TripRequest,
)

AUTO = "auto"


@dataclass
class RunResult:
    itinerary: Itinerary
    trace: list[TraceEvent] = field(default_factory=list)
    metrics: RunMetrics = field(default_factory=RunMetrics)
    unscheduled: list[str] = field(default_factory=list)
    history: list[ItinerarySnapshot] = field(default_factory=list)  # every reviewed draft
    steps: list[str] = field(default_factory=list)  # node names in the order they finished
    stop_reason: str = "passed"
    rationale: dict[str, str] = field(default_factory=dict)  # place id -> why it was picked
    thread_id: str = ""
    graph: Any = None  # the compiled graph (with its checkpointer), for replay

    def state_history(self) -> Iterator[StateSnapshot]:
        """Checkpointed supersteps, newest first."""
        if self.graph is None:
            return iter(())
        config: RunnableConfig = {"configurable": {"thread_id": self.thread_id}}
        return iter(self.graph.get_state_history(config))


def run(
    text: str,
    *,
    providers: DataProviders | None = None,
    llm: LLMClient | str | None = AUTO,
    settings: Settings | None = None,
    run_id: str | None = None,
    on_event: Callable[[TraceEvent], None] | None = None,
    bus: TraceBus | None = None,
    max_revisions: int | None = None,
    request: TripRequest | None = None,
) -> RunResult:
    """Plan a trip from free text, or from a ready ``request`` (the trip form; ``text`` is then
    its one-line summary). ``llm="auto"`` uses the configured providers if keys are set, with
    the answer cache and the free-tier guard."""
    from graph.build import build_graph, make_checkpointer
    from graph.state import GraphContext

    settings = settings or get_settings()
    client = make_protected_llm(settings) if llm == AUTO else llm
    assert client is None or not isinstance(client, str)
    run_id = run_id or uuid.uuid4().hex[:8]
    context = GraphContext(
        providers=providers or get_providers(settings),
        llm=client,
        settings=settings,
        run_id=run_id,
        max_revisions=settings.max_revisions if max_revisions is None else max_revisions,
        budget_options=BudgetOptions(contingency_pct=settings.contingency_pct),
    )
    graph = build_graph(make_checkpointer())
    config: RunnableConfig = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}

    def publish(event: TraceEvent) -> None:
        if on_event is not None:
            on_event(event)
        if bus is not None:
            bus.publish(event)

    start = TraceEvent(
        run_id=run_id,
        agent="runner",
        kind=TraceKind.START,
        summary="Run started" + ("" if client else " (no LLM: deterministic mode)"),
    )
    publish(start)
    try:
        first = {"raw_request": text, **({"request": request} if request else {})}
        steps = _stream(graph, first, config, context, settings, publish)
        return _result(graph, config, client, start, steps, publish)
    finally:
        if bus is not None:
            bus.close()  # also on failure (e.g. a rejected request), so readers stop waiting


def _stream(
    graph: Any,
    graph_input: dict[str, Any] | None,
    config: RunnableConfig,
    context: Any,
    settings: Settings,
    publish: Callable[[TraceEvent], None],
) -> list[str]:
    """Stream the graph (``graph_input=None`` resumes a checkpointed run); returns the nodes
    in the order they finished."""
    steps: list[str] = []
    # Live data: hybrid mode caps the network calls per run; live mode doesn't.
    budget = settings.external_call_budget if settings.data_mode is DataMode.HYBRID else None
    with data_run(budget):
        for mode, chunk in graph.stream(
            graph_input, config, context=context, stream_mode=["updates", "custom"]
        ):
            if mode == "custom" and isinstance(chunk, TraceEvent):
                publish(chunk)
            elif mode == "updates" and isinstance(chunk, dict):
                steps += [node for node in chunk if not node.startswith("__")]
    return steps


# ------------------------------------------------------------------ change requests
class ChangeKind(StrEnum):
    CHEAPER = "cheaper"  # lower the target (default 15% below the current total) -> Budget
    REMOVE = "remove"  # take a stop out of the plan -> Assemble
    SWAP = "swap"  # replace a stop with another place -> Destination


@dataclass
class ChangeRequest:
    kind: ChangeKind
    place_id: str | None = None
    target_aed: float | None = None  # CHEAPER only

    def describe(self, names: dict[str, str]) -> str:
        name = names.get(self.place_id or "", self.place_id or "")
        return {
            ChangeKind.CHEAPER: f"make it cheaper (target AED {self.target_aed or 0:,.0f})",
            ChangeKind.REMOVE: f"remove {name}",
            ChangeKind.SWAP: f"swap {name} for another place",
        }[self.kind]


CHEAPER_SHARE = 0.85


def revise(
    result: RunResult,
    change: ChangeRequest,
    *,
    providers: DataProviders | None = None,
    llm: LLMClient | str | None = AUTO,
    settings: Settings | None = None,
    on_event: Callable[[TraceEvent], None] | None = None,
    bus: TraceBus | None = None,
) -> RunResult:
    """Apply a traveller's change to a finished plan (architecture.md §6.3).

    The change is written into the checkpointed state as a review issue from the ``user``
    (``update_state`` as the review node), and the graph resumes from the router: only the
    agents responsible re-run, and the revised plan goes through Review like any draft."""
    from graph.state import GraphContext

    if result.graph is None:
        raise ValueError("this plan has no saved graph state (e.g. it was loaded from a file)")
    settings = settings or get_settings()
    client = make_protected_llm(settings) if llm == AUTO else llm
    assert client is None or not isinstance(client, str)
    graph = result.graph
    source: RunnableConfig = {"configurable": {"thread_id": result.thread_id}}
    state = graph.get_state(source).values
    # Fork: the change runs on a copy of the saved state, so the original plan (and anything
    # saved from it) stays exactly as it was.
    thread_id = f"{result.thread_id.split('~')[0]}~{uuid.uuid4().hex[:6]}"
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    graph.update_state(config, dict(state), as_node="finalize")
    final: Itinerary = state["final"]
    request: TripRequest = state["request"]
    revision = int(state.get("revision", 0)) + 1
    excluded = list(state.get("user_excluded", []))
    update: dict[str, Any] = {"revision": revision, "stop_reason": "", "change_from": revision}

    if change.kind is ChangeKind.CHEAPER:
        total = final.budget.total if final.budget else request.constraints.budget_aed
        target = round(change.target_aed or total * CHEAPER_SHARE, 2)
        change = ChangeRequest(ChangeKind.CHEAPER, target_aed=target)
        note = f"You asked for a cheaper plan: target AED {target:,.0f} (was AED {total:,.0f})"
        update["request"] = request.model_copy(update={"assumptions": [*request.assumptions, note]})
        update["budget_target"] = target  # the Budget agent's goal; the budget stays the budget
        targets = [RouteTarget.BUDGET]
        issue = ReviewIssue(
            code="user_cheaper",
            severity=Severity.MAJOR,
            message=f"The traveller asked for a cheaper plan (target AED {target:,.0f})",
            route_to=RouteTarget.BUDGET,
            origin=IssueOrigin.USER,
        )
    else:
        if not change.place_id:
            raise ValueError(f"{change.kind.value} needs a place_id")
        excluded.append(change.place_id)
        update["user_excluded"] = list(dict.fromkeys(excluded))
        if change.place_id in request.constraints.must_visit:  # they changed their mind
            must = [p for p in request.constraints.must_visit if p != change.place_id]
            c = request.constraints.model_copy(update={"must_visit": must})
            update["request"] = request.model_copy(update={"constraints": c})
        swap = change.kind is ChangeKind.SWAP
        target_node = RouteTarget.DESTINATION if swap else RouteTarget.ASSEMBLE
        targets = [target_node]
        issue = ReviewIssue(
            code="user_swap" if swap else "user_remove",
            severity=Severity.MAJOR,
            message="The traveller asked to "
            + ("swap this stop for another place" if swap else "remove this stop"),
            route_to=target_node,
            origin=IssueOrigin.USER,
            place_id=change.place_id,
        )
    update["review"] = ReviewReport.from_issues(
        [issue],
        score=final.review.score if final.review else 0.0,
        summary="Change requested by the traveller",
    )
    update["revision_targets"] = targets

    def publish(event: TraceEvent) -> None:
        if on_event is not None:
            on_event(event)
        if bus is not None:
            bus.publish(event)

    data = providers or get_providers(settings)
    place = data.places.get_place(change.place_id) if change.place_id else None
    names = {place.id: place.name} if place else {}
    start = TraceEvent(
        run_id=thread_id,
        agent="router",
        kind=TraceKind.ROUTE,
        summary=f"Revision {revision}: you asked to {change.describe(names)}; sending it to "
        + ", ".join(t.value for t in targets),
        payload={"targets": [t.value for t in targets], "change": change.kind.value},
        revision=revision,
    )
    update["trace"] = [start]  # kept in the run's history, like every routing decision
    graph.update_state(config, update, as_node="review")
    publish(start)
    context = GraphContext(
        providers=data,
        llm=client,
        settings=settings,
        run_id=thread_id,
        max_revisions=revision + settings.max_revisions,  # the change gets its own rounds
        budget_options=BudgetOptions(contingency_pct=settings.contingency_pct),
    )
    try:
        steps = _stream(graph, None, config, context, settings, publish)
        return _result(graph, config, client, start, steps, publish)
    finally:
        if bus is not None:
            bus.close()


def _result(
    graph: Any,
    config: RunnableConfig,
    client: LLMClient | None,
    start: TraceEvent,
    steps: list[str],
    publish: Callable[[TraceEvent], None],
) -> RunResult:
    state = graph.get_state(config).values
    final: Itinerary = state["final"]
    history: list[ItinerarySnapshot] = state["history"]
    chosen = next(
        (i for i, s in enumerate(history) if s.revision == state["final_draft"]), len(history) - 1
    )
    first_blocking = [i for i in _issues(history[0].itinerary) if i.blocks_pass]
    remaining = {(i.code, i.place_id, i.day) for i in _issues(final) if i.blocks_pass}
    usage = client.usage if client is not None else None
    metrics = RunMetrics(
        wall_time_s=round((datetime.now(UTC) - start.ts).total_seconds(), 3),
        llm_calls=usage.calls if usage else 0,
        llm_cache_hits=int(getattr(client, "cache_hits", 0)),
        tokens_in=usage.tokens_in if usage else 0,
        tokens_out=usage.tokens_out if usage else 0,
        revisions=state.get("revision", 0),
        final_score=final.review.score if final.review else None,
        issues_found=len(first_blocking),
        issues_resolved=sum((i.code, i.place_id, i.day) not in remaining for i in first_blocking),
    )
    end = TraceEvent(
        agent="runner",
        kind=TraceKind.END,
        summary=f"Done in {metrics.wall_time_s:.1f}s: score {metrics.final_score or 0:.0f}, "
        f"{'passed' if final.review and final.review.passed else 'NOT passed'}, "
        f"{metrics.revisions} revision(s)",
        run_id=start.run_id,
        revision=metrics.revisions,
        payload=metrics.model_dump(mode="json"),
    )
    publish(end)
    return RunResult(
        itinerary=final,
        trace=[*([] if start in state["trace"] else [start]), *state["trace"], end],
        metrics=metrics,
        unscheduled=state["drafts_unscheduled"][chosen],
        history=history,
        steps=steps,
        stop_reason=state.get("stop_reason", "passed"),
        rationale=dict(state["research"].rationale) if "research" in state else {},
        thread_id=config["configurable"]["thread_id"],
        graph=graph,
    )


def _issues(it: Itinerary) -> list[Any]:
    return list(it.review.issues) if it.review else []


def main(argv: list[str]) -> int:  # pragma: no cover - CLI
    """``uv run python -m core.runner ["request text"] [--no-llm]``: plan and print a trip."""
    from agents.orchestrator import RequestError
    from agents.skills.scripted import ScriptedResult, example_request, format_itinerary

    use_llm = "--no-llm" not in argv
    args = [a for a in argv if a != "--no-llm"]
    text = args[0] if args else example_request().raw_text

    def show(e: TraceEvent) -> None:
        print(f"[rev {e.revision}] {e.agent:<12} {e.kind.value:<9} {e.summary}", flush=True)

    try:
        result = run(text, llm=AUTO if use_llm else None, on_event=show)
    except RequestError as exc:
        print(f"Can't plan this request: {exc}")
        return 2
    print()
    print(format_itinerary(ScriptedResult(result.itinerary, unscheduled=result.unscheduled)))
    print(f"\nAssumptions: {'; '.join(result.itinerary.request.assumptions) or 'none'}")
    m = result.metrics
    print(f"Graph steps: {' -> '.join(result.steps)}")
    print(
        f"Metrics: {m.wall_time_s:.2f}s, {m.llm_calls} LLM calls "
        f"({m.tokens_in:,} in / {m.tokens_out:,} out tokens), {m.revisions} revision(s), "
        f"stop: {result.stop_reason}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(main(sys.argv[1:]))
