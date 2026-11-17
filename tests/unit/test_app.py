"""Phase 6: the Streamlit UI — its data helpers and a headless run of the whole app."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.components.budget import alternative_rows, category_rows, source_split
from app.components.graph_view import (
    active_nodes,
    highlighted,
    replay_steps,
    visited_nodes,
)
from app.components.itinerary import route_points, slot_rows
from app.components.review import diff_drafts, issue_rows
from app.components.timeline import Status, agent_cards
from app.state import RunOptions, start_run
from core.config import Settings
from core.runner import RunResult, run
from core.tracing import TraceEvent, TraceKind
from data.providers import DataProviders
from schemas import SlotKind

OFFLINE = Settings(_env_file=None)
OVER_BUDGET = "4 days in Dubai, mid-range hotel, budget AED 3000, culture and beaches, avoid crowds"
APP = Path(__file__).resolve().parents[2] / "app" / "streamlit_app.py"


@pytest.fixture(scope="module")
def revised(providers: DataProviders) -> RunResult:
    return run(OVER_BUDGET, providers=providers, llm=None, settings=OFFLINE)


def _event(agent: str, kind: TraceKind, node: str, revision: int = 0) -> TraceEvent:
    return TraceEvent(
        run_id="r", agent=agent, kind=kind, summary=f"{agent} {kind.value}",
        payload={"node": node}, revision=revision,
    )  # fmt: skip


# ------------------------------------------------------------------ timeline
def test_agent_status_follows_the_events() -> None:
    events = [
        _event("orchestrator", TraceKind.START, "parse"),
        _event("orchestrator", TraceKind.OUTPUT, "parse"),
        _event("destination", TraceKind.START, "destination"),
        _event("logistics", TraceKind.START, "logistics"),
        _event("logistics", TraceKind.OUTPUT, "logistics"),
        _event("budget", TraceKind.START, "budget", revision=1),
    ]
    cards = agent_cards(events)
    assert cards["orchestrator"].status is Status.DONE
    assert cards["destination"].status is Status.RUNNING
    assert cards["logistics"].status is Status.DONE
    assert cards["budget"].status is Status.REVISING
    assert cards["review"].status is Status.WAITING
    assert cards["logistics"].summary == "logistics output"
    assert agent_cards(events, finished=True)["destination"].status is Status.DONE


def test_llm_fallbacks_are_counted() -> None:
    cards = agent_cards([_event("review", TraceKind.ERROR, "review")])
    assert cards["review"].errors == 1 and cards["review"].status is Status.WAITING


# ------------------------------------------------------------------ graph view
def test_parallel_nodes_are_both_active() -> None:
    events = [
        _event("orchestrator", TraceKind.ROUTE, "orchestrate"),
        _event("destination", TraceKind.START, "destination"),
        _event("logistics", TraceKind.START, "logistics"),
    ]
    assert active_nodes(events) == ["destination", "logistics"]
    done = [*events, _event("destination", TraceKind.OUTPUT, "destination")]
    assert active_nodes(done) == ["logistics"]
    assert visited_nodes(done) == ["orchestrate", "destination", "logistics"]
    assert active_nodes([]) == []


def test_highlighting_adds_mermaid_classes() -> None:
    code = highlighted("graph TD;\n\ta-->b;", ["review"], ["parse", "review"])
    assert "\tclass parse visited" in code and "\tclass review active" in code


def test_replay_has_every_checkpoint(revised: RunResult) -> None:
    steps = replay_steps(revised)
    assert len(steps) == len(list(revised.state_history()))
    assert steps[0].next_nodes == ["__start__"] and steps[-1].next_nodes == []
    assert sorted(next(s.next_nodes for s in steps if len(s.next_nodes) == 2)) == [
        "destination",
        "logistics",
    ]
    last = steps[-1].state
    assert last["revision"] == 1 and last["drafts_in_history"] == 2
    assert "(over)" not in last["budget"]
    assert "(over)" in next(s.state["budget"] for s in steps if "budget" in s.state)


# ------------------------------------------------------------------ itinerary
def test_slot_rows_carry_costs_and_sources(revised: RunResult, providers: DataProviders) -> None:
    it = revised.itinerary
    day = it.days[0]
    rows = slot_rows(day, it, providers)
    assert len(rows) == len(day.slots)
    for slot, row in zip(day.slots, rows, strict=True):
        assert bool(row["source"]) is (slot.kind is not SlotKind.REST), row  # free time: none
        if slot.kind is SlotKind.MEAL:
            assert row["cost_aed"] and row["cost_aed"] > 0
    assert any("verified" in r["source"] for r in rows)  # OSM-matched places


def test_route_starts_and_ends_at_the_hotel(revised: RunResult, providers: DataProviders) -> None:
    it = revised.itinerary
    points = route_points(it.days[0], it, providers)
    assert points[0]["kind"] == points[-1]["kind"] == "hotel"
    assert [p["id"] for p in points[1:-1]] == it.days[0].place_ids


# ------------------------------------------------------------------ budget
def test_budget_rows_add_up(revised: RunResult) -> None:
    b = revised.itinerary.budget
    assert b is not None
    assert sum(r["aed"] for r in category_rows(b)) == pytest.approx(b.total)
    assert sum(source_split(b).values()) == pytest.approx(b.total, abs=0.05)
    rows = alternative_rows(revised.itinerary.alternatives)
    flags = [r["applied"] for r in rows]
    assert flags[0] and flags == sorted(flags, reverse=True)  # applied ones first


# ------------------------------------------------------------------ review & diff
def test_diff_shows_what_the_revision_changed(revised: RunResult) -> None:
    first, second = revised.history
    diff = diff_drafts(first, second)
    assert diff.hotel is not None and diff.hotel[0] != diff.hotel[1]
    assert diff.total is not None and diff.total[1] < diff.total[0]
    assert diff.score is not None and diff.score[1] > diff.score[0]
    codes = {r["code"] for r in issue_rows(first.itinerary)}
    assert "over_budget" in codes
    assert diff_drafts(second, second).days == [] and not diff_drafts(second, second).costs


# ------------------------------------------------------------------ worker thread
def test_worker_thread_streams_events(providers: DataProviders) -> None:
    handle = start_run(
        RunOptions(OVER_BUDGET, use_llm=False), providers=providers, settings=OFFLINE
    )
    handle.wait(60)
    assert handle.finished and handle.error is None and handle.result is not None
    assert len(handle.events) == len(handle.result.trace)
    assert any((e.payload or {}).get("node") == "destination" for e in handle.events)


def test_worker_thread_keeps_request_errors(providers: DataProviders) -> None:
    from agents.orchestrator import RequestError

    handle = start_run(
        RunOptions("3 days in Paris, EUR 2000", use_llm=False), providers=providers,
        settings=OFFLINE,
    )  # fmt: skip
    handle.wait(60)
    assert isinstance(handle.error, RequestError) and handle.result is None


# ------------------------------------------------------------------ the app
def test_app_plans_a_trip_offline() -> None:
    """Exit criterion: the example request runs from the UI in offline mode."""
    at = AppTest.from_file(str(APP), default_timeout=90)
    at.run()
    assert not at.exception
    assert "Dubai" in (at.text_area(key="request").value or "")
    model = at.radio(key="model")
    if not model.disabled:  # keys in .env: still test offline
        model.set_value("No LLM (deterministic)")
    assert model.value == "No LLM (deterministic)"
    at.text_area(key="request").set_value(OVER_BUDGET)
    at.button(key="plan").click().run()
    for _ in range(20):  # the live view reruns the script until the worker finishes
        if at.success or at.warning or at.exception:
            break
        at.run()
    assert not at.exception, at.exception
    assert at.success and "revision" in at.success[0].value
    labels = [t.label for t in at.tabs]
    for tab in ("Itinerary", "Budget", "Review & revisions", "Agent timeline", "Graph", "Metrics"):
        assert tab in labels
    assert {"Day 1", "Day 4"} <= set(labels)
    text = " ".join(m.value for m in at.markdown)
    assert "Draft 0 → draft 1" in text and "Router:" in text
    assert any("Nothing is booked" in c.value for c in at.caption)
    at.slider(key="replay_step").set_value(0).run()
    assert not at.exception


def test_live_view_renders_a_run_in_progress() -> None:
    def live_page() -> None:
        from app.components.graph_view import render_graph_live
        from app.components.timeline import render_timeline
        from core.tracing import TraceEvent, TraceKind

        events = [
            TraceEvent(run_id="r", agent=a, kind=TraceKind.START, summary=f"{a} started",
                       payload={"node": a})
            for a in ("destination", "logistics")
        ]  # fmt: skip
        render_timeline(events)
        render_graph_live(events)

    at = AppTest.from_function(live_page, default_timeout=30).run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Destination research** — running" in text and "Logistics** — running" in text


@pytest.mark.parametrize("keys", [False, True])
def test_sidebar_model_choice_follows_the_keys(keys: bool) -> None:
    def page(keys: bool) -> None:
        from pydantic import SecretStr

        from app.streamlit_app import sidebar
        from core.config import Settings

        extra = {"groq_api_key": SecretStr("g"), "google_api_key": SecretStr("k")} if keys else {}
        sidebar(Settings(_env_file=None, **extra))  # type: ignore[arg-type]

    at = AppTest.from_function(page, args=(keys,), default_timeout=30).run()
    assert not at.exception
    radio = at.sidebar.radio(key="model")
    assert radio.disabled is (not keys)
    assert radio.value == ("Configured LLMs" if keys else "No LLM (deterministic)")
    text = " ".join(m.value for m in at.sidebar.markdown)
    assert text.count("🟢" if keys else "⚪") == 5
