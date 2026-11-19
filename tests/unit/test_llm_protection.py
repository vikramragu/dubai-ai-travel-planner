"""Phase 8: protecting the free LLM tiers — the answer cache, the quota guard, the trip-form
path (no parsing call) and fewer calls on revision rounds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import diskcache
import pytest
from pydantic import BaseModel

from agents.orchestrator import form_request
from core.config import Settings
from core.llm import (
    CachingLLM,
    LLMUnavailableError,
    QuotaGuard,
    ScriptedLLM,
    _quota_error,
)
from core.runner import run
from core.tracing import TraceKind
from data.providers import DataProviders
from schemas import HotelTier, Interest, TripConstraints

OFFLINE = Settings(_env_file=None)


class Answer(BaseModel):
    value: int


def _counting(answer: Any = None) -> ScriptedLLM:
    return ScriptedLLM({"Answer": answer or {"value": 1}})


@pytest.fixture
def store(tmp_path: Path) -> Any:
    cache = diskcache.Cache(str(tmp_path / "llm"))
    yield cache
    cache.close()


# ------------------------------------------------------------------ answer cache
def test_identical_questions_are_answered_from_the_cache(store: Any) -> None:
    inner = _counting()
    llm = CachingLLM(inner, OFFLINE, store=store, demo_file=None)
    a = llm.structured("budget", Answer, system="s\nToday is 2026-11-19.", user="u")
    b = llm.structured("budget", Answer, system="s\nToday is 2026-11-20.", user="u")
    assert a == b and len(inner.calls) == 1 and llm.cache_hits == 1  # the date isn't the key
    llm.structured("budget", Answer, system="other", user="u")
    assert len(inner.calls) == 2  # a different question is asked


def test_demo_cache_file_and_recording(store: Any, tmp_path: Path) -> None:
    record: dict[str, Any] = {}
    first = CachingLLM(_counting(), OFFLINE, store=None, demo_file=None, record=record)
    first.structured("review", Answer, system="s", user="u")
    demo = tmp_path / "demo.json"
    demo.write_text(json.dumps(record))
    inner = _counting()
    again = CachingLLM(inner, OFFLINE, store=None, demo_file=demo)
    assert again.structured("review", Answer, system="s", user="u").value == 1
    assert inner.calls == [] and again.cache_hits == 1


# ------------------------------------------------------------------ quota guard
def test_daily_budget_refuses_calls_once_used(store: Any) -> None:
    guard = QuotaGuard(OFFLINE.model_copy(update={"llm_daily_token_budget": 100}), store)
    guard.check()
    guard.add(120)
    with pytest.raises(LLMUnavailableError, match="allowance"):
        guard.check()
    assert guard.status()["available"] is False
    # the count survives a restart (it is kept in the disk cache)
    restarted = QuotaGuard(OFFLINE.model_copy(update={"llm_daily_token_budget": 100}), store)
    assert restarted.used == 120


@pytest.mark.parametrize(
    ("message", "long_pause"),
    [
        ("Error code: 429 - Rate limit reached ... tokens per day (TPD): Limit 200000", True),
        ("Error code: 429 - tokens per minute (TPM): Limit 8000", False),
        ("RESOURCE_EXHAUSTED: quota exceeded", False),
    ],
)
def test_limit_errors_pause_ai_planning(message: str, long_pause: bool) -> None:
    pause = _quota_error(RuntimeError(message))
    assert pause is not None and (pause > 600) is long_pause
    assert _quota_error(RuntimeError("Connection reset")) is None


def test_a_limit_error_pauses_every_later_call(store: Any) -> None:
    guard = QuotaGuard(OFFLINE, store)
    inner = ScriptedLLM({"Answer": LLMUnavailableError("Error code: 429 tokens per day (TPD)")})
    llm = CachingLLM(inner, OFFLINE, store=None, guard=guard, demo_file=None)
    with pytest.raises(LLMUnavailableError):
        llm.structured("budget", Answer, system="s", user="u")
    with pytest.raises(LLMUnavailableError, match="paused"):
        llm.structured("budget", Answer, system="other", user="u")
    assert len(inner.calls) == 1  # the second call never reached the provider


def test_runs_still_plan_when_the_allowance_is_used(store: Any, providers: DataProviders) -> None:
    guard = QuotaGuard(OFFLINE.model_copy(update={"llm_daily_token_budget": 1}), store)
    guard.add(5)
    inner = ScriptedLLM({})
    llm = CachingLLM(inner, OFFLINE, store=None, guard=guard, demo_file=None)
    result = run(
        "3 days in Dubai, AED 4000, culture", providers=providers, llm=llm, settings=OFFLINE
    )
    assert inner.calls == [] and result.itinerary.review is not None
    assert any("allowance" in e.summary for e in result.trace if e.kind is TraceKind.ERROR)


# ------------------------------------------------------------------ fewer calls
def test_the_trip_form_skips_parsing(providers: DataProviders) -> None:
    c = TripConstraints(
        days=2,
        budget_aed=4000,
        interests=[Interest.CULTURE, Interest.BEACH],
        travelers=3,
        children=1,
        hotel_tier=HotelTier.BUDGET,
        must_visit=["kite-beach"],
    )
    req = form_request(c)
    llm = ScriptedLLM({})
    result = run(req.raw_text, request=req, providers=providers, llm=llm, settings=OFFLINE)
    assert "ParsedTrip" not in {schema for _, schema, _, _ in llm.calls}
    assert result.itinerary.request.constraints == c
    assert "kite-beach" in result.itinerary.place_ids
    assert any("adult prices" in a for a in result.itinerary.request.assumptions)
    assert any("trip form" in e.summary for e in result.trace)


def test_review_comments_only_for_a_passing_draft(providers: DataProviders) -> None:
    llm = ScriptedLLM({"LLMReview": {"comments": [{"message": "Nice."}], "summary": "ok"}})
    text = "4 days in Dubai, mid-range hotel, budget AED 3000, culture and beaches, avoid crowds"
    result = run(text, providers=providers, llm=llm, settings=OFFLINE)
    reviews = [c for c in llm.calls if c[1] == "LLMReview"]
    passing = [h for h in result.history if h.itinerary.review and h.itinerary.review.passed]
    assert result.metrics.revisions == 1 and len(reviews) == len(passing) == 1
    assert any(i.origin.value == "llm" for i in result.itinerary.review.issues)  # type: ignore[union-attr]


def test_a_repeated_request_makes_no_llm_calls(store: Any, providers: DataProviders) -> None:
    from tools.views import table_ids

    answers: dict[str, Any] = {
        "ParsedTrip": {"days": 3, "budget_amount": 5000, "interests": ["culture", "beach"]},
        "DestinationPicks": lambda p: {"attraction_ids": table_ids(p["attractions"])[:10]},
        "LogisticsChoice": lambda p: {"hotel_ranking": [h["area"] for h in p["hotel_areas"]]},
        "DayThemes": lambda p: {"days": [{"day": d["day"], "theme": "A day"} for d in p["days"]]},
        "LLMReview": {"comments": [], "summary": "ok"},
    }
    text = "3 days in Dubai, AED 5000, culture and beaches"
    inner = ScriptedLLM(answers)
    first = run(
        text, providers=providers, llm=CachingLLM(inner, OFFLINE, store=store, demo_file=None),
        settings=OFFLINE,
    )  # fmt: skip
    asked = len(inner.calls)
    again = CachingLLM(inner, OFFLINE, store=store, demo_file=None)
    second = run(text, providers=providers, llm=again, settings=OFFLINE)
    assert asked > 0 and len(inner.calls) == asked  # nothing new reached the provider
    assert second.metrics.llm_cache_hits > 0
    assert second.itinerary.days == first.itinerary.days
