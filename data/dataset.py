"""The curated dataset: load the seed JSON, validate it, and report coverage.

Every record is parsed with the Phase 1 schemas, then cross-checked (unique ids, known
neighborhoods, provenance, complete fare and season tables). ``build_db.py`` refuses to build
the SQLite database when :meth:`Dataset.problems` is not empty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from schemas import (
    Attraction,
    ClimateMonth,
    CrowdLevel,
    HotelArea,
    HotelFees,
    HotelSeason,
    HotelTier,
    Interest,
    Neighborhood,
    Restaurant,
    Source,
    TaxiKind,
    TaxiTariff,
    TransitFare,
    TransportMode,
    TravelMatrix,
    ZoneBand,
)
from schemas.pricing import FareProduct

SEED_DIR = Path(__file__).resolve().parent / "seed"

# Cuisines that count as "local food" for the example request.
LOCAL_CUISINES = {
    "emirati",
    "arabic",
    "lebanese",
    "syrian",
    "levantine",
    "yemeni",
    "iraqi",
    "egyptian",
    "persian",
    "afghan",
    "moroccan",
    "seafood",
}


def _read(seed_dir: Path, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((seed_dir / f"{name}.json").read_text(encoding="utf-8"))
    return data


class Dataset(BaseModel):
    model_config = ConfigDict(frozen=True)

    neighborhoods: list[Neighborhood]
    attractions: list[Attraction]
    restaurants: list[Restaurant]
    hotel_areas: list[HotelArea]
    hotel_fees: list[HotelFees]
    hotel_seasons: list[HotelSeason]
    transit_fares: list[TransitFare]
    taxi_tariffs: list[TaxiTariff]
    travel_matrix: TravelMatrix
    climate: list[ClimateMonth]

    @classmethod
    def load(cls, seed_dir: Path = SEED_DIR) -> Dataset:
        hotels = _read(seed_dir, "hotels")
        pricing = _read(seed_dir, "pricing")
        matrix = _read(seed_dir, "travel_matrix")
        return cls(
            neighborhoods=_read(seed_dir, "neighborhoods")["neighborhoods"],
            attractions=_read(seed_dir, "attractions")["attractions"],
            restaurants=_read(seed_dir, "restaurants")["restaurants"],
            hotel_areas=hotels["hotel_areas"],
            hotel_fees=hotels["hotel_fees"],
            hotel_seasons=hotels["seasons"],
            transit_fares=pricing["transit_fares"],
            taxi_tariffs=pricing["taxi_tariffs"],
            travel_matrix=TravelMatrix(entries=matrix["entries"]),
            climate=_read(seed_dir, "climate")["months"],
        )

    # ------------------------------------------------------------ validation
    def problems(self) -> list[str]:
        """Cross-record checks the per-model validators cannot do. Empty means valid."""
        out: list[str] = []
        hood_ids = {h.id for h in self.neighborhoods}
        if len(hood_ids) != len(self.neighborhoods):
            out.append("duplicate neighborhood ids")

        place_ids = [p.id for p in self.attractions] + [p.id for p in self.restaurants]
        dupes = sorted({i for i in place_ids if place_ids.count(i) > 1})
        if dupes:
            out.append(f"duplicate place ids: {dupes}")
        clash = sorted(set(place_ids) & hood_ids)
        if clash:
            out.append(f"place ids clash with neighborhood ids: {clash}")

        interests = {i.value for i in Interest}
        for p in [*self.attractions, *self.restaurants]:
            if p.neighborhood_id not in hood_ids:
                out.append(f"{p.id}: unknown neighborhood {p.neighborhood_id}")
            if p.source is not Source.ESTIMATED and not p.reference:
                out.append(f"{p.id}: source {p.source.value} needs a reference")
        for a in self.attractions:
            if not interests & set(a.tags):
                out.append(f"{a.id}: tags must include at least one interest")

        for h in self.hotel_areas:
            if h.neighborhood_id not in hood_ids:
                out.append(f"hotel area: unknown neighborhood {h.neighborhood_id}")
        keys = [(h.neighborhood_id, h.tier) for h in self.hotel_areas]
        if len(keys) != len(set(keys)):
            out.append("duplicate (neighborhood, tier) hotel areas")
        for tier in HotelTier:
            if not any(h.tier is tier for h in self.hotel_areas):
                out.append(f"no hotel areas for tier {tier.value}")
        if {f.tier for f in self.hotel_fees} != set(HotelTier):
            out.append("hotel_fees must cover every tier exactly once")
        if sorted(s.month for s in self.hotel_seasons) != list(range(1, 13)):
            out.append("hotel seasons must cover months 1-12 exactly once")
        if sorted(c.month for c in self.climate) != list(range(1, 13)):
            out.append("climate must cover months 1-12 exactly once")

        fare_keys = [(f.mode, f.product, f.zone_band) for f in self.transit_fares]
        if len(fare_keys) != len(set(fare_keys)):
            out.append("duplicate transit fares")
        for band in (ZoneBand.ONE_ZONE, ZoneBand.TWO_ZONES, ZoneBand.MULTI_ZONE):
            if (TransportMode.METRO, FareProduct.SILVER, band) not in fare_keys:
                out.append(f"missing metro silver fare for {band.value}")
        kinds = [t.kind for t in self.taxi_tariffs]
        if len(kinds) != len(set(kinds)):
            out.append("duplicate taxi tariffs")
        if TaxiKind.STREET_DAY not in kinds:
            out.append("missing street_day taxi tariff")

        for e in self.travel_matrix.entries:
            if e.from_id not in hood_ids or e.to_id not in hood_ids:
                out.append(f"travel matrix: unknown neighborhood in {e.from_id}->{e.to_id}")
        return out

    # ------------------------------------------------------------ coverage
    def coverage(self) -> dict[str, int]:
        """Candidates per need of the example request (Phase 2 exit criterion: >= 5 each)."""
        return {
            "culture": sum("culture" in a.tags for a in self.attractions),
            "beach": sum("beach" in a.tags for a in self.attractions),
            "local_food": sum(bool(LOCAL_CUISINES & set(r.cuisine)) for r in self.restaurants),
            "budget_hotel_areas": sum(h.tier is HotelTier.BUDGET for h in self.hotel_areas),
            "low_crowd_attractions": sum(a.crowd_level is CrowdLevel.LOW for a in self.attractions),
        }

    def source_counts(self) -> dict[str, int]:
        """How many prices are verified / api / estimated (attractions + restaurants)."""
        counts = {s.value: 0 for s in Source}
        for p in [*self.attractions, *self.restaurants]:
            counts[p.price.source.value] += 1
        return counts
