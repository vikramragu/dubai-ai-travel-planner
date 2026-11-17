"""Scheduler: candidates -> a timed, day-by-day draft itinerary (architecture.md §5.2).

Deterministic, no LLM:

1. **Score** each attraction: ``interest_match x (1 - crowd_penalty) x quality``.
2. **Select and assign to days** by cheapest insertion: every stop goes to the day where it
   adds the least travel, as long as the day's activity + transit time stays within the pace
   limit (relaxed 6 h, balanced 8 h, packed 10 h). Seeds go first so every interest is covered.
3. **Order** each day's stops (nearest neighbour + 2-opt, starting and ending at the hotel);
   beaches and sunset spots move to the end of the day.
4. **Build the timeline** with real legs from the routes provider: breakfast near the hotel,
   stops that respect opening hours (waiting a little if a place opens soon), outdoor stops kept
   out of the midday heat when it's hot, lunch near the stop before it, dinner near the last stop,
   and a leg back to the hotel. A stop that doesn't fit is dropped and reported.

Rule checks (``rule_checks.py``) verify the result independently.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import time
from enum import StrEnum
from functools import cache

from agents.skills.calendar import SEASONAL_MONTHS, TripDay, trip_calendar
from data.dataset import LOCAL_CUISINES
from data.heuristics import crosses_creek, estimate_minutes
from data.providers import AttractionQuery, DataProviders, RestaurantQuery
from schemas import (
    Attraction,
    Avoidance,
    CrowdLevel,
    DayPlan,
    HeatLevel,
    HotelArea,
    HotelTier,
    Interest,
    Itinerary,
    LatLon,
    Leg,
    MealType,
    Neighborhood,
    OpeningHours,
    PriceBand,
    Restaurant,
    Slot,
    SlotKind,
    TransportMode,
    TripConstraints,
    TripRequest,
)

LUNCH_FROM = 12 * 60 + 15
EARLY_LUNCH = 11 * 60 + 30  # earliest lunch, used before a long stop that would skip it
LUNCH_LATEST = 15 * 60
DINNER_AT = 18 * 60 + 30
MIDDAY = (11 * 60, 16 * 60)  # outdoor slots avoid this window on hot days
MAX_WAIT = 60  # minutes we will wait for a place to open
NEARBY_MINUTES = 18  # a day tops up from neighborhoods within this taxi time of its anchor
FILL_MINUTES = 30  # days with time left take good leftovers within this time of their anchor
REPAIR_MINUTES = 35  # a place added for a missing interest must be this close to the day
LONG_LEG_MIN = 60  # rule_checks flags longer legs, so areas further from the hotel are skipped
ONE_PER_DAY = frozenset({"beach", "beach_park", "mall", "water_park", "festival_park"})
LAST_MINUTE = 23 * 60 + 59

MEAL_BANDS = {
    HotelTier.BUDGET: (PriceBand.STREET, PriceBand.CASUAL),
    HotelTier.MID: (PriceBand.STREET, PriceBand.CASUAL, PriceBand.MID),
    HotelTier.LUXURY: (PriceBand.CASUAL, PriceBand.MID, PriceBand.FINE),
}
MEAL_MINUTES = {MealType.BREAKFAST: 40, MealType.LUNCH: 60, MealType.DINNER: 75}


class TransportPolicy(StrEnum):
    FASTEST = "fastest"  # always the quickest option
    BALANCED = "balanced"  # walk short hops; abra/metro when barely slower than a taxi
    PUBLIC_FIRST = "public_first"  # walk, abra and metro whenever reasonable (cheapest)


# How much slower than taxi-time estimates the real legs tend to be, per policy.
_TRANSIT_FACTOR = {
    TransportPolicy.FASTEST: 1.0,
    TransportPolicy.BALANCED: 1.15,
    TransportPolicy.PUBLIC_FIRST: 1.5,
}


@dataclass(frozen=True)
class SchedulerOptions:
    transport: TransportPolicy = TransportPolicy.BALANCED
    meal_bands: tuple[PriceBand, ...] | None = None  # None: by hotel tier
    exclude_ids: frozenset[str] = frozenset()  # attractions never to schedule
    swaps: tuple[tuple[str, str], ...] = ()  # (attraction id, replacement id)
    month: int | None = None  # used when the request has no start_date
    run_id: str = "scripted"
    preferred_restaurants: frozenset[str] = frozenset()  # e.g. the Destination agent's picks


@dataclass
class ScheduleResult:
    itinerary: Itinerary
    unscheduled: list[str] = field(default_factory=list)  # selected but did not fit
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Scored:
    place: Attraction
    score: float


# Dietary needs the restaurant data can answer (cuisine or tags).
SUPPORTED_DIETS = {"vegetarian": {"vegetarian", "vegetarian_options"}}


def suits_diet(r: Restaurant, dietary: Iterable[str]) -> bool:
    """True when the restaurant serves every supported dietary need (others are ignored)."""
    labels = set(r.cuisine) | set(r.tags)
    return all(labels & SUPPORTED_DIETS[d] for d in dietary if d in SUPPORTED_DIETS)


# ------------------------------------------------------------------ scoring
def crowd_penalty(level: CrowdLevel, avoid_crowds: bool) -> float:
    if avoid_crowds:
        return {CrowdLevel.LOW: 0.0, CrowdLevel.MEDIUM: 0.25, CrowdLevel.HIGH: 0.9}[level]
    return {CrowdLevel.LOW: 0.0, CrowdLevel.MEDIUM: 0.1, CrowdLevel.HIGH: 0.3}[level]


def quality(a: Attraction, constraints: TripConstraints, month: int) -> float:
    """A rough desirability factor from the data we have (there are no ratings)."""
    if "seasonal" in a.tags and month not in SEASONAL_MONTHS:
        return 0.0  # closed out of season
    q = 1.0
    if a.typical_duration_min > 240:
        q *= 0.8  # half-day+ places crowd out variety
    if Avoidance.LONG_WALKS in constraints.avoid and "walking" in a.tags:
        q *= 0.6
    return q


def score(a: Attraction, constraints: TripConstraints, month: int) -> float:
    interests = {i.value for i in constraints.interests}
    matched = len(interests & set(a.tags))
    if matched == 0:
        return 0.0
    interest_match = 0.5 + 0.5 * matched / len(interests)
    return round(
        interest_match
        * (1 - crowd_penalty(a.crowd_level, constraints.avoids_crowds))
        * quality(a, constraints, month),
        4,
    )


# ------------------------------------------------------------------ geometry
def taxi_minutes_between(a: LatLon, b: LatLon) -> int:
    return _taxi_minutes_cached(a.lat, a.lon, b.lat, b.lon)


@cache
def _taxi_minutes_cached(alat: float, alon: float, blat: float, blon: float) -> int:
    est = estimate_minutes(
        LatLon(lat=alat, lon=alon), LatLon(lat=blat, lon=blon), TransportMode.TAXI
    )
    return est[0] if est else 0


CREEK_PENALTY = 15  # minutes; makes routes cross Dubai Creek once rather than back and forth


def _hop(a: LatLon, a_hood: str | None, b: LatLon, b_hood: str | None) -> int:
    """Ordering cost of one move: taxi minutes, plus a penalty for crossing the creek."""
    return taxi_minutes_between(a, b) + (CREEK_PENALTY if crosses_creek(a_hood, b_hood) else 0)


def _tour(
    start: LatLon,
    stops: Sequence[_Scored],
    tail: Sequence[_Scored] = (),
    start_hood: str | None = None,
) -> list[_Scored]:
    """Nearest neighbour from the hotel, improved by 2-opt; returns a closed-tour order.

    ``tail`` stops (beaches, sunset spots) are kept at the end, and the rest of the day is
    ordered so that it finishes near them, instead of being reordered and then cut. Crossing
    Dubai Creek costs extra, so a day crosses it as few times as possible.
    """
    tail = list(tail)
    remaining, order, here, here_hood = list(stops), [], start, start_hood
    while remaining:
        nxt = min(
            remaining,
            key=lambda s: (
                _hop(here, here_hood, s.place.location, s.place.neighborhood_id),
                s.place.id,
            ),
        )
        order.append(nxt)
        remaining.remove(nxt)
        here, here_hood = nxt.place.location, nxt.place.neighborhood_id

    def length(seq: Sequence[_Scored]) -> int:
        pts = [
            (start, start_hood),
            *((s.place.location, s.place.neighborhood_id) for s in seq),
            *((s.place.location, s.place.neighborhood_id) for s in tail),
            (start, start_hood),
        ]
        return sum(_hop(p, ph, q, qh) for (p, ph), (q, qh) in itertools.pairwise(pts))

    improved = True
    while improved and len(order) > 1:
        improved = False
        for i in range(len(order) - 1):
            for j in range(i + 1, len(order)):
                candidate = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                if length(candidate) < length(order):
                    order, improved = candidate, True
    return order + tail


def _estimate_day(start: LatLon, stops: Sequence[_Scored], factor: float) -> int:
    """Activity + transit minutes for a day (used before real legs are known)."""
    if not stops:
        return 0
    order = _tour(
        start,
        [s for s in stops if not _late_stop(s.place)],
        [s for s in stops if _late_stop(s.place)],
    )
    pts = [start, *(s.place.location for s in order), start]
    transit = sum(taxi_minutes_between(p, q) for p, q in itertools.pairwise(pts))
    return math.ceil(transit * factor) + sum(s.place.typical_duration_min for s in stops)


def _late_stop(a: Attraction) -> bool:
    """Beaches and sunset spots are best at the end of the day."""
    return not a.indoor and bool({"beach", "sunset"} & set(a.tags))


# ------------------------------------------------------------------ time helpers
def _clock(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)


def _earliest_start(
    hours: OpeningHours | None,
    day: TripDay,
    arrival: int,
    duration: int,
    max_wait: int = MAX_WAIT,
) -> int | None:
    """First start >= arrival (waiting at most ``max_wait``) when the place is open throughout."""
    if arrival + duration > LAST_MINUTE:
        return None
    if hours is None or hours.always_open:
        return arrival  # unknown hours are assumed open (rule checks can't flag them either)
    candidates = [
        arrival,
        *sorted(
            o
            for w in hours.weekly.get(day.weekday, [])
            if arrival < (o := w.opens.hour * 60 + w.opens.minute) <= arrival + max_wait
        ),
    ]
    for start in candidates:
        end = start + duration
        if end <= LAST_MINUTE and hours.is_open(day.weekday, _clock(start), _clock(end)):
            return start
    return None


def _opens_later(a: Attraction, day: TripDay, arrival: int) -> bool:
    """True when the place opens later today (after our arrival plus the normal wait)."""
    hours = a.opening_hours
    if hours is None or hours.always_open:
        return False
    return (
        _earliest_start(hours, day, arrival, a.typical_duration_min, max_wait=LAST_MINUTE)
        is not None
    )


# ------------------------------------------------------------------ the scheduler
class Scheduler:
    def __init__(
        self,
        request: TripRequest,
        hotel: HotelArea,
        providers: DataProviders,
        options: SchedulerOptions | None = None,
    ) -> None:
        self.request = request
        self.c = request.constraints
        self.hotel = hotel
        self.p = providers
        self.o = options or SchedulerOptions()
        self.calendar = trip_calendar(self.c, self.o.month)
        self.month = self.calendar[0].month
        self.hoods: dict[str, Neighborhood] = {h.id: h for h in providers.places.neighborhoods()}
        if hotel.neighborhood_id not in self.hoods:
            raise ValueError(f"hotel area {hotel.neighborhood_id} is not a known neighborhood")
        self.hotel_id = hotel.neighborhood_id
        self.hotel_loc = self.hoods[self.hotel_id].location
        self.capacity = self.c.pace.activity_hours * 60
        self.meal_bands = self.o.meal_bands or MEAL_BANDS[self.c.hotel_tier]
        self.used_restaurants: set[str] = set()
        self.heat = self._heat()
        self.notes: list[str] = []

    def _heat(self) -> dict[int, HeatLevel]:
        days = self.p.weather.forecast(self.calendar[0].date, self.c.days)
        return {i + 1: d.heat for i, d in enumerate(days)}

    # -------------------------------------------------------------- selection
    def candidates(self, given: Iterable[Attraction] | None) -> list[_Scored]:
        if given is None:
            pool = self.p.places.search_attractions(
                AttractionQuery(
                    interests=list(self.c.interests),
                    max_crowd=CrowdLevel.MEDIUM if self.c.avoids_crowds else None,
                    limit=500,
                )
            )
        else:
            pool = list(given)
        swaps = dict(self.o.swaps)
        by_id = {a.id: a for a in pool}
        for old, new in swaps.items():
            replacement = self.p.places.get_place(new)
            if isinstance(replacement, Attraction):
                by_id.pop(old, None)
                by_id[new] = replacement
        scored = [
            _Scored(a, score(a, self.c, self.month))
            for a in by_id.values()
            if a.id not in self.o.exclude_ids
        ]
        pinned = set(swaps.values())
        return sorted(
            (s for s in scored if s.score > 0 or s.place.id in pinned),
            key=lambda s: (-(s.place.id in pinned), -s.score, s.place.id),
        )

    def _fill(
        self, day: list[_Scored], candidates: Iterable[_Scored], budget: int, factor: float
    ) -> list[_Scored]:
        """Add candidates (best first) while the day stays within the time budget."""
        day = list(day)
        for x in sorted(candidates, key=lambda c: (-c.score, c.place.id)):
            if x in day:
                continue
            if x.place.category in ONE_PER_DAY and any(
                y.place.category in ONE_PER_DAY and _late_stop(y.place) == _late_stop(x.place)
                for y in day
            ):
                continue  # one beach / mall / water park per day
            if _estimate_day(self.hotel_loc, [*day, x], factor) <= budget:
                day.append(x)
        return day

    def assign(self, scored: list[_Scored]) -> list[list[_Scored]]:
        """Cluster-first: each day takes the best neighborhood, then nearby ones fill gaps."""
        factor = _TRANSIT_FACTOR[self.o.transport]
        budget = self.capacity - 20  # headroom: meal hops and real legs vary
        pinned = {new for _, new in self.o.swaps}
        interests = [i.value for i in self.c.interests if i is not Interest.FOOD]
        remaining = list(scored)
        covered: set[str] = set()
        days: list[list[_Scored]] = []
        for _ in range(self.c.days):
            clusters: dict[str, list[_Scored]] = {}
            for x in remaining:
                if taxi_minutes_between(self.hotel_loc, x.place.location) > LONG_LEG_MIN:
                    continue  # e.g. Hatta from the city: every leg would break the long-leg rule
                clusters.setdefault(x.place.neighborhood_id, []).append(x)
            if not clusters:
                days.append([])
                continue

            def value(
                hood: str,
                clusters: dict[str, list[_Scored]] = clusters,
                covered: set[str] = covered,
            ) -> float:
                fill = self._fill([], clusters[hood], budget, factor)
                new = {i for i in interests if any(i in x.place.tags for x in fill)} - covered
                v = sum(x.score for x in fill) * (1 + 0.5 * len(new))
                return v + (100 if any(x.place.id in pinned for x in fill) else 0)

            anchor = max(sorted(clusters), key=value)
            day = self._fill([], clusters[anchor], budget, factor)
            here = self.hoods[anchor].location
            nearby = sorted(
                (h for h in clusters if h != anchor),
                key=lambda h: (taxi_minutes_between(here, self.hoods[h].location), h),
            )
            for h in nearby:
                if taxi_minutes_between(here, self.hoods[h].location) > NEARBY_MINUTES:
                    break
                day = self._fill(day, clusters[h], budget, factor)
            days.append(day)
            covered |= {i for i in interests if any(i in x.place.tags for x in day)}
            remaining = [x for x in remaining if x not in day]
        # Coverage repair: every interest gets at least one stop. The best place for a missing
        # interest goes into the nearest day, displacing that day's weakest stop if needed.
        for interest in interests:
            if any(interest in x.place.tags for d in days for x in d):
                continue
            options = [
                x
                for x in remaining
                if interest in x.place.tags
                and taxi_minutes_between(self.hotel_loc, x.place.location) <= LONG_LEG_MIN
            ]
            used = [d for d in range(len(days)) if days[d]]
            # The best (place, day) pair that's close: a top beach an hour away is no use.
            pairs = [
                (x.score - minutes / 30, -minutes, x.place.id, x, d)
                for x in options
                for d in used
                if (minutes := taxi_minutes_between(days[d][0].place.location, x.place.location))
                <= REPAIR_MINUTES
            ]
            if not pairs:
                continue
            *_, best, target = max(pairs, key=lambda p: p[:3])
            day = [*days[target], best]
            removed: list[_Scored] = []
            while len(day) > 1 and _estimate_day(self.hotel_loc, day, factor) > budget:
                # Drop the weakest stop, preferring ones whose interests stay covered.
                rest = [x for d, stops in enumerate(days) if d != target for x in stops]

                def still_covered(
                    x: _Scored, day: list[_Scored] = day, rest: list[_Scored] = rest
                ) -> bool:
                    kept = [y for y in [*rest, *day] if y is not x]
                    return all(
                        any(i in y.place.tags for y in kept) for i in interests if i in x.place.tags
                    )

                others = [x for x in day if x is not best]
                weakest = min(others, key=lambda x: (not still_covered(x), x.score, x.place.id))
                day.remove(weakest)
                removed.append(weakest)
            if _estimate_day(self.hotel_loc, day, factor) <= budget:  # commit only if it fits
                days[target] = day
                remaining = [x for x in remaining if x is not best] + removed
        # Fill pass: days with time left take the best leftovers close to their anchor, so
        # good places aren't dropped while afternoons stay empty.
        leftovers = [
            x
            for x in remaining
            if taxi_minutes_between(self.hotel_loc, x.place.location) <= LONG_LEG_MIN
        ]
        for d, day in enumerate(days):
            if not day or not leftovers:
                continue
            first = day[0].place.location
            near = [
                x
                for x in leftovers
                if taxi_minutes_between(first, x.place.location) <= FILL_MINUTES
            ]
            filled = self._fill(day, near, budget, factor)
            leftovers = [x for x in leftovers if x not in filled]
            days[d] = filled
        return days

    # -------------------------------------------------------------- legs and meals
    def _leg(self, from_id: str, to_id: str, day: int) -> Leg | None:
        if from_id == to_id:
            return None
        walk_max = 20 if self.o.transport is TransportPolicy.PUBLIC_FIRST else 15
        if Avoidance.LONG_WALKS in self.c.avoid or self.heat[day] in (
            HeatLevel.HOT,
            HeatLevel.EXTREME,
        ):
            walk_max = min(walk_max, 10)
        options: dict[TransportMode, Leg] = {}
        for mode in (
            TransportMode.WALK,
            TransportMode.ABRA,
            TransportMode.METRO,
            TransportMode.TAXI,
        ):
            leg = self.p.routes.travel(from_id, to_id, mode, party_size=self.c.travelers)
            if leg is not None:
                options[mode] = leg
        walk = options.pop(TransportMode.WALK, None)
        if walk is not None and walk.duration_min.value <= walk_max:
            options[TransportMode.WALK] = walk
        if not options:
            return None
        if self.o.transport is TransportPolicy.FASTEST or TransportMode.TAXI not in options:
            return min(
                options.values(), key=lambda leg: (leg.duration_min.value, leg.cost_aed.value)
            )
        if TransportMode.WALK in options:
            return options[TransportMode.WALK]
        taxi = options[TransportMode.TAXI]
        t = taxi.duration_min.value
        slack = 10 if self.o.transport is TransportPolicy.BALANCED else max(30, t)
        for mode in (TransportMode.ABRA, TransportMode.METRO):
            leg = options.get(mode)
            if leg is not None and leg.duration_min.value <= min(t + slack, 75):
                return leg
        return taxi

    def _restaurant(
        self,
        meal: MealType,
        near: LatLon,
        start: int,
        day: TripDay,
        also_exclude: frozenset[str] = frozenset(),
        here_hood: str | None = None,
    ) -> Restaurant | None:
        """A restaurant for this meal; reuses an earlier day's one rather than skip the meal
        (few places suit some diets), but never one already used today."""
        args = (meal, near, start, day, also_exclude, here_hood)
        return self._find_restaurant(*args, repeats=False) or self._find_restaurant(
            *args, repeats=True
        )

    def _find_restaurant(
        self,
        meal: MealType,
        near: LatLon,
        start: int,
        day: TripDay,
        also_exclude: frozenset[str],
        here_hood: str | None,
        *,
        repeats: bool,
    ) -> Restaurant | None:
        prefer_local = Interest.FOOD in self.c.interests
        preferred = self.o.preferred_restaurants
        # Few places suit some diets, so search further before giving up on the meal.
        radii = (2.0, 5.0, 12.0, 25.0) if self.c.dietary else (2.0, 5.0, 12.0)
        across: Restaurant | None = None  # best option on the other side of the creek
        for radius in radii:
            found = self.p.places.search_restaurants(
                RestaurantQuery(
                    meal_type=meal,
                    near=near,
                    radius_km=radius,
                    price_bands=list(self.meal_bands),
                    exclude_ids=sorted(
                        also_exclude
                        if meal is MealType.BREAKFAST or repeats
                        else self.used_restaurants | also_exclude
                    ),
                    max_crowd=CrowdLevel.MEDIUM if self.c.avoids_crowds else None,
                    limit=20,
                )
            )
            duration = MEAL_MINUTES[meal]
            usable = [
                r
                for r in found
                if _earliest_start(r.opening_hours, day, start, duration) == start
                and suits_diet(r, self.c.dietary)
            ]
            # Stable sorts: preferred picks first, then local food, then the provider's order.
            if prefer_local:
                usable.sort(key=lambda r: not (LOCAL_CUISINES & set(r.cuisine)))
            if preferred:
                usable.sort(key=lambda r: r.id not in preferred)
            # Most important: stay on this side of Dubai Creek (crossing means abra or taxi).
            # A same-side place a little further away beats one just across the water.
            usable.sort(key=lambda r: crosses_creek(here_hood, r.neighborhood_id))
            if usable and not crosses_creek(here_hood, usable[0].neighborhood_id):
                return usable[0]
            if usable and across is None:
                across = usable[0]
        return across

    # -------------------------------------------------------------- the day timeline
    def build_day(self, day: TripDay, stops: list[_Scored]) -> tuple[DayPlan, list[_Scored]]:
        """Build the day; drop the weakest stop and retry until it fits. Returns dropped stops."""
        stops = list(stops)
        dropped: list[_Scored] = []
        snapshot = set(self.used_restaurants)
        while True:
            self.used_restaurants = set(snapshot)
            plan, skipped = self._timeline(day, stops)
            over = plan.minutes_of(SlotKind.ACTIVITY, SlotKind.TRANSIT) > self.capacity
            if not over or not stops:
                return plan, dropped + skipped
            weakest = min(stops, key=lambda s: (s.score, s.place.id))
            stops.remove(weakest)
            dropped.append(weakest)

    def _timeline(self, day: TripDay, stops: list[_Scored]) -> tuple[DayPlan, list[_Scored]]:
        start = 10 * 60 if Avoidance.EARLY_MORNINGS in self.c.avoid else 9 * 60
        latest = 22 * 60 if Avoidance.LATE_NIGHTS in self.c.avoid else 23 * 60 + 30
        hot = self.heat[day.day] in (HeatLevel.HOT, HeatLevel.EXTREME)
        slots: list[Slot] = []
        skipped: list[_Scored] = []
        t, here, here_loc = start, self.hotel_id, self.hotel_loc
        had_lunch = False
        today: set[str] = set()

        def move(to_id: str, loc: LatLon) -> bool:
            nonlocal t, here, here_loc
            leg = self._leg(here, to_id, day.day)
            if leg is not None:
                end = t + max(1, leg.duration_min.value)
                if end > LAST_MINUTE:
                    return False
                title = f"{leg.mode.value.replace('_', ' ').title()} to {self._name(to_id)}"
                slots.append(
                    Slot(
                        start=_clock(t),
                        end=_clock(end),
                        kind=SlotKind.TRANSIT,
                        title=title,
                        leg=leg,
                    )
                )
                t = end
            here, here_loc = to_id, loc
            return True

        def rest_until(when: int, title: str) -> None:
            nonlocal t
            if when - t >= 10:
                slots.append(
                    Slot(start=_clock(t), end=_clock(when), kind=SlotKind.REST, title=title)
                )
            t = max(t, when)

        def meal(kind: MealType, not_before: int) -> bool:
            nonlocal t
            r = self._restaurant(
                kind, here_loc, max(t, not_before), day, frozenset(today), self._hood_of(here)
            )
            if r is None:
                return False
            leg = self._leg(here, r.id, day.day)
            travel = leg.duration_min.value if leg else 0
            rest_until(not_before - travel, "Free time")
            if not move(r.id, r.location):
                return False
            begin = t
            end = begin + MEAL_MINUTES[kind]
            if end > LAST_MINUTE:
                return False
            slots.append(
                Slot(
                    start=_clock(begin),
                    end=_clock(end),
                    kind=SlotKind.MEAL,
                    title=f"{kind.value.title()} at {r.name}",
                    place_id=r.id,
                    notes=", ".join(r.cuisine),
                )
            )
            today.add(r.id)  # never the same restaurant twice in one day
            if kind is not MealType.BREAKFAST:  # but a regular breakfast spot is fine
                self.used_restaurants.add(r.id)
            t = end
            return True

        def evening(pick: _Scored, *, before_dinner: bool) -> bool:
            """Schedule a late-opening stop now if it fits; False to try it later or drop it."""
            nonlocal t
            a = pick.place
            leg = self._leg(here, a.id, day.day)
            arrival = t + (leg.duration_min.value if leg else 0)
            begin = _earliest_start(
                a.opening_hours, day, arrival, a.typical_duration_min, max_wait=LAST_MINUTE
            )
            if begin is None:
                return False
            end = begin + a.typical_duration_min
            if (before_dinner and end > DINNER_AT + 90) or end > latest:
                return False
            travel = arrival - t
            rest_until(begin - travel, "Free time")
            if not move(a.id, a.location):
                return False
            rest_until(begin, f"Wait for {a.name} to open")
            slots.append(
                Slot(
                    start=_clock(t),
                    end=_clock(t + a.typical_duration_min),
                    kind=SlotKind.ACTIVITY,
                    title=a.name,
                    place_id=a.id,
                    notes=", ".join(tag for tag in a.tags if tag != "free")[:120],
                )
            )
            t += a.typical_duration_min
            return True

        later: list[_Scored] = []
        meal(MealType.BREAKFAST, start)
        remaining = list(stops)
        while remaining:
            nxt = remaining[0].place
            # Lunch now if it's lunchtime, or if the next stop would run past lunch hours
            # (e.g. a 3-hour beach visit starting at noon).
            travel = taxi_minutes_between(here_loc, nxt.location)
            runs_past = t + travel + nxt.typical_duration_min > LUNCH_LATEST
            if not had_lunch and (t >= LUNCH_FROM or (t >= EARLY_LUNCH and runs_past)):
                had_lunch = meal(MealType.LUNCH, max(t, EARLY_LUNCH))
            pick = remaining[0]
            if hot:  # keep outdoor stops out of the midday heat when an indoor one can go first
                for s in remaining:
                    arrive = t + taxi_minutes_between(here_loc, s.place.location)
                    if s.place.indoor or not (MIDDAY[0] <= arrive < MIDDAY[1]):
                        pick = s
                        break
            remaining.remove(pick)
            a = pick.place
            leg = self._leg(here, a.id, day.day)
            arrival = t + (leg.duration_min.value if leg else 0)
            begin = _earliest_start(a.opening_hours, day, arrival, a.typical_duration_min)
            if begin is None and _opens_later(a, day, arrival):
                later.append(pick)  # e.g. an evening cruise or show: try it around dinner
                continue
            if begin is None or begin + a.typical_duration_min > latest - 60:
                skipped.append(pick)  # closed today, or no time left before dinner
                continue
            if not move(a.id, a.location):
                skipped.append(pick)
                continue
            rest_until(begin, f"Wait for {a.name} to open")
            end = t + a.typical_duration_min
            slots.append(
                Slot(
                    start=_clock(t),
                    end=_clock(end),
                    kind=SlotKind.ACTIVITY,
                    title=a.name,
                    place_id=a.id,
                    notes=", ".join(tag for tag in a.tags if tag != "free")[:120],
                )
            )
            t = end
        if not had_lunch and t <= LUNCH_LATEST:
            meal(MealType.LUNCH, max(t, LUNCH_FROM))
        # Places that open later (Global Village at 16:00, a dhow cruise at 20:00): before
        # dinner if they open in time, otherwise after it.
        later.sort(key=lambda s: (-s.score, s.place.id))
        after_dinner = []
        for s in later:
            if not evening(s, before_dinner=True):
                after_dinner.append(s)
        meal(MealType.DINNER, max(t, DINNER_AT))
        for s in after_dinner:
            if not evening(s, before_dinner=False):
                skipped.append(s)
        if t < latest:
            move(self.hotel_id, self.hotel_loc)
        if not slots:  # nothing at all fitted: keep the day valid
            slots.append(
                Slot(
                    start=_clock(start),
                    end=_clock(start + 60),
                    kind=SlotKind.REST,
                    title="Free day",
                )
            )
        plan = DayPlan(
            day=day.day,
            theme=self._theme(stops, skipped),
            date=None if day.assumed else day.date,
            base_area=self._base_area(stops, skipped),
            slots=slots,
        )
        return plan, skipped

    # -------------------------------------------------------------- labels
    def _hood_of(self, pid: str) -> str | None:
        if pid in self.hoods:
            return pid
        place = self.p.places.get_place(pid)
        return place.neighborhood_id if place else None

    def _name(self, pid: str) -> str:
        if pid in self.hoods:
            return "the hotel" if pid == self.hotel_id else self.hoods[pid].name
        place = self.p.places.get_place(pid)
        return place.name if place else pid

    def _base_area(self, stops: list[_Scored], skipped: list[_Scored]) -> str:
        kept = [s for s in stops if s not in skipped]
        if not kept:
            return self.hotel_id
        counts: dict[str, int] = {}
        for s in kept:
            counts[s.place.neighborhood_id] = counts.get(s.place.neighborhood_id, 0) + 1
        return max(sorted(counts), key=lambda h: counts[h])

    def _theme(self, stops: list[_Scored], skipped: list[_Scored]) -> str:
        kept = [s for s in stops if s not in skipped]
        if not kept:
            return "Free day near the hotel"
        area = self.hoods[self._base_area(stops, skipped)].name
        wanted = [i.value for i in self.c.interests]
        tags = [t for t in wanted if any(t in s.place.tags for s in kept)]
        return f"{area}: {' & '.join(tags[:2]) or 'sightseeing'}"

    # -------------------------------------------------------------- entry point
    def run(self, given: Iterable[Attraction] | None = None) -> ScheduleResult:
        scored = self.candidates(given)
        days = self.assign(scored)
        factor = _TRANSIT_FACTOR[self.o.transport]
        budget = self.capacity - 20
        plans: list[DayPlan] = []
        carry: list[_Scored] = []  # stops a day couldn't fit, offered to the following days
        for trip_day, stops in zip(self.calendar, days, strict=True):
            stops = list(stops)
            if stops:
                anchor = stops[0].place.location
                for c in sorted(carry, key=lambda x: (-x.score, x.place.id)):
                    near = taxi_minutes_between(anchor, c.place.location) <= FILL_MINUTES
                    if near and _estimate_day(self.hotel_loc, [*stops, c], factor) <= budget:
                        stops.append(c)
                        carry.remove(c)
            late = [s for s in stops if _late_stop(s.place)]
            ordered = _tour(
                self.hotel_loc,
                [s for s in stops if not _late_stop(s.place)],
                late,
                start_hood=self.hotel_id,
            )
            plan, dropped = self.build_day(trip_day, ordered)
            plans.append(plan)
            carry += dropped
        unscheduled = [s.place.id for s in carry]
        if self.calendar[0].assumed:
            self.notes.append(
                f"No start date: assumed a trip starting {self.calendar[0].date:%A %d %B} "
                "for opening hours, weather and hotel prices"
            )
        itinerary = Itinerary(
            run_id=self.o.run_id, request=self.request, hotel=self.hotel, days=plans
        )
        return ScheduleResult(itinerary=itinerary, unscheduled=unscheduled, notes=self.notes)


def schedule(
    request: TripRequest,
    hotel: HotelArea,
    providers: DataProviders,
    *,
    candidates: Iterable[Attraction] | None = None,
    options: SchedulerOptions | None = None,
) -> ScheduleResult:
    """Build a draft itinerary (no budget, no review) for ``request``."""
    return Scheduler(request, hotel, providers, options).run(candidates)
