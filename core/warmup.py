"""Demo warm-up: run the demo requests once with the real LLMs and keep every answer.

The answers go to ``data/demo_llm_cache.json`` (commit it). The app reads that file on start,
so the demo requests then make **no LLM calls** anywhere, including Streamlit Cloud, whose disk
cache is separate from yours: nothing can hit a free-tier limit during a demo, and the plans
come back in a second. Re-run it before a demo (answers depend on the prompts, so a change to
an agent's prompt or to the dataset needs a fresh warm-up).

Usage::

    uv run python -m core.warmup                 # the demo requests below
    uv run python -m core.warmup "my request"    # add your own
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agents.orchestrator import form_request
from core.config import get_settings
from core.llm import DEMO_CACHE, CachingLLM, make_llm
from core.runner import run
from schemas import HotelTier, Interest, TripConstraints

DEMO_REQUESTS = [
    "Plan a 4-day trip to Dubai with a budget of AED 5,000, excluding flights. I enjoy cultural "
    "attractions, local food, and beaches. I prefer affordable hotels and want to avoid crowded "
    "places.",
    "4 days in Dubai, mid-range hotel, budget AED 3000, culture and beaches, avoid crowds",
    "3 days in Dubai for a family of four with two kids, budget AED 6000, culture and beaches, "
    "we want to see the Etihad Museum and Kite Beach",
]
DEMO_FORMS = [
    TripConstraints(
        days=3,
        budget_aed=5000,
        interests=[Interest.CULTURE, Interest.FAMILY, Interest.BEACH],
        travelers=4,
        children=2,
        rooms=2,
        hotel_tier=HotelTier.MID,
        must_visit=["etihad-museum"],
    ),
]


def main(argv: list[str]) -> int:  # pragma: no cover - calls the real LLMs
    settings = get_settings()
    inner = make_llm(settings)
    if inner is None:
        print("No API keys configured: nothing to warm up (set GROQ_API_KEY / GOOGLE_API_KEY).")
        return 1
    record: dict[str, Any] = {}
    if DEMO_CACHE.exists():
        record.update(json.loads(DEMO_CACHE.read_text(encoding="utf-8")))
    for text in [*DEMO_REQUESTS, *argv]:
        before = inner.usage.calls
        llm = CachingLLM(inner, settings, store=None, demo_file=DEMO_CACHE, record=record)
        result = run(text, llm=llm, settings=settings)
        calls = inner.usage.calls - before
        print(f"{result.metrics.final_score:>5.0f}  {calls:>3} new calls  {text[:70]}")
    for c in DEMO_FORMS:
        req = form_request(c)
        before = inner.usage.calls
        llm = CachingLLM(inner, settings, store=None, demo_file=DEMO_CACHE, record=record)
        result = run(req.raw_text, request=req, llm=llm, settings=settings)
        calls = inner.usage.calls - before
        print(f"{result.metrics.final_score:>5.0f}  {calls:>3} new calls  (trip form)")
    DEMO_CACHE.write_text(json.dumps(record, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(record)} answers to {DEMO_CACHE}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
