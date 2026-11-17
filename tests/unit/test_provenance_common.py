"""Provenance, ids, coordinates, prices and opening hours."""

from __future__ import annotations

from datetime import date, time

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import NonNegativeFloat, TypeAdapter, ValidationError

from schemas import (
    LatLon,
    OpeningHours,
    Pace,
    PriceRange,
    Slug,
    Source,
    SourcedValue,
    TimeWindow,
    Weekday,
    estimated,
    from_api,
    verified,
)


class TestSourcedValue:
    def test_helpers_set_source(self) -> None:
        assert verified(3.0, "https://www.rta.ae", date(2026, 11, 1)).source is Source.VERIFIED
        assert from_api(12, "osrm").source is Source.API
        assert estimated(5).source is Source.ESTIMATED

    def test_verified_needs_reference_and_date(self) -> None:
        with pytest.raises(ValidationError, match="reference"):
            SourcedValue[float](value=1.0, source=Source.VERIFIED, reference="x")
        with pytest.raises(ValidationError, match="reference"):
            SourcedValue[float](value=1.0, source=Source.VERIFIED, as_of=date(2026, 1, 1))

    def test_api_needs_reference(self) -> None:
        with pytest.raises(ValidationError):
            SourcedValue[int](value=1, source=Source.API)

    def test_parametrised_type_is_enforced(self) -> None:
        adapter = TypeAdapter(SourcedValue[NonNegativeFloat])
        with pytest.raises(ValidationError):
            adapter.validate_python(estimated(-1.0))

    def test_json_round_trip(self) -> None:
        value = verified(PriceRange.fixed(15), "https://example.org", date(2026, 11, 1))
        dumped = value.model_dump_json()
        assert SourcedValue[PriceRange].model_validate_json(dumped) == value


class TestSlug:
    @pytest.mark.parametrize("good", ["kite-beach", "al_fahidi", "dxb1"])
    def test_accepts(self, good: str) -> None:
        assert TypeAdapter(Slug).validate_python(good) == good

    @pytest.mark.parametrize("bad", ["Kite Beach", "-start", "", "a/b"])
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            TypeAdapter(Slug).validate_python(bad)


class TestLatLon:
    def test_inside_dubai(self) -> None:
        LatLon(lat=25.2048, lon=55.2708)  # downtown
        LatLon(lat=24.80, lon=56.12)  # Hatta

    @pytest.mark.parametrize(("lat", "lon"), [(24.45, 54.37), (51.5, -0.12), (25.2, 57.0)])
    def test_outside_dubai_rejected(self, lat: float, lon: float) -> None:
        with pytest.raises(ValidationError, match="outside Dubai"):
            LatLon(lat=lat, lon=lon)

    def test_distance(self) -> None:
        fahidi = LatLon(lat=25.2637, lon=55.2972)
        kite = LatLon(lat=25.1580, lon=55.2000)
        assert 14 < fahidi.distance_km(kite) < 16
        assert fahidi.distance_km(fahidi) == 0


class TestPriceRange:
    def test_free_and_midpoint(self) -> None:
        assert PriceRange().is_free
        assert PriceRange(min_aed=10, max_aed=30).midpoint == 20

    def test_min_le_max(self) -> None:
        with pytest.raises(ValidationError):
            PriceRange(min_aed=50, max_aed=10)

    def test_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            PriceRange(min_aed=-1, max_aed=5)

    @given(st.floats(0, 10_000), st.floats(0, 10_000))
    def test_midpoint_within_range(self, a: float, b: float) -> None:
        lo, hi = sorted((a, b))
        r = PriceRange(min_aed=lo, max_aed=hi)
        assert lo <= r.midpoint <= hi


class TestOpeningHours:
    def test_window_contains(self) -> None:
        w = TimeWindow(opens=time(9), closes=time(17))
        assert w.contains(time(9), time(17))
        assert not w.contains(time(8, 30), time(10))
        assert not w.contains(time(16), time(18))

    def test_overnight_window(self) -> None:
        w = TimeWindow(opens=time(16), closes=time(1))
        assert w.overnight
        assert w.contains(time(22), time(23, 30))

    def test_empty_window_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TimeWindow(opens=time(9), closes=time(9))

    def test_weekly(self) -> None:
        hours = OpeningHours(weekly={Weekday.MON: [TimeWindow(opens=time(10), closes=time(18))]})
        assert hours.is_open(Weekday.MON, time(11), time(12))
        assert not hours.is_open(Weekday.TUE, time(11), time(12))

    def test_overnight_spills_into_next_day(self) -> None:
        # Open Friday only, 18:00-02:00.
        hours = OpeningHours(weekly={Weekday.FRI: [TimeWindow(opens=time(18), closes=time(2))]})
        assert hours.is_open(Weekday.FRI, time(23), time(23, 59))
        assert hours.is_open(Weekday.SAT, time(0, 30), time(1, 30))  # still Friday's night
        assert not hours.is_open(Weekday.SAT, time(1, 30), time(2, 30))  # past closing
        assert not hours.is_open(Weekday.SAT, time(19), time(20))  # closed on Saturday
        assert not hours.is_open(Weekday.THU, time(0, 30), time(1))  # no Wednesday window

    def test_weekday_previous(self) -> None:
        assert Weekday.MON.previous is Weekday.SUN
        assert Weekday.SAT.previous is Weekday.FRI

    def test_always_open(self) -> None:
        assert OpeningHours(always_open=True).is_open(Weekday.FRI, time(5), time(6))

    def test_needs_windows_or_always_open(self) -> None:
        with pytest.raises(ValidationError):
            OpeningHours()
        with pytest.raises(ValidationError):
            OpeningHours(
                always_open=True,
                weekly={Weekday.MON: [TimeWindow(opens=time(9), closes=time(10))]},
            )

    def test_weekday_from_date(self) -> None:
        assert Weekday.from_date(date(2026, 11, 7)) is Weekday.SAT


def test_pace_activity_hours() -> None:
    assert [p.activity_hours for p in Pace] == [6, 8, 10]
