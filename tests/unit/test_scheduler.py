"""Scheduler: scoring, day assignment, timelines, opening hours, pace and preferences."""

from __future__ import annotations

from datetime import date, time

import pytest

from agents.skills.calendar import reference_start, trip_calendar
from agents.skills.rule_checks import check_itinerary
from agents.skills.scheduler import (
    SchedulerOptions,
    TransportPolicy,
    _earliest_start,
    _tour,
    crowd_penalty,
    schedule,
    score,
)
from agents.skills.scripted import example_request, pick_hotel
from data.providers import DataProviders
from schemas import (
    Attraction,
    Avoidance,
    CrowdLevel,
    Interest,
    Itinerary,
    OpeningHours,
    Pace,
    SlotKind,
    TimeWindow,
    TransportMode,
    TripRequest,
    Weekday,
)


def _request(**changes: object) -> TripRequest:
    req = example_request()
    return req.model_copy(update={"constraints": req.constraints.model_copy(update=changes)})


def _plan(providers: DataProviders, req: TripRequest | None = None, **opts: object) -> Itinerary:
    req = req or example_request()
    hotel = pick_hotel(req, providers)
    return schedule(req, hotel, providers, options=SchedulerOptions(**opts)).itinerary  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def example_plan(providers: DataProviders) -> Itinerary:
    return _plan(providers)


class TestScoring:
    def test_interest_and_crowd(self, providers: DataProviders) -> None:
        c = example_request().constraints
        museum = providers.places.get_place("al-shindagha-museum")
        mall = providers.places.get_place("dubai-mall")
        ski = providers.places.get_place("ski-dubai")
        assert isinstance(museum, Attraction) and isinstance(mall, Attraction)
        assert isinstance(ski, Attraction)
        assert score(museum, c, 12) > score(mall, c, 12)  # crowded and off-interest-ish
        assert score(ski, c, 12) == 0  # no matching interest

    def test_crowd_penalty_is_stronger_when_avoiding(self) -> None:
        assert crowd_penalty(CrowdLevel.HIGH, True) > crowd_penalty(CrowdLevel.HIGH, False)
        assert crowd_penalty(CrowdLevel.LOW, True) == 0

    def test_seasonal_places_score_zero_in_summer(self, providers: DataProviders) -> None:
        c = _request(interests=[Interest.CULTURE, Interest.FAMILY]).constraints
        gv = providers.places.get_place("global-village")
        assert isinstance(gv, Attraction)
        assert score(gv, c, 12) > 0
        assert score(gv, c, 7) == 0


class TestHelpers:
    def test_earliest_start_waits_for_opening(self) -> None:
        hours = OpeningHours(weekly={Weekday.MON: [TimeWindow(opens=time(10), closes=time(18))]})
        day = trip_calendar(example_request().constraints)[0]  # a Monday
        assert day.weekday is Weekday.MON
        assert _earliest_start(hours, day, 9 * 60 + 30, 60) == 10 * 60  # waits 30 min
        assert _earliest_start(hours, day, 7 * 60, 60) is None  # would wait too long
        assert _earliest_start(hours, day, 17 * 60 + 30, 60) is None  # closes first
        assert _earliest_start(None, day, 9 * 60, 60) == 9 * 60  # unknown: assume open

    def test_reference_start_is_a_monday(self) -> None:
        assert reference_start(12) == date(2026, 12, 7)
        assert all(reference_start(m).weekday() == 0 for m in range(1, 13))

    def test_tour_visits_every_stop(self, providers: DataProviders) -> None:
        from agents.skills.scheduler import _Scored

        ids = ["kite-beach", "al-shindagha-museum", "deira-gold-souk", "etihad-museum"]
        stops = []
        for i in ids:
            a = providers.places.get_place(i)
            assert isinstance(a, Attraction)
            stops.append(_Scored(a, 1.0))
        hotel = providers.places.neighborhoods()[0].location
        assert {s.place.id for s in _tour(hotel, stops)} == set(ids)


class TestExamplePlan:
    def test_shape(self, example_plan: Itinerary) -> None:
        assert len(example_plan.days) == 4
        assert example_plan.budget is None and example_plan.review is None  # a draft

    def test_passes_rule_checks(self, example_plan: Itinerary, providers: DataProviders) -> None:
        codes = {i.code for i in check_itinerary(example_plan, providers)}
        assert codes <= {"budget_missing"}  # only "no budget yet" on a draft

    def test_days_start_and_end_at_the_hotel(self, example_plan: Itinerary) -> None:
        hotel = example_plan.hotel.neighborhood_id
        for day in example_plan.days:
            legs = [s.leg for s in day.slots if s.leg is not None]
            assert legs[0].from_id == hotel
            assert legs[-1].to_id == hotel

    def test_legs_connect_the_places(self, example_plan: Itinerary) -> None:
        for day in example_plan.days:
            here = example_plan.hotel.neighborhood_id
            for s in day.slots:
                if s.leg is not None:
                    assert s.leg.from_id == here
                    here = s.leg.to_id
                elif s.place_id is not None:
                    assert s.place_id == here

    def test_respects_pace_and_crowds(
        self, example_plan: Itinerary, providers: DataProviders
    ) -> None:
        limit = Pace.BALANCED.activity_hours * 60
        for day in example_plan.days:
            assert day.minutes_of(SlotKind.ACTIVITY, SlotKind.TRANSIT) <= limit
            for s in day.slots:
                if s.place_id:
                    p = providers.places.get_place(s.place_id)
                    assert p is not None and p.crowd_level is not CrowdLevel.HIGH

    def test_covers_interests_without_repeats(
        self, example_plan: Itinerary, providers: DataProviders
    ) -> None:
        activity_ids = [
            s.place_id for d in example_plan.days for s in d.slots if s.kind is SlotKind.ACTIVITY
        ]
        assert len(activity_ids) == len(set(activity_ids))
        tags = set()
        for pid in activity_ids:
            p = providers.places.get_place(pid or "")
            assert p is not None
            tags |= set(p.tags)
        assert {"culture", "beach"} <= tags

    def test_days_stay_in_one_area(self, example_plan: Itinerary, providers: DataProviders) -> None:
        """Cluster-first: every day's stops are within ~10 km of each other."""
        for day in example_plan.days:
            locs = [
                p.location
                for s in day.slots
                if s.kind is SlotKind.ACTIVITY
                and (p := providers.places.get_place(s.place_id or ""))
            ]
            assert all(a.distance_km(b) < 12 for a in locs for b in locs), day.theme

    def test_no_start_date_is_noted(self, providers: DataProviders) -> None:
        req = example_request()
        result = schedule(req, pick_hotel(req, providers), providers)
        assert any("assumed" in n for n in result.notes)
        assert all(d.date is None for d in result.itinerary.days)


class TestPreferences:
    def test_start_date_sets_dates(self, providers: DataProviders) -> None:
        it = _plan(providers, _request(start_date=date(2027, 1, 15)))
        assert [d.date for d in it.days][:2] == [date(2027, 1, 15), date(2027, 1, 16)]

    def test_avoid_early_mornings_and_late_nights(self, providers: DataProviders) -> None:
        req = _request(avoid=[Avoidance.EARLY_MORNINGS, Avoidance.LATE_NIGHTS])
        it = _plan(providers, req)
        for day in it.days:
            assert day.slots[0].start >= time(10)
            assert day.slots[-1].end <= time(22, 30)

    def test_relaxed_pace_is_lighter(self, providers: DataProviders) -> None:
        relaxed = _plan(providers, _request(pace=Pace.RELAXED))
        packed = _plan(providers, _request(pace=Pace.PACKED))

        def busy(it: Itinerary) -> int:
            return sum(d.minutes_of(SlotKind.ACTIVITY) for d in it.days)

        assert busy(relaxed) < busy(packed)
        for day in relaxed.days:
            assert day.minutes_of(SlotKind.ACTIVITY, SlotKind.TRANSIT) <= 6 * 60

    def test_public_first_uses_fewer_taxis(self, providers: DataProviders) -> None:
        def taxis(it: Itinerary) -> int:
            return sum(
                1 for d in it.days for s in d.slots if s.leg and s.leg.mode is TransportMode.TAXI
            )

        assert taxis(_plan(providers, transport=TransportPolicy.PUBLIC_FIRST)) < taxis(
            _plan(providers, transport=TransportPolicy.FASTEST)
        )

    def test_avoid_long_walks(self, providers: DataProviders) -> None:
        it = _plan(providers, _request(avoid=[Avoidance.LONG_WALKS]))
        for d in it.days:
            for s in d.slots:
                if s.leg and s.leg.mode is TransportMode.WALK:
                    assert s.leg.duration_min.value <= 10

    def test_exclude_and_swap(self, providers: DataProviders, example_plan: Itinerary) -> None:
        first = next(
            s.place_id for d in example_plan.days for s in d.slots if s.kind is SlotKind.ACTIVITY
        )
        assert first is not None
        it = _plan(providers, exclude_ids=frozenset({first}), swaps=(("x", "zabeel-park"),))
        ids = {s.place_id for d in it.days for s in d.slots}
        assert first not in ids
        assert "zabeel-park" in ids  # swapped-in places are pinned

    def test_explicit_candidates(self, providers: DataProviders) -> None:
        req = _request(days=1)
        only = [providers.places.get_place("etihad-museum")]
        result = schedule(req, pick_hotel(req, providers), providers, candidates=only)  # type: ignore[arg-type]
        acts = [s.place_id for s in result.itinerary.days[0].slots if s.kind is SlotKind.ACTIVITY]
        assert acts == ["etihad-museum"]

    def test_unknown_hotel_area(self, providers: DataProviders) -> None:
        req = example_request()
        hotel = pick_hotel(req, providers).model_copy(update={"neighborhood_id": "nowhere"})
        with pytest.raises(ValueError, match="not a known neighborhood"):
            schedule(req, hotel, providers)
