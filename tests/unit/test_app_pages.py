"""Phase 8: the traveller UI — pages, dev mode, change requests, saved trips, skyline.

The whole app runs headlessly with Streamlit's ``AppTest``. LLM providers are switched off
(``LLM_PROVIDER=none``) so no test ever calls a real model, whatever is in ``.env``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from app.components.prechecks import precheck
from app.components.results import progress_steps
from app.components.skyline import CSS, sky_svg, skyline_svg
from app.pages.browse import add_to_trip, place_rows
from app.pages.plan_trip import build_constraints
from app.pages.saved import compare_rows
from app.state import itinerary_json, load_itinerary, make_saved
from core.config import Settings, get_settings
from core.runner import run
from core.tracing import TraceEvent, TraceKind
from data.providers import AttractionQuery, DataProviders
from schemas import Interest, Pace

APP = str(Path(__file__).resolve().parents[2] / "app" / "streamlit_app.py")
OFFLINE = Settings(_env_file=None)


@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No usable LLM provider, even if .env has keys: the app plans without AI."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("LLM_PROVIDER_REVIEW", "none")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    assert not at.exception, at.exception
    return at


def _click(at: AppTest, label: str) -> AppTest:
    at.button[[b.label for b in at.button].index(label)].click().run()
    for _ in range(40):  # the progress view reruns until the worker thread finishes
        if at.success or at.warning or at.exception or at.error:
            break
        at.run()
    assert not at.exception, at.exception
    return at


# ------------------------------------------------------------------ whole app
def test_plan_from_the_form_without_typing(no_llm: None) -> None:
    at = _app()
    assert at.title[0].value == "Plan a trip"
    assert "AI planning is off" in " ".join(c.value for c in at.sidebar.caption)
    at = _click(at, "Plan my trip")
    assert at.success and "4-day plan" in at.success[0].value
    labels = [t.label for t in at.tabs]
    assert {"Your plan", "Budget", "Day 1", "Day 4"} <= set(labels)
    assert "Graph" not in labels and "Metrics" not in labels  # dev mode is off
    assert any(b.label == "💸 Make it cheaper" for b in at.button)
    buttons = {b.label for b in at.button}
    assert {"🗑️ Remove this stop", "🔁 Swap for another place"} <= buttons


def test_dev_mode_shows_the_technical_views(no_llm: None) -> None:
    at = _app()
    assert not any(r.label == "Model" for r in at.sidebar.radio)
    at.sidebar.toggle(key="_dev").set_value(True).run()
    assert any(r.label == "Model" for r in at.sidebar.radio)
    assert at.sidebar.radio(key="model").disabled  # no keys: AI can't be chosen
    assert any(s.label == "Data mode" for s in at.sidebar.selectbox)
    at = _click(at, "Plan my trip")
    labels = [t.label for t in at.tabs]
    assert {"Review & revisions", "Agent timeline", "Graph", "Metrics"} <= set(labels)


def test_make_it_cheaper_from_the_plan(no_llm: None) -> None:
    at = _click(_app(), "Plan my trip")
    total = at.success[0].value
    at = _click(at, "💸 Make it cheaper")
    assert any("Updated the plan" in i.value for i in at.info)
    assert at.success and at.success[0].value != total


def test_save_a_trip(no_llm: None) -> None:
    at = _click(_app(), "Plan my trip")
    at.button[[b.label for b in at.button].index("⭐ Save this trip")].click().run()
    assert len(at.session_state["saved"]) == 1


# ------------------------------------------------------------------ pages, as functions
def test_ask_page_examples_and_checks(no_llm: None) -> None:
    def page() -> None:
        import streamlit as st

        from app.pages.ask_agents import page as ask
        from app.state import session

        session(st.session_state)
        ask()

    at = AppTest.from_function(page, default_timeout=60).run()
    assert not at.exception
    at.text_area(key="request").set_value("3 days in Paris").run()
    assert at.button(key="plan").disabled  # not Dubai, no budget: blocked before planning
    assert any("only covers Dubai" in e.value for e in at.error)
    at.button_group(key="example").set_value("Family weekend").run()
    assert "family of four" in (at.text_area(key="request").value or "")
    assert not at.button(key="plan").disabled


def test_browse_and_saved_pages_render(no_llm: None) -> None:
    def browse_page() -> None:
        import streamlit as st

        from app.pages.browse import page
        from app.state import session

        session(st.session_state)
        page()

    at = AppTest.from_function(browse_page, default_timeout=60).run()
    assert not at.exception and any("places" in m.value for m in at.markdown)

    def saved_page() -> None:
        import streamlit as st

        from app.pages.saved import page
        from app.state import session

        session(st.session_state)
        page()

    at = AppTest.from_function(saved_page, default_timeout=60).run()
    assert not at.exception and any("No saved trips" in i.value for i in at.info)


def test_travel_data_page_renders(no_llm: None) -> None:
    def data_page() -> None:
        from app.pages.travel_data import page

        page()

    at = AppTest.from_function(data_page, default_timeout=60).run()
    assert not at.exception
    assert any(s.value == "Freshness" for s in at.subheader)


# ------------------------------------------------------------------ helpers
def test_form_values_become_a_request() -> None:
    from datetime import date

    c = build_constraints(
        start=date(2026, 12, 10),
        days=3,
        budget=4000,
        adults=2,
        children=2,
        tier="Mid-range",
        interests=[Interest.FAMILY],
        pace=Pace.RELAXED,
        avoid=[],
        vegetarian=True,
        must_visit=["kite-beach"],
    )
    assert (c.travelers, c.children, c.rooms, c.dietary) == (4, 2, 1, ["vegetarian"])
    with pytest.raises(ValueError, match="interest"):
        build_constraints(
            start=date(2026, 12, 10),
            days=3,
            budget=4000,
            adults=2,
            children=0,
            tier="Budget",
            interests=[],
            pace=Pace.BALANCED,
            avoid=[],
            vegetarian=False,
            must_visit=[],
        )


@pytest.mark.parametrize(
    ("text", "level", "words"),
    [
        ("3 days in Paris, EUR 2000", "error", "only covers Dubai"),
        ("4 days in Dubai, culture", "error", "budget"),
        ("12 days in Dubai, AED 9000, culture", "warning", "first 7"),
        ("Dubai, AED 3000, culture", "info", "3 days"),
    ],
)
def test_prechecks(text: str, level: str, words: str) -> None:
    assert any(c.level == level and words in c.message for c in precheck(text))
    assert not [c for c in precheck("4 days in Dubai, AED 5000, culture") if c.level == "error"]


def test_progress_steps_follow_the_nodes() -> None:
    def ev(node: str, revision: int = 0) -> TraceEvent:
        return TraceEvent(
            run_id="r",
            agent="x",
            kind=TraceKind.START,
            summary="s",
            payload={"node": node},
            revision=revision,
        )

    steps, current = progress_steps([ev("parse"), ev("orchestrate"), ev("logistics")])
    assert steps[current][0] == "logistics"
    _, current = progress_steps([ev("parse"), ev("review"), ev("budget", revision=1)])
    assert steps[current][0] == "budget"  # a revision round can step back


def test_saved_trips_round_trip_and_compare(providers: DataProviders) -> None:
    a = run("3 days in Dubai, AED 5000, culture", providers=providers, llm=None, settings=OFFLINE)
    b = run("3 days in Dubai, AED 3000, beaches", providers=providers, llm=None, settings=OFFLINE)
    saved = [make_saved(a.itinerary, a), make_saved(b.itinerary)]
    again = load_itinerary(itinerary_json(a.itinerary))
    assert again == a.itinerary
    with pytest.raises(ValueError, match="trip file"):
        load_itinerary(b'{"not": "a trip"}')
    rows = compare_rows(saved)
    total = next(r for r in rows if r[""] == "Total (AED)")
    assert total[saved[0].name] == round(a.itinerary.budget.total)  # type: ignore[union-attr]


def test_add_to_trip_caps_at_ten(providers: DataProviders) -> None:
    places = providers.places.search_attractions(AttractionQuery(limit=50))
    state: dict[str, Any] = {"must_visit": []}
    current, added = add_to_trip(state, [p.id for p in places[:12]])
    assert added == 10 and len(current) == 10
    assert add_to_trip(state, [places[0].id])[1] == 0  # no duplicates
    assert {"name", "price", "source"} <= set(place_rows(places[:1])[0])


def test_skyline_has_both_scenes_and_follows_the_theme() -> None:
    svg = sky_svg() + skyline_svg()
    assert "<svg" in svg and 'class="w"' in svg and "beacon" in svg  # windows, Burj beacon
    assert 'data-dtp-theme="night"' in CSS and "--win-o: 0.95" in CSS  # lights on at night
    assert "transition" in CSS  # day and night cross-fade

    def page() -> None:
        from app.components.skyline import render_skyline

        render_skyline()

    at = AppTest.from_function(page, default_timeout=30).run()
    assert not at.exception
