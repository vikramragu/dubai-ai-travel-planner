"""Plan quality fixes found by running the agents (2026-11-17):

- no walking across Dubai Creek, and cross-creek taxis pay the tunnel/bridge detour;
- a day crosses the creek at most twice and meals stay on the hotel's side;
- beach/sunset stops stay last while the rest of the day is ordered to finish near them;
- evening-only places are scheduled around dinner;
- lunch happens before a long stop that would run past lunch hours;
- swap descriptions separate the ticket saving from the re-planned saving;
- heavy-travel days caused by a far hotel are routed to Logistics, which then avoids it.
"""

from __future__ import annotations

from datetime import time

import pytest

from agents.base import AgentContext
from agents.logistics import LogisticsAgent
from agents.skills.budget_engine import PlanInputs, generate_alternatives, plan_trip
from agents.skills.rule_checks import check_itinerary
from agents.skills.scheduler import _Scored, _tour, schedule
from agents.skills.scripted import example_request, pick_hotel
from data import heuristics as h
from data.providers import DataProviders
from graph.nodes import detours as _detours
from schemas import (
    Attraction,
    HotelTier,
    Interest,
    Itinerary,
    LatLon,
    ReviewIssue,
    RouteTarget,
    Severity,
    SlotKind,
    TransportMode,
    TripRequest,
)

FAHIDI = LatLon(lat=25.2625, lon=55.3010)
RAS = LatLon(lat=25.2705, lon=55.2985)


def _req(**changes: object) -> TripRequest:
    r = example_request()
    return r.model_copy(update={"constraints": r.constraints.model_copy(update=changes)})


def _hood(providers: DataProviders, pid: str) -> str:
    place = providers.places.get_place(pid)
    return place.neighborhood_id if place else pid


@pytest.fixture(scope="module")
def plan(providers: DataProviders) -> Itinerary:
    req = example_request()
    return schedule(req, pick_hotel(req, providers), providers).itinerary


class TestCreek:
    def test_sides(self) -> None:
        assert h.crosses_creek("al-fahidi", "al-ras")
        assert not h.crosses_creek("al-ras", "al-rigga")
        assert not h.crosses_creek("al-fahidi", None)

    def test_no_walk_and_a_taxi_detour(self) -> None:
        assert (
            h.estimate_minutes(FAHIDI, RAS, TransportMode.WALK, hood_a="al-fahidi", hood_b="al-ras")
            is None
        )
        assert h.estimate_minutes(FAHIDI, RAS, TransportMode.WALK) is not None  # unknown sides
        across = h.estimate_minutes(
            FAHIDI, RAS, TransportMode.TAXI, hood_a="al-fahidi", hood_b="al-ras"
        )
        plain = h.estimate_minutes(FAHIDI, RAS, TransportMode.TAXI)
        assert across is not None and plain is not None
        assert across[1] == pytest.approx(plain[1] + h.CREEK_DETOUR_KM)

    def test_plans_respect_the_creek(self, plan: Itinerary, providers: DataProviders) -> None:
        for day in plan.days:
            crossings = 0
            for s in day.slots:
                if s.leg is None:
                    continue
                if h.crosses_creek(_hood(providers, s.leg.from_id), _hood(providers, s.leg.to_id)):
                    crossings += 1
                    assert s.leg.mode is not TransportMode.WALK
            assert crossings <= 2, day.theme

    def test_breakfast_on_the_hotels_side(self, plan: Itinerary, providers: DataProviders) -> None:
        hotel = plan.hotel.neighborhood_id
        for day in plan.days:
            breakfast = day.slots[1]
            assert breakfast.kind is SlotKind.MEAL and breakfast.place_id
            assert not h.crosses_creek(hotel, _hood(providers, breakfast.place_id))


def test_beach_stays_last_and_the_day_ends_near_it(providers: DataProviders) -> None:
    ids = ["kite-beach", "etihad-museum", "al-shindagha-museum", "jumeirah-mosque"]
    stops = []
    for i in ids:
        a = providers.places.get_place(i)
        assert isinstance(a, Attraction)
        stops.append(_Scored(a, 1.0))
    beach = stops[0]
    order = _tour(RAS, stops[1:], [beach], start_hood="al-ras")
    assert order[-1] is beach
    assert {s.place.id for s in order} == set(ids)
    # the stop before the beach is the Jumeirah one, not the old town
    assert order[-2].place.neighborhood_id == "jumeirah"


def test_evening_places_around_dinner(providers: DataProviders) -> None:
    req = _req(interests=[Interest.NIGHTLIFE, Interest.CULTURE], avoid=[], days=2)
    it = schedule(req, pick_hotel(req, providers), providers).itinerary
    slots = [s for d in it.days for s in d.slots]
    cruise = next((s for s in slots if s.place_id == "creek-dhow-dinner-cruise"), None)
    assert cruise is not None and cruise.start >= time(20)
    assert "outside_opening_hours" not in {i.code for i in check_itinerary(it, providers)}


def test_lunch_before_a_long_stop(plan: Itinerary) -> None:
    for day in plan.days:
        lunch = [s for s in day.slots if s.kind is SlotKind.MEAL and time(11) <= s.start < time(16)]
        assert lunch, f"day {day.day} has no lunch"


def test_swap_descriptions_are_honest(providers: DataProviders) -> None:
    req = _req(hotel_tier=HotelTier.MID, budget_aed=3000)
    p = plan_trip(req, PlanInputs(hotel=pick_hotel(req, providers)), providers)
    for alt, _ in generate_alternatives(p, req, providers):
        if alt.kind.value == "free_attraction" and "in tickets" in alt.description:
            assert f"AED {alt.saving_aed:,.0f} less overall" in alt.description


class TestFarHotel:
    @staticmethod
    def _stretched(it: Itinerary) -> Itinerary:
        """Make day 1's last leg (back to the hotel) 160 min, so the day exceeds 2.5 h."""
        day = it.days[0]
        last = day.slots[-1]
        assert last.kind is SlotKind.TRANSIT
        start = last.start.hour * 60 + last.start.minute
        long = last.model_copy(
            update={"end": time(min(23, (start + 160) // 60), (start + 160) % 60)}
        )
        days = [day.model_copy(update={"slots": [*day.slots[:-1], long]}), *it.days[1:]]
        return it.model_copy(update={"days": days})

    @pytest.mark.parametrize(
        ("area", "route"), [("al-barsha", RouteTarget.LOGISTICS), ("al-ras", RouteTarget.ASSEMBLE)]
    )
    def test_routing(self, providers: DataProviders, area: str, route: RouteTarget) -> None:
        req = _req(days=2)
        hotel = next(
            a for a in providers.hotels.areas(HotelTier.BUDGET) if a.neighborhood_id == area
        )
        it = self._stretched(schedule(req, hotel, providers).itinerary)
        base = it.days[0].base_area
        issue = next(i for i in check_itinerary(it, providers) if i.code == "day_transit_heavy")
        if area == "al-barsha":
            assert base in {"al-fahidi", "al-ras", "al-shindagha", "al-rigga"}  # old town day
        assert issue.route_to is route

    def test_blamed_area_goes_last(self, providers: DataProviders) -> None:
        req = example_request()
        agent = LogisticsAgent(AgentContext(providers=providers))
        first = agent.plan(req).recommended_hotel.neighborhood_id
        issue = ReviewIssue(
            code="day_transit_heavy",
            severity=Severity.MAJOR,
            message="m",
            route_to=RouteTarget.LOGISTICS,
        )
        again = agent.plan(req, issues=[issue], current_area=first)
        assert again.recommended_hotel.neighborhood_id != first
        assert again.hotel_areas[-1].neighborhood_id == first


def test_detours_pick_the_longest_leg(plan: Itinerary) -> None:
    issue = ReviewIssue(
        code="day_transit_heavy",
        severity=Severity.MAJOR,
        message="m",
        route_to=RouteTarget.ASSEMBLE,
        day=1,
    )
    dropped = _detours(plan, [issue])
    assert len(dropped) == 1
    day = plan.days[0]
    pairs = [
        (a.leg.duration_min.value, b.place_id)
        for a, b in zip(day.slots, day.slots[1:], strict=False)
        if a.leg and b.kind is SlotKind.ACTIVITY
    ]
    assert dropped == {max(pairs)[1]}
    assert _detours(plan, []) == frozenset()
