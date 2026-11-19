"""Phase 8: traveller change requests resume the graph and re-run only the agents responsible."""

from __future__ import annotations

import pytest

from core.config import Settings
from core.runner import ChangeKind, ChangeRequest, RunResult, revise, run
from data.providers import DataProviders

OFFLINE = Settings(_env_file=None)
REQUEST = "4 days in Dubai, AED 5000, culture and beaches"


@pytest.fixture(scope="module")
def plan(providers: DataProviders) -> RunResult:
    return run(REQUEST, providers=providers, llm=None, settings=OFFLINE)


def _first_activity(result: RunResult, day: int = 0) -> str:
    slots = result.itinerary.days[day].slots
    return next(s.place_id for s in slots if s.kind.value == "activity" and s.place_id)


def _change(plan: RunResult, change: ChangeRequest, providers: DataProviders) -> RunResult:
    return revise(plan, change, providers=providers, llm=None, settings=OFFLINE)


def test_remove_re_runs_assembly_only(plan: RunResult, providers: DataProviders) -> None:
    victim = _first_activity(plan)
    after = _change(plan, ChangeRequest(ChangeKind.REMOVE, place_id=victim), providers)
    assert victim not in after.itinerary.place_ids
    assert after.steps[0] == "assemble" and "destination" not in after.steps
    assert "logistics" not in after.steps and after.steps[-1] == "finalize"
    assert after.itinerary.review is not None and after.itinerary.review.passed


def test_swap_re_runs_destination(plan: RunResult, providers: DataProviders) -> None:
    victim = _first_activity(plan, day=1)
    after = _change(plan, ChangeRequest(ChangeKind.SWAP, place_id=victim), providers)
    assert victim not in after.itinerary.place_ids
    assert after.steps[:2] == ["orchestrate", "destination"] and "logistics" not in after.steps
    assert after.itinerary.review is not None and after.itinerary.review.passed


def test_cheaper_lowers_the_cost(plan: RunResult, providers: DataProviders) -> None:
    assert plan.itinerary.budget is not None
    before = plan.itinerary.budget.total
    after = _change(plan, ChangeRequest(ChangeKind.CHEAPER), providers)
    assert after.itinerary.budget is not None and after.itinerary.budget.within_budget
    assert after.itinerary.budget.total < before
    # the budget stays the traveller's; the target is the Budget agent's goal (best effort)
    assert (
        after.itinerary.request.constraints.budget_aed
        == plan.itinerary.request.constraints.budget_aed
    )
    assert after.steps[0] == "budget" and "destination" not in after.steps
    notes = after.itinerary.request.assumptions
    assert any("cheaper plan" in a for a in notes) and any("Cut the plan to" in a for a in notes)
    assert after.itinerary.review is not None and after.itinerary.review.passed


def test_changes_stack_and_history_keeps_every_draft(
    plan: RunResult, providers: DataProviders
) -> None:
    removed = _first_activity(plan)
    once = _change(plan, ChangeRequest(ChangeKind.REMOVE, place_id=removed), providers)
    twice = _change(
        once, ChangeRequest(ChangeKind.SWAP, place_id=_first_activity(once, 1)), providers
    )
    assert removed not in twice.itinerary.place_ids  # an earlier removal stays removed
    revisions = [h.revision for h in twice.history]
    assert revisions == sorted(revisions) and len(revisions) >= 3
    route = [e for e in twice.trace if e.agent == "router" and "you asked" in e.summary]
    assert len(route) == 2


def test_removing_a_must_visit_place_drops_it_from_the_list(providers: DataProviders) -> None:
    text = "3 days in Dubai, AED 5000, culture and beaches, I want to see Kite Beach"
    first = run(text, providers=providers, llm=None, settings=OFFLINE)
    assert "kite-beach" in first.itinerary.place_ids
    after = _change(first, ChangeRequest(ChangeKind.REMOVE, place_id="kite-beach"), providers)
    assert "kite-beach" not in after.itinerary.place_ids
    assert after.itinerary.request.constraints.must_visit == []
    assert after.itinerary.review is not None and after.itinerary.review.passed


def test_a_change_leaves_the_original_plan_untouched(providers: DataProviders) -> None:
    original = run(REQUEST, providers=providers, llm=None, settings=OFFLINE)
    before = original.itinerary
    victim = _first_activity(original)
    changed = _change(original, ChangeRequest(ChangeKind.REMOVE, place_id=victim), providers)
    assert changed.thread_id != original.thread_id
    # the original's saved state (what Saved trips and the replay read) is unchanged
    snapshots = list(original.state_history())
    assert snapshots[0].values["final"] == before and victim in before.place_ids
    assert len(snapshots[0].values["history"]) == len(original.history)
