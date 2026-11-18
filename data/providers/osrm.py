"""Live routing with OSRM (research.md §2.4): taxi and walking legs, tagged ``api``.

- ``/route`` for one leg, ``/table`` for the neighborhood matrix. Car times use the car server,
  walking the foot server (``OSRM_CAR_URL`` / ``OSRM_FOOT_URL``).
- The same rules as the stored routes turn a route into a leg (``heuristics.road_leg``): taxi
  pickup time and a city-traffic factor; no walking across the creek or on absurd detours.
- Metro, abra and the rest are not road modes: the provider raises :class:`NotApplicable` and
  the chain asks the local provider instead (no fallback event).
- Fares come from the local fare tables (the ``leg_cost`` callable), per person.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from core.config import Settings
from data import heuristics
from data.providers.base import NotApplicable, ProviderError
from data.providers.http import HttpClient
from schemas import (
    LatLon,
    Leg,
    SourcedValue,
    TransportMode,
    TravelMatrix,
    TravelMatrixEntry,
    estimated,
    from_api,
)

Locate = Callable[[str], tuple[LatLon, str | None] | None]  # id -> (location, neighborhood id)
LegCost = Callable[[TransportMode, float, int], SourcedValue[float] | None]
ROAD_MODES = (TransportMode.TAXI, TransportMode.RIDE_HAIL, TransportMode.WALK)


class OSRMRoutesProvider:
    name = "osrm"

    def __init__(
        self, http: HttpClient, settings: Settings, *, locate: Locate, leg_cost: LegCost
    ) -> None:
        self.http = http
        self.settings = settings
        self.locate = locate
        self.leg_cost = leg_cost

    def _base(self, mode: TransportMode) -> str:
        url = (
            self.settings.osrm_foot_url
            if mode is TransportMode.WALK
            else self.settings.osrm_car_url
        )
        return url.rstrip("/")

    def _api(self, mode: TransportMode) -> str:
        return f"OSRM {urlsplit(self._base(mode)).netloc} (live route)"

    def _point(self, pid: str) -> tuple[LatLon, str | None]:
        found = self.locate(pid)
        if found is None:
            raise ProviderError(f"osrm: unknown place or neighborhood {pid}")
        return found

    # ------------------------------------------------------------------ RoutesProvider
    def travel(
        self, from_id: str, to_id: str, mode: TransportMode, *, party_size: int = 1
    ) -> Leg | None:
        if mode not in ROAD_MODES:
            raise NotApplicable(mode.value)
        if from_id == to_id:
            return None
        (a, hood_a), (b, hood_b) = self._point(from_id), self._point(to_id)
        body = self.http.get_json(
            "osrm",
            f"{self._base(mode)}/route/v1/driving/{a.lon:.6f},{a.lat:.6f};{b.lon:.6f},{b.lat:.6f}",
            params={"overview": "false"},
            ttl_h=self.settings.cache_ttl_routes_h,
        )
        try:
            if body.get("code") != "Ok" or not body.get("routes"):
                raise ProviderError(f"osrm: no route ({body.get('code')})")
            minutes = float(body["routes"][0]["duration"]) / 60
            km = float(body["routes"][0]["distance"]) / 1000
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"osrm: unexpected route response ({exc!r})") from exc
        leg = heuristics.road_leg(mode, minutes, km, a.distance_km(b), hood_a=hood_a, hood_b=hood_b)
        if leg is None:
            return None
        cost = self.leg_cost(mode, leg[1], party_size)
        if cost is None:
            return None
        api = self._api(mode)
        return Leg(
            from_id=from_id,
            to_id=to_id,
            mode=mode,
            duration_min=from_api(leg[0], api),
            distance_km=from_api(leg[1], api),
            cost_aed=cost,
        )

    def matrix(
        self, neighborhood_ids: Sequence[str], modes: Sequence[TransportMode]
    ) -> TravelMatrix:
        """One ``/table`` call per road mode; other modes are left to the local matrix."""
        ids = list(dict.fromkeys(neighborhood_ids))
        road_modes = [m for m in modes if m in ROAD_MODES]
        if len(ids) < 2 or not road_modes:
            raise NotApplicable("matrix")
        points = [self._point(i) for i in ids]
        coords = ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p, _ in points)
        entries: list[TravelMatrixEntry] = []
        for mode in road_modes:
            body = self.http.get_json(
                "osrm",
                f"{self._base(mode)}/table/v1/driving/{coords}",
                params={"annotations": "duration,distance"},
                ttl_h=self.settings.cache_ttl_routes_h,
            )
            try:
                if body.get("code") != "Ok":
                    raise ProviderError(f"osrm: table failed ({body.get('code')})")
                durations, distances = body["durations"], body["distances"]
                if len(durations) != len(ids) or any(len(r) != len(ids) for r in durations):
                    raise ProviderError("osrm: table has the wrong shape")
            except (AttributeError, KeyError, TypeError) as exc:
                raise ProviderError(f"osrm: unexpected table response ({exc!r})") from exc
            api = self._api(mode)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    s, m = durations[i][j], distances[i][j]
                    if s is None or m is None:
                        continue
                    leg = heuristics.road_leg(
                        mode,
                        s / 60,
                        m / 1000,
                        points[i][0].distance_km(points[j][0]),
                        hood_a=points[i][1],
                        hood_b=points[j][1],
                    )
                    if leg is not None:
                        entries.append(
                            TravelMatrixEntry(
                                from_id=ids[i],
                                to_id=ids[j],
                                mode=mode,
                                duration_min=from_api(leg[0], api),
                                distance_km=from_api(leg[1], api),
                            )
                        )
        return TravelMatrix(entries=entries)


class EstimatedRoutesProvider:
    """Last link of a live routes chain: the distance heuristic for places the stored data
    doesn't know (live OpenStreetMap places), e.g. when OSRM is down. Tagged ``estimated``."""

    name = "estimate"

    def __init__(
        self, *, locate: Locate, station: Callable[[str], str | None], leg_cost: LegCost
    ) -> None:
        self.locate = locate
        self.station = station
        self.leg_cost = leg_cost

    def travel(
        self, from_id: str, to_id: str, mode: TransportMode, *, party_size: int = 1
    ) -> Leg | None:
        if from_id == to_id:
            return None
        a, b = self.locate(from_id), self.locate(to_id)
        if a is None or b is None:
            raise ProviderError(f"estimate: unknown place {from_id if a is None else to_id}")
        est = heuristics.estimate_minutes(
            a[0],
            b[0],
            mode,
            station_a=self.station(a[1]) if a[1] else None,
            station_b=self.station(b[1]) if b[1] else None,
            hood_a=a[1],
            hood_b=b[1],
        )
        if est is None:
            return None
        cost = self.leg_cost(mode, est[1], party_size)
        if cost is None:
            return None
        return Leg(
            from_id=from_id,
            to_id=to_id,
            mode=mode,
            duration_min=estimated(est[0], heuristics.BASIS),
            distance_km=estimated(est[1], heuristics.BASIS),
            cost_aed=cost,
        )

    def matrix(
        self, neighborhood_ids: Sequence[str], modes: Sequence[TransportMode]
    ) -> TravelMatrix:
        raise NotApplicable("matrix")
