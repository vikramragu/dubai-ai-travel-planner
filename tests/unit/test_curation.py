"""Opening-hours parsing used when merging curation with the OSM export."""

from __future__ import annotations

from datetime import time

import pytest

from data.scripts.curate import _day_range, curated_hours, osm_hours
from schemas import Weekday


class TestDayRange:
    def test_wraps_around_the_week(self) -> None:
        assert _day_range("sat-thu") == [
            Weekday.SAT,
            Weekday.SUN,
            Weekday.MON,
            Weekday.TUE,
            Weekday.WED,
            Weekday.THU,
        ]

    def test_single_and_daily(self) -> None:
        assert _day_range("fri") == [Weekday.FRI]
        assert len(_day_range("daily")) == 7
        assert _day_range("Fr-Sa") == [Weekday.FRI, Weekday.SAT]


class TestOsmHours:
    def test_always(self) -> None:
        hours = osm_hours("24/7")
        assert hours is not None and hours.always_open
        midnight = osm_hours("Mo-Su 00:00-24:00")
        assert midnight is not None and midnight.always_open

    def test_friday_differs(self) -> None:
        hours = osm_hours("Sa-Th 08:00-19:30; Fr 14:30-19:30")
        assert hours is not None
        assert hours.is_open(Weekday.MON, time(9), time(10))
        assert not hours.is_open(Weekday.FRI, time(9), time(10))
        assert hours.is_open(Weekday.FRI, time(15), time(16))

    def test_closed_days(self) -> None:
        hours = osm_hours("Su-Th 09:00-15:00; Fr-Sa off")
        assert hours is not None
        assert not hours.is_open(Weekday.SAT, time(10), time(11))

    def test_overnight(self) -> None:
        hours = osm_hours("Mo-Su 10:00-2:00")
        assert hours is not None
        assert hours.is_open(Weekday.TUE, time(0, 30), time(1, 30))

    def test_split_shift(self) -> None:
        hours = osm_hours("Mo-Su 12:00-15:00,19:00-23:00")
        assert hours is not None
        assert not hours.is_open(Weekday.MON, time(16), time(17))
        assert hours.is_open(Weekday.MON, time(19), time(20))

    @pytest.mark.parametrize(
        "tag", ["sunrise-sunset", "Mo-Su 07:00-25:00", "Jan-Mar Mo 10:00-12:00", "", None]
    )
    def test_unsupported_returns_none(self, tag: str | None) -> None:
        assert osm_hours(tag) is None


class TestCuratedHours:
    def test_always_and_none(self) -> None:
        hours = curated_hours("always")
        assert hours is not None and hours.always_open
        assert curated_hours(None) is None

    def test_weekly(self) -> None:
        hours = curated_hours({"sat-thu": "10:00-13:00,16:00-22:00", "fri": "16:00-22:00"})
        assert hours is not None
        assert hours.is_open(Weekday.SUN, time(10, 30), time(12))
        assert not hours.is_open(Weekday.FRI, time(10, 30), time(12))
        assert not hours.is_open(Weekday.SUN, time(13, 30), time(14))
