"""Deterministic review rules (architecture.md §5.4). They run before the LLM reviewer and are
authoritative: the LLM may add qualitative issues but can't clear a failed blocker.

| Rule (code)                        | Severity | Routed to   |
|------------------------------------|----------|-------------|
| over_budget                        | blocker  | budget      |
| hotel_cost_driver (> 50% of budget)| major    | logistics   |
| day_overloaded (activity+transit)  | blocker  | assemble    |
| outside_opening_hours              | blocker  | assemble    |
| closed_for_season                  | blocker  | destination |
| unknown_place (not in the dataset) | blocker  | destination |
| long_leg (> 60 min)                | major    | logistics   |
| day_transit_heavy (> 150 min)      | major    | logistics (hotel legs) or assemble |
| duplicate_place (attraction)       | major    | destination |
| crowded_place ("avoid crowds")     | major    | destination |
| interest_missing                   | major    | destination |
| missing_meal (no lunch or dinner)  | minor    | assemble    |
| missing_leg (moved without a leg)  | minor    | assemble    |
| repeated_restaurant                | minor    | assemble    |
| budget_missing                     | minor    | budget      |
| outdoor_midday_heat                | minor    | assemble    |
| dietary_not_met                    | minor    | assemble    |
| early_start / late_finish          | minor    | assemble    |
"""

from __future__ import annotations

from collections import Counter
from datetime import time

from agents.skills.calendar import SEASONAL_MONTHS, trip_calendar
from agents.skills.scheduler import LONG_LEG_MIN, suits_diet, taxi_minutes_between
from data.dataset import LOCAL_CUISINES
from data.providers import DataProviders
from schemas import (
    Attraction,
    Avoidance,
    CrowdLevel,
    HeatLevel,
    Interest,
    Itinerary,
    MealType,
    Restaurant,
    ReviewIssue,
    ReviewReport,
    RouteTarget,
    Severity,
    SlotKind,
)

MAX_LEG_MIN = LONG_LEG_MIN
MAX_DAY_TRANSIT_MIN = 150
HOT_HEAT = (HeatLevel.HOT, HeatLevel.EXTREME)
MIDDAY = (11 * 60, 16 * 60)
HOTEL_SHARE_LIMIT = 0.5
PENALTY = {Severity.BLOCKER: 30, Severity.MAJOR: 12, Severity.MINOR: 3}


def _issue(
    code: str,
    severity: Severity,
    route: RouteTarget,
    message: str,
    *,
    day: int | None = None,
    place_id: str | None = None,
) -> ReviewIssue:
    return ReviewIssue(
        code=code, severity=severity, message=message, route_to=route, day=day, place_id=place_id
    )


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def meal_of(start: time) -> MealType:
    """Which meal a meal slot is, by its start time (titles may be rewritten by the LLM)."""
    m = _minutes(start)
    if m < 11 * 60:
        return MealType.BREAKFAST
    if m < 16 * 60 + 30:
        return MealType.LUNCH
    return MealType.DINNER


FAR_HOTEL_MIN = 25  # taxi minutes from the hotel area to a day's area


def check_itinerary(
    itinerary: Itinerary, providers: DataProviders, *, month: int | None = None
) -> list[ReviewIssue]:
    """Run every rule; ``month`` is used when the request has no start date."""
    c = itinerary.request.constraints
    calendar = trip_calendar(c, month)
    weather = providers.weather.forecast(calendar[0].date, c.days)
    limit = c.pace.activity_hours * 60
    issues: list[ReviewIssue] = []
    hood_loc = {h.id: h.location for h in providers.places.neighborhoods()}

    def _hotel_minutes(a: str, b: str) -> int:
        if a not in hood_loc or b not in hood_loc:
            return 0
        return taxi_minutes_between(hood_loc[a], hood_loc[b])

    places: dict[str, Attraction | Restaurant] = {}

    def place(pid: str) -> Attraction | Restaurant | None:
        if pid not in places:
            found = providers.places.get_place(pid)
            if found is None:
                return None
            places[pid] = found
        return places[pid]

    # ---------------------------------------------------------------- budget
    b = itinerary.budget
    if b is None:
        issues.append(_issue("budget_missing", Severity.MINOR, RouteTarget.BUDGET, "No budget yet"))
    else:
        if not b.within_budget:
            issues.append(
                _issue(
                    "over_budget",
                    Severity.BLOCKER,
                    RouteTarget.BUDGET,
                    f"Total AED {b.total:,.0f} is over the AED {b.budget_aed:,.0f} budget "
                    f"by AED {-b.remaining_aed:,.0f}",
                )
            )
            if b.accommodation > HOTEL_SHARE_LIMIT * b.budget_aed:
                issues.append(
                    _issue(
                        "hotel_cost_driver",
                        Severity.MAJOR,
                        RouteTarget.LOGISTICS,
                        f"Accommodation (AED {b.accommodation:,.0f}) is more than half the budget",
                    )
                )

    # ---------------------------------------------------------------- per day
    seen_attractions: dict[str, int] = {}
    restaurant_visits: Counter[str] = Counter()
    for day, trip_day in zip(itinerary.days, calendar, strict=True):
        heat = weather[day.day - 1].heat
        busy = day.minutes_of(SlotKind.ACTIVITY, SlotKind.TRANSIT)
        if busy > limit:
            issues.append(
                _issue(
                    "day_overloaded",
                    Severity.BLOCKER,
                    RouteTarget.ASSEMBLE,
                    f"Day {day.day} has {busy} min of activities and travel; "
                    f"the {c.pace.value} pace allows {limit}",
                    day=day.day,
                )
            )
        transit = day.minutes_of(SlotKind.TRANSIT)
        if transit > MAX_DAY_TRANSIT_MIN:
            # A day far from the hotel area is the hotel's problem, not the day's.
            far_hotel = (
                _hotel_minutes(itinerary.hotel.neighborhood_id, day.base_area) >= FAR_HOTEL_MIN
            )
            issues.append(
                _issue(
                    "day_transit_heavy",
                    Severity.MAJOR,
                    RouteTarget.LOGISTICS if far_hotel else RouteTarget.ASSEMBLE,
                    f"Day {day.day} spends {transit} min travelling"
                    + (f"; {day.base_area} is far from the hotel area" if far_hotel else ""),
                    day=day.day,
                )
            )
        meals = set()
        prev_place: str | None = None
        for slot in day.slots:
            if slot.kind is SlotKind.TRANSIT and slot.leg is not None:
                if slot.leg.duration_min.value > MAX_LEG_MIN:
                    issues.append(
                        _issue(
                            "long_leg",
                            Severity.MAJOR,
                            RouteTarget.LOGISTICS,
                            f"Day {day.day}: {slot.title} takes {slot.leg.duration_min.value} min",
                            day=day.day,
                        )
                    )
                prev_place = None  # we moved
                continue
            if slot.place_id is None:
                continue
            if prev_place is not None and prev_place != slot.place_id:
                issues.append(
                    _issue(
                        "missing_leg",
                        Severity.MINOR,
                        RouteTarget.ASSEMBLE,
                        f"Day {day.day}: no travel between {prev_place} and {slot.place_id}",
                        day=day.day,
                        place_id=slot.place_id,
                    )
                )
            prev_place = slot.place_id
            p = place(slot.place_id)
            if p is None:
                issues.append(
                    _issue(
                        "unknown_place",
                        Severity.BLOCKER,
                        RouteTarget.DESTINATION,
                        f"Day {day.day}: '{slot.place_id}' is not in the dataset",
                        day=day.day,
                        place_id=slot.place_id,
                    )
                )
                continue
            hours = p.opening_hours
            if hours is not None and not hours.is_open(trip_day.weekday, slot.start, slot.end):
                issues.append(
                    _issue(
                        "outside_opening_hours",
                        Severity.BLOCKER,
                        RouteTarget.ASSEMBLE,
                        f"Day {day.day}: {p.name} is not open {slot.start:%H:%M}-{slot.end:%H:%M} "
                        f"on {trip_day.weekday.value.title()}",
                        day=day.day,
                        place_id=p.id,
                    )
                )
            if c.avoids_crowds and p.crowd_level is CrowdLevel.HIGH:
                issues.append(
                    _issue(
                        "crowded_place",
                        Severity.MAJOR,
                        RouteTarget.DESTINATION,
                        f"Day {day.day}: {p.name} is usually crowded, but the traveller "
                        "wants to avoid crowds",
                        day=day.day,
                        place_id=p.id,
                    )
                )
            if isinstance(p, Attraction):
                if "seasonal" in p.tags and trip_day.month not in SEASONAL_MONTHS:
                    issues.append(
                        _issue(
                            "closed_for_season",
                            Severity.BLOCKER,
                            RouteTarget.DESTINATION,
                            f"Day {day.day}: {p.name} only opens October-May",
                            day=day.day,
                            place_id=p.id,
                        )
                    )
                if p.id in seen_attractions:
                    issues.append(
                        _issue(
                            "duplicate_place",
                            Severity.MAJOR,
                            RouteTarget.DESTINATION,
                            f"{p.name} appears on day {seen_attractions[p.id]} and day {day.day}",
                            day=day.day,
                            place_id=p.id,
                        )
                    )
                seen_attractions.setdefault(p.id, day.day)
                start, end = _minutes(slot.start), _minutes(slot.end)
                if heat in HOT_HEAT and not p.indoor and start < MIDDAY[1] and end > MIDDAY[0]:
                    issues.append(
                        _issue(
                            "outdoor_midday_heat",
                            Severity.MINOR,
                            RouteTarget.ASSEMBLE,
                            f"Day {day.day}: {p.name} is outdoors at midday ({heat.value} heat); "
                            "move it to the morning or after 16:00",
                            day=day.day,
                            place_id=p.id,
                        )
                    )
            elif slot.kind is SlotKind.MEAL:
                if not suits_diet(p, c.dietary):
                    issues.append(
                        _issue(
                            "dietary_not_met",
                            Severity.MINOR,
                            RouteTarget.ASSEMBLE,
                            f"Day {day.day}: {p.name} may not suit a {', '.join(c.dietary)} diet",
                            day=day.day,
                            place_id=p.id,
                        )
                    )
                kind = meal_of(slot.start)
                meals.add(kind)
                if kind is not MealType.BREAKFAST:  # a regular breakfast spot is fine
                    restaurant_visits[p.id] += 1
        for m in (MealType.LUNCH, MealType.DINNER):
            if m not in meals:
                issues.append(
                    _issue(
                        "missing_meal",
                        Severity.MINOR,
                        RouteTarget.ASSEMBLE,
                        f"Day {day.day} has no {m.value}",
                        day=day.day,
                    )
                )
        timed = [s for s in day.slots if s.kind is not SlotKind.REST]
        if timed:
            if Avoidance.EARLY_MORNINGS in c.avoid and _minutes(timed[0].start) < 9 * 60:
                issues.append(
                    _issue(
                        "early_start",
                        Severity.MINOR,
                        RouteTarget.ASSEMBLE,
                        f"Day {day.day} starts at {timed[0].start:%H:%M}",
                        day=day.day,
                    )
                )
            if Avoidance.LATE_NIGHTS in c.avoid and _minutes(timed[-1].end) > 22 * 60:
                issues.append(
                    _issue(
                        "late_finish",
                        Severity.MINOR,
                        RouteTarget.ASSEMBLE,
                        f"Day {day.day} ends at {timed[-1].end:%H:%M}",
                        day=day.day,
                    )
                )

    for pid, n in sorted(restaurant_visits.items()):
        if n > 1:
            issues.append(
                _issue(
                    "repeated_restaurant",
                    Severity.MINOR,
                    RouteTarget.ASSEMBLE,
                    f"{places[pid].name} is used for {n} meals",
                    place_id=pid,
                )
            )

    # ---------------------------------------------------------------- interests
    visited = [p for p in places.values() if isinstance(p, Attraction)]
    eaten = [p for p in places.values() if isinstance(p, Restaurant)]
    for interest in c.interests:
        if interest is Interest.FOOD:
            ok = any(LOCAL_CUISINES & set(r.cuisine) for r in eaten) or any(
                "food" in a.tags for a in visited
            )
        else:
            ok = any(interest.value in a.tags for a in visited)
        if not ok:
            issues.append(
                _issue(
                    "interest_missing",
                    Severity.MAJOR,
                    RouteTarget.DESTINATION,
                    f"Nothing in the plan matches the '{interest.value}' interest",
                )
            )
    return issues


def score(issues: list[ReviewIssue]) -> float:
    """100 minus a penalty per issue (blocker 30, major 12, minor 3), floored at 0."""
    return float(max(0, 100 - sum(PENALTY[i.severity] for i in issues)))


def review_from_rules(
    itinerary: Itinerary, providers: DataProviders, *, month: int | None = None
) -> ReviewReport:
    issues = check_itinerary(itinerary, providers, month=month)
    counts = Counter(i.severity.value for i in issues)
    summary = (
        "Rule checks: "
        + ", ".join(f"{counts[s.value]} {s.value}" for s in Severity if counts[s.value])
        if issues
        else "Rule checks: no issues"
    )
    return ReviewReport.from_issues(issues, score=score(issues), summary=summary)
