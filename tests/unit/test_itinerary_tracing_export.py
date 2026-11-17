"""Slots, day plans, the full itinerary, tracing models and JSON-schema export."""

from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.tracing import RunMetrics, TraceEvent, TraceKind
from schemas import (
    DayPlan,
    Itinerary,
    ItinerarySnapshot,
    ResearchResult,
    Slot,
    SlotKind,
    Source,
)
from schemas.export import EXPORTED_MODELS, json_schemas, write_json_schemas


class TestSlot:
    def test_end_after_start(self) -> None:
        with pytest.raises(ValidationError, match="after start"):
            Slot(start=time(10), end=time(9), kind=SlotKind.REST, title="Nap")

    def test_activity_needs_place(self) -> None:
        with pytest.raises(ValidationError, match="place_id"):
            Slot(start=time(9), end=time(10), kind=SlotKind.ACTIVITY, title="Something")

    def test_transit_needs_leg(self) -> None:
        with pytest.raises(ValidationError, match="leg"):
            Slot(start=time(9), end=time(10), kind=SlotKind.TRANSIT, title="Taxi")

    def test_duration(self) -> None:
        s = Slot(start=time(9, 15), end=time(11, 0), kind=SlotKind.REST, title="Break")
        assert s.duration_min == 105


class TestDayPlan:
    def test_overlapping_slots_rejected(self, example_itinerary: Itinerary) -> None:
        day = example_itinerary.days[0]
        overlapping = day.slots[1].model_copy(update={"start": time(8, 30)})
        with pytest.raises(ValidationError, match="starts before"):
            DayPlan.model_validate({**day.model_dump(), "slots": [day.slots[0], overlapping]})

    def test_minutes_of(self, example_itinerary: Itinerary) -> None:
        day1 = example_itinerary.days[0]
        assert day1.minutes_of(SlotKind.ACTIVITY) == 120 + 90
        assert day1.minutes_of(SlotKind.MEAL) == 60 + 75


class TestItinerary:
    def test_example_itinerary_validates(self, example_itinerary: Itinerary) -> None:
        it = example_itinerary
        assert it.is_final
        assert len(it.days) == 4
        assert it.budget is not None and it.budget.within_budget
        assert it.budget.total < it.request.constraints.budget_aed
        assert it.review is not None and it.review.passed

    def test_example_only_uses_researched_places(
        self, example_itinerary: Itinerary, example_research: ResearchResult
    ) -> None:
        assert set(example_itinerary.place_ids) <= example_research.place_ids

    def test_example_has_no_high_crowd_places(
        self, example_itinerary: Itinerary, example_research: ResearchResult
    ) -> None:
        crowd = {a.id: a.crowd_level for a in example_research.attractions}
        assert all(crowd.get(pid) != "high" for pid in example_itinerary.place_ids)

    def test_json_round_trip(self, example_itinerary: Itinerary) -> None:
        restored = Itinerary.model_validate_json(example_itinerary.model_dump_json())
        assert restored == example_itinerary

    def _data(self, it: Itinerary) -> dict[str, Any]:
        return it.model_dump()

    def test_day_count_must_match_request(self, example_itinerary: Itinerary) -> None:
        data = self._data(example_itinerary)
        data["days"] = data["days"][:3]
        with pytest.raises(ValidationError, match="request asks for 4"):
            Itinerary.model_validate(data)

    def test_days_numbered_in_order(self, example_itinerary: Itinerary) -> None:
        data = self._data(example_itinerary)
        data["days"][0]["day"], data["days"][1]["day"] = 2, 1
        with pytest.raises(ValidationError, match="numbered"):
            Itinerary.model_validate(data)

    def test_dates_follow_start_date(self, example_itinerary: Itinerary) -> None:
        data = self._data(example_itinerary)
        data["request"]["constraints"]["start_date"] = date(2026, 12, 1)
        data["days"][1]["date"] = date(2026, 12, 5)
        with pytest.raises(ValidationError, match="2026-12-02"):
            Itinerary.model_validate(data)

    def test_budget_must_match_request(self, example_itinerary: Itinerary) -> None:
        data = self._data(example_itinerary)
        data["request"]["constraints"]["budget_aed"] = 4000
        with pytest.raises(ValidationError, match="budget"):
            Itinerary.model_validate(data)

    def test_draft_without_budget_or_review(self, example_itinerary: Itinerary) -> None:
        draft = example_itinerary.model_copy(update={"budget": None, "review": None})
        assert not Itinerary.model_validate(draft.model_dump()).is_final

    def test_unknown_field_rejected(self, example_itinerary: Itinerary) -> None:
        with pytest.raises(ValidationError):
            Itinerary.model_validate({**self._data(example_itinerary), "flights": []})

    def test_snapshot(self, example_itinerary: Itinerary) -> None:
        snap = ItinerarySnapshot(revision=1, itinerary=example_itinerary, note="after budget fix")
        assert snap.created_at.tzinfo is not None

    def test_all_example_costs_are_estimated(self, example_itinerary: Itinerary) -> None:
        assert example_itinerary.budget is not None
        b = example_itinerary.budget
        assert b.total_by_source(Source.ESTIMATED) == b.total


class TestTracing:
    def test_trace_event(self) -> None:
        e = TraceEvent(run_id="r1", agent="budget", kind=TraceKind.OUTPUT, summary="AED 4,820")
        assert e.ts.tzinfo is not None
        assert e.revision == 0

    def test_trace_event_needs_summary(self) -> None:
        with pytest.raises(ValidationError):
            TraceEvent(run_id="r1", agent="budget", kind=TraceKind.OUTPUT, summary="")

    def test_metrics(self) -> None:
        assert RunMetrics(issues_found=2, issues_resolved=2).final_score is None
        with pytest.raises(ValidationError):
            RunMetrics(issues_found=1, issues_resolved=2)


class TestExport:
    def test_every_model_exports(self) -> None:
        schemas = json_schemas()
        assert set(schemas) == set(EXPORTED_MODELS)
        assert "days" in schemas["Itinerary"]["properties"]

    def test_write_json_schemas(self, tmp_path: Path) -> None:
        paths = write_json_schemas(tmp_path)
        assert len(paths) == len(EXPORTED_MODELS)
        assert json.loads((tmp_path / "TripRequest.schema.json").read_text())["title"] == (
            "TripRequest"
        )
