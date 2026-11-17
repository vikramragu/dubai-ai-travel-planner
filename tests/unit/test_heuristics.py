"""Travel-time heuristics shared by the stored matrix and the local routes fallback."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from data import heuristics as h
from schemas import LatLon, TransportMode

FAHIDI = LatLon(lat=25.2625, lon=55.3010)
RAS = LatLon(lat=25.2705, lon=55.2985)
MARINA = LatLon(lat=25.0800, lon=55.1405)


def test_walk_only_for_short_distances() -> None:
    assert h.estimate_minutes(FAHIDI, RAS, TransportMode.WALK) is not None
    assert h.estimate_minutes(FAHIDI, MARINA, TransportMode.WALK) is None


def test_metro_needs_stations() -> None:
    assert h.estimate_minutes(FAHIDI, MARINA, TransportMode.METRO) is None
    est = h.estimate_minutes(
        FAHIDI, MARINA, TransportMode.METRO, station_a="Al Fahidi", station_b="DMCC"
    )
    assert est is not None
    same_line = h.estimate_minutes(
        FAHIDI, MARINA, TransportMode.METRO, station_a="Al Rigga", station_b="DMCC"
    )
    assert same_line is not None and same_line[0] < est[0]  # no transfer penalty


def test_abra_only_across_the_creek() -> None:
    assert h.abra_minutes("al-shindagha", "al-ras") is not None
    assert h.abra_minutes("al-ras", "al-shindagha") == h.abra_minutes("al-shindagha", "al-ras")
    assert h.abra_minutes("downtown", "al-ras") is None


def test_zone_bands() -> None:
    assert [h.zone_band_for_km(k) for k in (2, 10, 30)] == ["1_zone", "2_zones", "3_plus_zones"]


@given(st.floats(0.1, 150), st.floats(0.1, 150))
def test_taxi_time_grows_with_distance(a: float, b: float) -> None:
    lo, hi = sorted((a, b))
    assert h.taxi_minutes(lo) <= h.taxi_minutes(hi)


def test_taxi_detour() -> None:
    est = h.estimate_minutes(FAHIDI, MARINA, TransportMode.TAXI)
    assert est is not None
    minutes, km = est
    assert km > FAHIDI.distance_km(MARINA)
    assert 20 <= minutes <= 45
