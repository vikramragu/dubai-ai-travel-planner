"""Travel-time heuristics: haversine distance x detour factor / mode speed.

Used both to pre-compute the stored neighborhood travel matrix (``data/scripts/build_matrix.py``)
and as the last-resort fallback in the local routes provider, so the two always agree.
Every value produced here is tagged ``estimated``; Phase 7 replaces taxi and walking times
with OSRM routes and metro times with GTFS where available.
"""

from __future__ import annotations

import math

from schemas.common import LatLon
from schemas.logistics import TransportMode

BASIS = "heuristic: haversine x detour factor / typical mode speed"

# Road and footpath distances are longer than the straight line.
DETOUR = {
    TransportMode.TAXI: 1.35,
    TransportMode.RIDE_HAIL: 1.35,
    TransportMode.BUS: 1.45,
    TransportMode.WALK: 1.25,
    TransportMode.METRO: 1.20,
    TransportMode.TRAM: 1.20,
    TransportMode.ABRA: 1.00,
}

MAX_WALK_KM = 2.5  # straight-line; beyond this walking is not offered (heat, long_walks)

# Metro lines (for the transfer penalty). Interchanges are BurJuman and Union.
METRO_LINE = {
    "Al Ras": "green",
    "Al Ghubaiba": "green",
    "Al Fahidi": "green",
    "Al Jaddaf": "green",
    "Al Rigga": "red",
    "ADCB": "red",
    "Emirates Towers": "red",
    "Burj Khalifa/Dubai Mall": "red",
    "Business Bay": "red",
    "Mall of the Emirates": "red",
    "DMCC": "red",
}

# Abra crossings across Dubai Creek: (neighborhood, neighborhood) -> crossing minutes.
ABRA_CROSSINGS = {
    frozenset({"al-shindagha", "al-ras"}): 6,  # Bur Dubai Old Souk <-> Deira Old Souk
    frozenset({"al-fahidi", "al-ras"}): 8,  # Bur Dubai <-> Al Sabkha
}


# Dubai Creek splits the old city. There is no walking route across it in the old town: people
# cross by abra, metro or taxi (bridges / tunnel). Neighborhoods north-east of the creek:
DEIRA_SIDE = frozenset({"al-ras", "al-rigga", "al-mamzar"})
CREEK_DETOUR_KM = 3.0  # a car crossing in the old town goes via the Shindagha tunnel or a bridge


def crosses_creek(hood_a: str | None, hood_b: str | None) -> bool:
    """True when the two neighborhoods are on opposite banks of Dubai Creek."""
    if hood_a is None or hood_b is None:
        return False
    return (hood_a in DEIRA_SIDE) != (hood_b in DEIRA_SIDE)


def road_km(a: LatLon, b: LatLon, mode: TransportMode) -> float:
    return round(a.distance_km(b) * DETOUR[mode], 2)


# Speed for each stretch of a trip: the first km crawl through city streets, longer trips get
# onto Sheikh Zayed Rd / E311. Applied per stretch, so time always grows with distance.
TAXI_SPEED_BANDS = ((3.0, 22.0), (10.0, 45.0), (30.0, 65.0), (math.inf, 85.0))  # (up to km, km/h)


def taxi_minutes(km: float) -> int:
    """Includes ~5 min to find/hail a cab and drop off."""
    minutes, done = 5.0, 0.0
    for upto, kmh in TAXI_SPEED_BANDS:
        stretch = min(km, upto) - done
        if stretch <= 0:
            break
        minutes += stretch / kmh * 60
        done += stretch
    return math.ceil(minutes)


def walk_minutes(km: float) -> int:
    return math.ceil(km / 4.5 * 60)


def metro_minutes(km: float, line_a: str | None, line_b: str | None) -> int | None:
    """Walk to and from stations (2 x 8 min), wait (4 min), ride at ~32 km/h, transfer 6 min."""
    if line_a is None or line_b is None:
        return None
    transfer = 0 if line_a == line_b else 6
    return math.ceil(16 + 4 + km / 32 * 60 + transfer)


def abra_minutes(hood_a: str, hood_b: str) -> int | None:
    crossing = ABRA_CROSSINGS.get(frozenset({hood_a, hood_b}))
    return None if crossing is None else crossing + 5 + 6  # wait + short walks either side


def estimate_minutes(
    a: LatLon,
    b: LatLon,
    mode: TransportMode,
    *,
    station_a: str | None = None,
    station_b: str | None = None,
    hood_a: str | None = None,
    hood_b: str | None = None,
) -> tuple[int, float] | None:
    """(minutes, km) for one mode, or ``None`` when the mode is not a sensible option."""
    straight = a.distance_km(b)
    if mode in (TransportMode.TAXI, TransportMode.RIDE_HAIL):
        km = road_km(a, b, mode)
        if crosses_creek(hood_a, hood_b) and straight < 5:
            km = round(km + CREEK_DETOUR_KM, 2)
        return taxi_minutes(km), km
    if mode is TransportMode.WALK:
        if straight > MAX_WALK_KM or crosses_creek(hood_a, hood_b):
            return None
        km = road_km(a, b, mode)
        return walk_minutes(km), km
    if mode is TransportMode.METRO:
        if station_a is None or station_b is None:
            return None
        km = road_km(a, b, mode)
        if station_a == station_b:
            return None  # same station: walk or taxi instead
        minutes = metro_minutes(km, METRO_LINE.get(station_a), METRO_LINE.get(station_b))
        return None if minutes is None else (minutes, km)
    if mode is TransportMode.ABRA:
        if hood_a is None or hood_b is None:
            return None
        minutes = abra_minutes(hood_a, hood_b)
        return None if minutes is None else (minutes, round(straight, 2))
    return None  # bus/tram are not modelled offline


def zone_band_for_km(km: float) -> str:
    """Rough nol zone band from distance (Dubai has 7 fare zones). Values of ``ZoneBand``."""
    if km < 6:
        return "1_zone"
    if km < 15:
        return "2_zones"
    return "3_plus_zones"
