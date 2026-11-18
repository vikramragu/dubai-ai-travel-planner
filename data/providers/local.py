"""Local provider: every data need served from the curated SQLite database (always available).

Search filters run in SQL on indexed columns; full records are read back from the ``data``
JSON column through the Phase 1 models. Travel between two neighborhoods uses the stored
matrix; anything else falls back to the haversine heuristic in ``data/heuristics.py``.
"""

from __future__ import annotations

import math
import sqlite3
from calendar import monthrange
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

from data import heuristics
from data.providers.base import AttractionQuery, ProviderError, RestaurantQuery, UnknownPlace
from schemas import (
    Attraction,
    ClimateMonth,
    CrowdLevel,
    DayWeather,
    HotelArea,
    HotelFees,
    HotelSeason,
    HotelTier,
    LatLon,
    Leg,
    Neighborhood,
    Restaurant,
    Source,
    SourcedValue,
    TaxiKind,
    TaxiTariff,
    TransitFare,
    TransportMode,
    TravelMatrix,
    TravelMatrixEntry,
    estimated,
)
from schemas.pricing import FareProduct, ZoneBand

CROWD_RANK = {CrowdLevel.LOW: 0, CrowdLevel.MEDIUM: 1, CrowdLevel.HIGH: 2}
TAXI_SEATS = 4
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class _Point(NamedTuple):
    location: LatLon
    neighborhood_id: str


class LocalProvider:
    """Implements PlacesProvider, RoutesProvider, WeatherProvider, HotelProvider and
    PricingProvider over ``dubai.sqlite``."""

    name = "local"

    def __init__(self, db_path: Path) -> None:
        if not db_path.exists():
            raise ProviderError(
                f"{db_path} not found; build it with `uv run python -m data.build_db`"
            )
        uri = f"file:{db_path.resolve()}?mode=ro"
        # Read-only and shareable across threads (the UI runs the graph in a worker thread).
        self._con = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._has_roads = bool(
            self._rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name='road_times'")
        )
        as_of = self._rows("SELECT value FROM meta WHERE key = 'road_times_as_of'")
        self._road_basis = heuristics.ROAD_BASIS + (
            f", routed {as_of[0][0]}" if as_of and as_of[0][0] else ""
        )

    def close(self) -> None:
        self._con.close()

    def _rows(self, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
        return self._con.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ places
    def neighborhoods(self) -> list[Neighborhood]:
        return [
            Neighborhood.model_validate_json(str(r[0]))
            for r in self._rows("SELECT data FROM neighborhoods ORDER BY id")
        ]

    def search_attractions(self, query: AttractionQuery) -> list[Attraction]:
        wanted = [*(i.value for i in query.interests), *query.tags]
        where: list[str] = ["1=1"]
        params: list[object] = []
        if wanted:
            where.append(
                f"EXISTS (SELECT 1 FROM attraction_tags t WHERE t.attraction_id = a.id "
                f"AND t.tag IN ({','.join('?' * len(wanted))}))"
            )
            params += wanted
        self._common_filters(where, params, query.neighborhood_ids, query.max_crowd, "a")
        if query.free_only:
            where.append("a.price_max = 0")
        if query.indoor_only:
            where.append("a.indoor = 1")
        if query.exclude_ids:
            where.append(f"a.id NOT IN ({','.join('?' * len(query.exclude_ids))})")
            params += query.exclude_ids
        score_sql = (
            f"(SELECT COUNT(*) FROM attraction_tags t WHERE t.attraction_id = a.id "
            f"AND t.tag IN ({','.join('?' * len(wanted))}))"
            if wanted
            else "0"
        )
        rows = self._rows(
            f"SELECT a.data, a.lat, a.lon, a.crowd_rank, {score_sql} FROM attractions a "
            f"WHERE {' AND '.join(where)}",
            [*wanted, *params],
        )
        ranked = self._rank(rows, query.near, query.radius_km)
        return [Attraction.model_validate_json(d) for d in ranked[: query.limit]]

    def search_restaurants(self, query: RestaurantQuery) -> list[Restaurant]:
        where: list[str] = ["1=1"]
        params: list[object] = []
        cuisines = [c.lower() for c in query.cuisines]
        if cuisines:
            where.append(
                f"EXISTS (SELECT 1 FROM restaurant_cuisines c WHERE c.restaurant_id = r.id "
                f"AND c.cuisine IN ({','.join('?' * len(cuisines))}))"
            )
            params += cuisines
        if query.price_bands:
            where.append(f"r.price_band IN ({','.join('?' * len(query.price_bands))})")
            params += [b.value for b in query.price_bands]
        if query.meal_type is not None:
            where.append(
                "EXISTS (SELECT 1 FROM restaurant_meals m WHERE m.restaurant_id = r.id "
                "AND m.meal = ?)"
            )
            params.append(query.meal_type.value)
        self._common_filters(where, params, query.neighborhood_ids, query.max_crowd, "r")
        if query.exclude_ids:
            where.append(f"r.id NOT IN ({','.join('?' * len(query.exclude_ids))})")
            params += query.exclude_ids
        score_sql = (
            f"(SELECT COUNT(*) FROM restaurant_cuisines c WHERE c.restaurant_id = r.id "
            f"AND c.cuisine IN ({','.join('?' * len(cuisines))}))"
            if cuisines
            else "0"
        )
        rows = self._rows(
            f"SELECT r.data, r.lat, r.lon, r.crowd_rank, {score_sql} FROM restaurants r "
            f"WHERE {' AND '.join(where)}",
            [*cuisines, *params],
        )
        ranked = self._rank(rows, query.near, query.radius_km)
        return [Restaurant.model_validate_json(d) for d in ranked[: query.limit]]

    @staticmethod
    def _common_filters(
        where: list[str],
        params: list[object],
        hood_ids: list[str],
        max_crowd: CrowdLevel | None,
        alias: str,
    ) -> None:
        if hood_ids:
            where.append(f"{alias}.neighborhood_id IN ({','.join('?' * len(hood_ids))})")
            params += hood_ids
        if max_crowd is not None:
            where.append(f"{alias}.crowd_rank <= ?")
            params.append(CROWD_RANK[max_crowd])

    @staticmethod
    def _rank(
        rows: list[tuple[object, ...]], near: LatLon | None, radius_km: float | None
    ) -> list[str]:
        """Most matched tags first, then quieter, then closer, then by id (stable)."""
        scored = []
        for data, lat, lon, crowd_rank, score in rows:
            dist = (
                near.distance_km(LatLon(lat=float(lat), lon=float(lon)))  # type: ignore[arg-type]
                if near
                else 0.0
            )
            if radius_km is not None and dist > radius_km:
                continue
            scored.append((-int(score), int(crowd_rank), round(dist, 3), str(data)))  # type: ignore[call-overload]
        scored.sort()
        return [s[3] for s in scored]

    def get_place(self, place_id: str) -> Attraction | Restaurant | None:
        row = self._rows("SELECT data FROM attractions WHERE id = ?", [place_id])
        if row:
            return Attraction.model_validate_json(str(row[0][0]))
        row = self._rows("SELECT data FROM restaurants WHERE id = ?", [place_id])
        return Restaurant.model_validate_json(str(row[0][0])) if row else None

    # ------------------------------------------------------------------ routes
    def _point(self, pid: str) -> _Point | None:
        for table, hood_col in (
            ("neighborhoods", "id"),
            ("attractions", "neighborhood_id"),
            ("restaurants", "neighborhood_id"),
        ):
            row = self._rows(f"SELECT lat, lon, {hood_col} FROM {table} WHERE id = ?", [pid])
            if row:
                lat, lon, hood = row[0]
                return _Point(LatLon(lat=float(lat), lon=float(lon)), str(hood))  # type: ignore[arg-type]
        return None

    def _road(self, from_id: str, to_id: str, mode: TransportMode) -> tuple[float, float] | None:
        """Stored OSRM (minutes, km) for taxi/walking between two curated points."""
        profile = {
            TransportMode.TAXI: "car",
            TransportMode.RIDE_HAIL: "car",
            TransportMode.WALK: "foot",
        }.get(mode)
        if profile is None or not self._has_roads:  # older databases have no road table
            return None
        row = self._rows(
            "SELECT minutes, km FROM road_times WHERE from_id = ? AND to_id = ? AND profile = ?",
            [from_id, to_id, profile],
        )
        return (float(row[0][0]), float(row[0][1])) if row else None  # type: ignore[arg-type]

    def _station(self, hood_id: str) -> str | None:
        row = self._rows("SELECT metro_station FROM neighborhoods WHERE id = ?", [hood_id])
        return str(row[0][0]) if row and row[0][0] is not None else None

    def travel(
        self, from_id: str, to_id: str, mode: TransportMode, *, party_size: int = 1
    ) -> Leg | None:
        if from_id == to_id:
            return None
        a, b = self._point(from_id), self._point(to_id)
        if a is None or b is None:
            raise UnknownPlace(f"unknown place or neighborhood: {from_id if a is None else to_id}")

        stored = self._rows(
            "SELECT data FROM travel_matrix WHERE mode = ? AND "
            "((from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?))",
            [mode.value, from_id, to_id, to_id, from_id],
        )
        road = self._road(from_id, to_id, mode) if not stored else None
        if stored:
            entry = TravelMatrixEntry.model_validate_json(str(stored[0][0]))
            minutes, km = entry.duration_min, entry.distance_km
        elif road is not None:  # stored OSRM route between two curated points
            leg = heuristics.road_leg(
                mode,
                road[0],
                road[1],
                a.location.distance_km(b.location),
                hood_a=a.neighborhood_id,
                hood_b=b.neighborhood_id,
            )
            if leg is None:
                return None
            minutes = estimated(leg[0], self._road_basis)
            km = estimated(leg[1], self._road_basis)
        else:
            if mode is TransportMode.METRO and a.neighborhood_id == b.neighborhood_id:
                return None  # same area: walk or taxi
            est = heuristics.estimate_minutes(
                a.location,
                b.location,
                mode,
                station_a=self._station(a.neighborhood_id),
                station_b=self._station(b.neighborhood_id),
                hood_a=a.neighborhood_id,
                hood_b=b.neighborhood_id,
            )
            if est is None:
                return None
            minutes = estimated(est[0], heuristics.BASIS)
            km = estimated(est[1], heuristics.BASIS)
        cost = self.leg_cost(mode, km.value, party_size)
        if cost is None:
            return None
        return Leg(
            from_id=from_id,
            to_id=to_id,
            mode=mode,
            duration_min=minutes,
            distance_km=km,
            cost_aed=cost,
        )

    def leg_cost(
        self, mode: TransportMode, km: float, party_size: int
    ) -> SourcedValue[float] | None:
        """Cost per person for one leg."""
        if mode is TransportMode.WALK:
            return estimated(0.0, "walking is free")
        if mode in (TransportMode.TAXI, TransportMode.RIDE_HAIL):
            kind = TaxiKind.STREET_DAY if mode is TransportMode.TAXI else TaxiKind.APP_OFF_PEAK
            tariff = self.taxi_tariff(kind)
            if tariff is None:
                return None
            cabs = math.ceil(party_size / TAXI_SEATS)
            per_person = round(tariff.fare(km) * cabs / party_size, 2)
            return estimated(per_person, f"{kind.value} tariff x estimated distance ({km} km)")
        if mode is TransportMode.ABRA:
            fare = self.transit_fare(mode, FareProduct.CASH, ZoneBand.FLAT)
            return None if fare is None else estimated(fare.fare_aed, "abra flat fare")
        band = ZoneBand(heuristics.zone_band_for_km(km))
        fare = self.transit_fare(mode, FareProduct.SILVER, band)
        if fare is None:
            return None
        return estimated(
            fare.fare_aed, f"nol silver fare, zone band {band.value} estimated from distance"
        )

    def matrix(
        self, neighborhood_ids: Sequence[str], modes: Sequence[TransportMode]
    ) -> TravelMatrix:
        ids, mode_values = list(neighborhood_ids), [m.value for m in modes]
        if not ids or not mode_values:
            return TravelMatrix()
        q = ",".join("?" * len(ids))
        rows = self._rows(
            f"SELECT data FROM travel_matrix WHERE from_id IN ({q}) AND to_id IN ({q}) "
            f"AND mode IN ({','.join('?' * len(mode_values))}) ORDER BY from_id, to_id, mode",
            [*ids, *ids, *mode_values],
        )
        return TravelMatrix(
            entries=[TravelMatrixEntry.model_validate_json(str(r[0])) for r in rows]
        )

    # ------------------------------------------------------------------ weather
    def climate(self, month: int) -> ClimateMonth:
        row = self._rows("SELECT data FROM climate WHERE month = ?", [month])
        if not row:
            raise ProviderError(f"no climate data for month {month}")
        return ClimateMonth.model_validate_json(str(row[0][0]))

    def forecast(self, start: date, days: int) -> list[DayWeather]:
        """Climate normals for each date (tagged ``estimated``: not a real forecast)."""
        out = []
        for i in range(days):
            d = start + timedelta(days=i)
            c = self.climate(d.month)
            days_in_month = monthrange(d.year, d.month)[1]
            out.append(
                DayWeather(
                    date=d,
                    temp_max_c=c.temp_max_c,
                    temp_min_c=c.temp_min_c,
                    precipitation_mm=round(c.precipitation_mm / days_in_month, 2),
                    rain_probability_pct=round(min(100.0, c.rainy_days / days_in_month * 100), 1),
                    source=Source.ESTIMATED,
                    reference=f"climate normal for {MONTHS[d.month - 1]} (Open-Meteo ERA5 archive)",
                )
            )
        return out

    # ------------------------------------------------------------------ hotels
    def areas(self, tier: HotelTier | None = None) -> list[HotelArea]:
        if tier is None:
            rows = self._rows("SELECT data FROM hotel_areas ORDER BY tier, nightly_min")
        else:
            rows = self._rows(
                "SELECT data FROM hotel_areas WHERE tier = ? ORDER BY nightly_min", [tier.value]
            )
        return [HotelArea.model_validate_json(str(r[0])) for r in rows]

    def fees(self, tier: HotelTier) -> HotelFees:
        row = self._rows("SELECT data FROM hotel_fees WHERE tier = ?", [tier.value])
        if not row:
            raise ProviderError(f"no hotel fees for tier {tier.value}")
        return HotelFees.model_validate_json(str(row[0][0]))

    def season(self, month: int) -> HotelSeason:
        row = self._rows("SELECT data FROM hotel_seasons WHERE month = ?", [month])
        if not row:
            raise ProviderError(f"no hotel season for month {month}")
        return HotelSeason.model_validate_json(str(row[0][0]))

    # ------------------------------------------------------------------ pricing
    def transit_fare(
        self, mode: TransportMode, product: FareProduct, zone_band: ZoneBand
    ) -> TransitFare | None:
        row = self._rows(
            "SELECT data FROM transit_fares WHERE mode = ? AND product = ? AND zone_band = ?",
            [mode.value, product.value, zone_band.value],
        )
        return TransitFare.model_validate_json(str(row[0][0])) if row else None

    def taxi_tariff(self, kind: TaxiKind) -> TaxiTariff | None:
        row = self._rows("SELECT data FROM taxi_tariffs WHERE kind = ?", [kind.value])
        return TaxiTariff.model_validate_json(str(row[0][0])) if row else None
