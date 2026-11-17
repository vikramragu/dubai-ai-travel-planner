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
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import StateSnapshot

from agents.skills.budget_engine import BudgetOptions
from core.config import Settings, get_settings
from core.llm import LLMClient, make_llm
from core.tracing import RunMetrics, TraceBus, TraceEvent, TraceKind
from data.providers import DataProviders, get_providers
from schemas import Itinerary, ItinerarySnapshot

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
) -> RunResult:
    """Plan a trip from free text. ``llm="auto"`` uses the configured providers if keys are set."""
    from graph.build import build_graph, make_checkpointer
    from graph.state import GraphContext

    settings = settings or get_settings()
    client = make_llm(settings) if llm == AUTO else llm
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
    steps: list[str] = []
    try:
        for mode, chunk in graph.stream(
            {"raw_request": text},
            config,
            context=context,
            stream_mode=["updates", "custom"],
        ):
            if mode == "custom" and isinstance(chunk, TraceEvent):
                publish(chunk)
            elif mode == "updates" and isinstance(chunk, dict):
                steps += [node for node in chunk if not node.startswith("__")]
        return _result(graph, config, client, start, steps, publish)
    finally:
        if bus is not None:
            bus.close()  # also on failure (e.g. a rejected request), so readers stop waiting


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
        trace=[start, *state["trace"], end],
        metrics=metrics,
        unscheduled=state["drafts_unscheduled"][chosen],
        history=history,
        steps=steps,
        stop_reason=state.get("stop_reason", "passed"),
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
