"""The graph runner: offline, with a stubbed LLM, and (when configured) the real LLMs."""

from __future__ import annotations

import pytest

from agents.orchestrator import RequestError
from agents.skills.scripted import example_request
from core.config import Settings
from core.llm import ScriptedLLM as FakeLLM
from core.llm import llm_configured
from core.runner import run
from core.tracing import TraceKind
from data.providers import DataProviders, get_providers
from tools.views import table_ids

EXAMPLE_REQUEST_TEXT = example_request().raw_text

OFFLINE = Settings(_env_file=None)


def test_example_request_offline(providers: DataProviders) -> None:
    result = run(EXAMPLE_REQUEST_TEXT, providers=providers, llm=None, settings=OFFLINE)
    it = result.itinerary
    assert it.is_final and it.review is not None and it.review.passed
    assert it.budget is not None and it.budget.within_budget
    assert result.metrics.llm_calls == 0 and result.metrics.revisions == 0
    agents = {e.agent for e in result.trace}
    assert {"orchestrator", "destination", "logistics", "budget", "review"} <= agents
    assert result.trace[-1].kind is TraceKind.END
    assert any("December" in a for a in it.request.assumptions)


def test_over_budget_is_revised(providers: DataProviders) -> None:
    text = "4 days in Dubai, mid-range hotel, budget AED 3000, culture and beaches, avoid crowds"
    result = run(text, providers=providers, llm=None, settings=OFFLINE)
    it = result.itinerary
    assert result.metrics.revisions == 1
    assert result.metrics.issues_found >= 1
    assert result.metrics.issues_resolved == result.metrics.issues_found
    assert it.review is not None and it.review.passed
    assert it.budget is not None and it.budget.within_budget
    assert any(a.applied for a in it.alternatives)
    routes = [e for e in result.trace if e.kind is TraceKind.ROUTE and e.agent == "router"]
    assert routes and "budget" in routes[0].summary
    assert all(e.revision == 1 for e in result.trace[result.trace.index(routes[0]) :])


def test_with_stubbed_llm(providers: DataProviders) -> None:
    llm = FakeLLM(
        {
            "ParsedTrip": {
                "days": 3,
                "budget_amount": 4000,
                "interests": ["culture", "beach"],
                "hotel_tier": "budget",
                "avoid": ["crowds"],
                "travelers": 2,
            },
            "DestinationPicks": lambda p: {
                "attraction_ids": table_ids(p["attractions"])[:10],
                "restaurant_ids": table_ids(p["restaurants"])[:8],
                "rationale": {i: "Fits the interests." for i in table_ids(p["attractions"])[:10]},
            },
            "LogisticsChoice": lambda p: {
                "hotel_ranking": [h["area"] for h in p["hotel_areas"]][::-1],
                "rationale": {},
                "transport_notes": "Use the metro for long hops.",
            },
            "DayThemes": lambda p: {
                "days": [
                    {"day": d["day"], "theme": f"Day {d['day']} in {d['area']}"} for d in p["days"]
                ]
            },
            "LLMReview": {"comments": [{"message": "Nice variety."}], "summary": "Good plan."},
        }
    )
    result = run("anything", providers=providers, llm=llm, settings=OFFLINE)
    it = result.itinerary
    assert it.request.constraints.travelers == 2 and len(it.days) == 3
    assert it.days[0].theme.startswith("Day 1 in")
    assert it.review is not None and it.review.passed
    assert any(i.origin.value == "llm" for i in it.review.issues)
    assert result.metrics.llm_calls == len(llm.calls) >= 5
    roles = {c[0] for c in llm.calls}
    assert roles == {"orchestrator", "destination", "logistics", "review"}
    assert not any(e.kind is TraceKind.ERROR for e in result.trace)


def test_llm_errors_never_break_a_run(providers: DataProviders) -> None:
    from core.llm import LLMUnavailableError

    llm = FakeLLM(
        {
            name: LLMUnavailableError("timeout")
            for name in (
                "ParsedTrip",
                "DestinationPicks",
                "LogisticsChoice",
                "DayThemes",
                "LLMReview",
            )
        }
    )
    result = run(EXAMPLE_REQUEST_TEXT, providers=providers, llm=llm, settings=OFFLINE)
    assert result.itinerary.review is not None and result.itinerary.review.passed
    assert sum(e.kind is TraceKind.ERROR for e in result.trace) >= 5


def test_events_stream(providers: DataProviders) -> None:
    seen: list[str] = []
    run(
        EXAMPLE_REQUEST_TEXT,
        providers=providers,
        llm=None,
        settings=OFFLINE,
        on_event=lambda e: seen.append(e.agent),
    )
    assert seen[0] == "runner" and "review" in seen


def test_bad_requests_raise(providers: DataProviders) -> None:
    with pytest.raises(RequestError, match="Dubai"):
        run("3 days in Paris, EUR 2000", providers=providers, llm=None, settings=OFFLINE)


@pytest.mark.llm
@pytest.mark.skipif(
    not llm_configured(every_role=True), reason="needs GROQ_API_KEY and GOOGLE_API_KEY"
)
def test_real_llm_smoke() -> None:  # pragma: no cover - needs API keys
    """Phase 4 exit criterion: one real-LLM run of the example request (Groq + Gemini)."""
    result = run(EXAMPLE_REQUEST_TEXT)
    it = result.itinerary
    assert result.metrics.llm_calls > 0
    # Every agent got a usable LLM answer (no fallback to deterministic code). Guardrail events,
    # e.g. an invented place id being dropped, are allowed: that is the guardrail working.
    fallbacks = [e.summary for e in result.trace if "deterministic fallback" in e.summary]
    assert not fallbacks, fallbacks
    places = get_providers().places
    invented = [pid for pid in it.place_ids if places.get_place(pid) is None]
    assert not invented, invented
    assert it.review is not None and it.budget is not None
    assert it.request.constraints.days == 4 and it.request.constraints.budget_aed == 5000
