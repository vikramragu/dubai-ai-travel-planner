"""Phase 5 exit criteria: the LangGraph planner (architecture.md §6-7)."""

from __future__ import annotations

import logging

import pytest

from core.config import Settings
from core.runner import RunResult, run
from core.tracing import TraceBus, TraceKind
from data.providers import DataProviders
from graph.build import build_graph, mermaid
from graph.nodes import decide, research_targets
from graph.routing import route_after_orchestrate, route_after_review
from graph.state import PlannerState
from schemas import ItinerarySnapshot, ReviewIssue, ReviewReport, RouteTarget, Severity

OFFLINE = Settings(_env_file=None)
EXAMPLE = (
    "Plan a 4-day trip to Dubai with a budget of AED 5,000, excluding flights. "
    "I enjoy cultural attractions, local food, and beaches. "
    "I prefer affordable hotels and want to avoid crowded places."
)
OVER_BUDGET = "4 days in Dubai, mid-range hotel, budget AED 3000, culture and beaches, avoid crowds"
IMPOSSIBLE = "4 days in Dubai, budget AED 300, culture"


def _issue(
    target: RouteTarget, code: str = "x", severity: Severity = Severity.MAJOR
) -> ReviewIssue:
    return ReviewIssue(code=code, severity=severity, message=code, route_to=target)


def _report(*issues: ReviewIssue) -> ReviewReport:
    return ReviewReport.from_issues(list(issues), score=50, summary="s")


@pytest.fixture(scope="module")
def over_budget(providers: DataProviders) -> RunResult:
    return run(OVER_BUDGET, providers=providers, llm=None, settings=OFFLINE)


@pytest.fixture(scope="module")
def example(providers: DataProviders) -> RunResult:
    return run(EXAMPLE, providers=providers, llm=None, settings=OFFLINE)


# ------------------------------------------------------------------ structure
def test_graph_has_every_node_and_the_loop() -> None:
    nodes = set(build_graph().get_graph().nodes)
    assert {
        "parse",
        "orchestrate",
        "destination",
        "logistics",
        "assemble",
        "budget",
        "review",
        "finalize",
    } <= nodes
    diagram = mermaid()
    assert "review" in diagram and "finalize" in diagram and "orchestrate" in diagram


def test_destination_and_logistics_share_a_superstep(example: RunResult) -> None:
    pending = [tuple(sorted(s.next)) for s in example.state_history()]
    assert ("destination", "logistics") in pending


def test_example_request_passes_first_time(example: RunResult) -> None:
    it = example.itinerary
    assert it.is_final and it.review is not None and it.review.passed
    assert example.steps[:2] == ["parse", "orchestrate"]
    assert set(example.steps[2:4]) == {"destination", "logistics"}
    assert example.steps[4:] == ["assemble", "budget", "review", "finalize"]
    assert example.stop_reason == "passed" and len(example.history) == 1


# ------------------------------------------------------------------ routing
@pytest.mark.parametrize(
    ("targets", "node", "fan_out"),
    [
        ({RouteTarget.DESTINATION}, "orchestrate", ["destination"]),
        ({RouteTarget.LOGISTICS}, "orchestrate", ["logistics"]),
        (
            {RouteTarget.DESTINATION, RouteTarget.LOGISTICS},
            "orchestrate",
            ["destination", "logistics"],
        ),
        ({RouteTarget.LOGISTICS, RouteTarget.BUDGET}, "orchestrate", ["logistics"]),
        ({RouteTarget.ASSEMBLE}, "assemble", []),
        ({RouteTarget.ASSEMBLE, RouteTarget.BUDGET}, "assemble", []),
        ({RouteTarget.BUDGET}, "budget", []),
    ],
)
def test_each_issue_type_routes_to_its_node(
    targets: set[RouteTarget], node: str, fan_out: list[str]
) -> None:
    report = _report(*(_issue(t) for t in targets))
    chosen, reason = decide(report, [], revision=0, max_revisions=3)
    assert set(chosen) == targets and reason == ""
    state: PlannerState = {"revision": 1, "revision_targets": chosen}
    assert route_after_review(state) == node
    if node == "orchestrate":
        assert route_after_orchestrate(state) == fan_out


def test_first_round_runs_both_research_agents() -> None:
    assert research_targets({"revision": 0}) == ["destination", "logistics"]


def test_minor_issues_do_not_revise() -> None:
    report = _report(_issue(RouteTarget.ASSEMBLE, severity=Severity.MINOR))
    assert decide(report, [], revision=0, max_revisions=3) == ([], "passed")
    assert route_after_review({"revision_targets": []}) == "finalize"


def test_loop_stops_at_max_revisions() -> None:
    report = _report(_issue(RouteTarget.BUDGET, "over_budget", Severity.BLOCKER))
    assert decide(report, [], revision=3, max_revisions=3) == ([], "max_revisions")
    assert decide(report, [], revision=2, max_revisions=3)[0] == [RouteTarget.BUDGET]


def test_loop_stops_when_nothing_improves(example: RunResult) -> None:
    it = example.itinerary
    first = _report(_issue(RouteTarget.BUDGET, "over_budget", Severity.BLOCKER))
    other = _report(_issue(RouteTarget.ASSEMBLE, "day_overloaded", Severity.BLOCKER))
    previous = ItinerarySnapshot(revision=0, itinerary=it.model_copy(update={"review": first}))
    current = ItinerarySnapshot(revision=1, itinerary=it)
    assert decide(first, [previous, current], 1, 3) == ([], "no_improvement")
    assert decide(other, [previous, current], 1, 3)[1] == ""


# ------------------------------------------------------------------ revision loop
def test_over_budget_is_detected_and_revised(over_budget: RunResult) -> None:
    """Success criterion: a constraint violation is detected and revised."""
    first = over_budget.history[0].itinerary
    assert first.budget is not None and not first.budget.within_budget
    assert first.review is not None and "over_budget" in {i.code for i in first.review.issues}
    final = over_budget.itinerary
    assert final.budget is not None and final.budget.within_budget
    assert final.review is not None and final.review.passed
    assert over_budget.metrics.revisions == 1 == final.revisions
    assert over_budget.metrics.issues_resolved == over_budget.metrics.issues_found >= 1


def test_only_targeted_nodes_rerun(over_budget: RunResult) -> None:
    second_round = over_budget.steps[over_budget.steps.index("review") + 1 :]
    # over_budget -> budget, hotel_cost_driver -> logistics; destination is not re-run
    assert second_round == ["orchestrate", "logistics", "assemble", "budget", "review", "finalize"]
    revision_events = [e for e in over_budget.trace if e.revision == 1]
    assert "destination" not in {e.agent for e in revision_events}


def test_checkpoints_hold_every_draft(over_budget: RunResult) -> None:
    assert [s.revision for s in over_budget.history] == [0, 1]
    snapshots = list(over_budget.state_history())
    lengths = {len(s.values.get("history", [])) for s in snapshots}
    assert {0, 1, 2} <= lengths
    # time travel: the first, over-budget draft is still in an earlier checkpoint
    drafts = [s.values["draft"] for s in snapshots if "draft" in s.values]
    assert any(d.budget is not None and not d.budget.within_budget for d in drafts)
    assert any(d.budget is not None and d.budget.within_budget for d in drafts)


@pytest.mark.parametrize(("max_revisions", "reason"), [(1, "max_revisions"), (5, "no_improvement")])
def test_impossible_budget_terminates(
    providers: DataProviders, max_revisions: int, reason: str
) -> None:
    result = run(
        IMPOSSIBLE, providers=providers, llm=None, settings=OFFLINE, max_revisions=max_revisions
    )
    assert result.stop_reason == reason
    assert result.metrics.revisions <= max_revisions
    it = result.itinerary
    assert it.review is not None and not it.review.passed
    assert "Stopped revising" in it.request.assumptions[-1]
    assert it.review.score == max(
        s.itinerary.review.score for s in result.history if s.itinerary.review
    )


# ------------------------------------------------------------------ streaming
def test_events_stream_live_and_reach_the_bus(providers: DataProviders) -> None:
    bus = TraceBus()
    live: list[str] = []
    result = run(
        EXAMPLE,
        providers=providers,
        llm=None,
        settings=OFFLINE,
        bus=bus,
        on_event=lambda e: live.append(e.agent),
    )
    assert bus.closed
    from_bus = list(bus)
    assert len(from_bus) == len(live) == len(result.trace)
    assert live[0] == "runner" and result.trace[-1].kind is TraceKind.END
    assert {"destination", "logistics"} <= set(live)


def test_bus_is_closed_when_the_request_is_rejected(providers: DataProviders) -> None:
    from agents.orchestrator import RequestError

    bus = TraceBus()
    with pytest.raises(RequestError):
        run("3 days in Paris, EUR 2000", providers=providers, llm=None, settings=OFFLINE, bus=bus)
    assert bus.closed
    events = list(bus)  # returns instead of blocking forever
    assert events[0].agent == "runner" and events[-1].agent == "orchestrator"


def test_checkpoints_deserialise_only_allow_listed_types(
    providers: DataProviders, caplog: pytest.LogCaptureFixture
) -> None:
    result = run(OVER_BUDGET, providers=providers, llm=None, settings=OFFLINE)
    with caplog.at_level(logging.WARNING):
        snapshots = list(result.state_history())  # deserialises every checkpoint
    assert snapshots[0].values["final"] == result.itinerary
    assert not [r for r in caplog.records if "unregistered type" in r.getMessage()]
