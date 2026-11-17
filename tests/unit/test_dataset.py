"""The curated seed data: validation, coverage of the example request, and the SQLite build."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from data.build_db import DatasetError, build
from data.dataset import SEED_DIR, Dataset
from data.scripts.staleness_report import Dated, months_between, scan, stale
from schemas import HotelTier, Interest, Source


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return Dataset.load()


class TestSeedData:
    def test_seed_is_valid(self, dataset: Dataset) -> None:
        assert dataset.problems() == []

    def test_sizes(self, dataset: Dataset) -> None:
        assert 15 <= len(dataset.neighborhoods) <= 25
        assert len(dataset.attractions) >= 45
        assert len(dataset.restaurants) >= 45
        assert {h.tier for h in dataset.hotel_areas} == set(HotelTier)

    def test_example_request_coverage(self, dataset: Dataset) -> None:
        """Phase 2 exit criterion: >= 5 candidates for each need of the example request."""
        coverage = dataset.coverage()
        assert all(n >= 5 for n in coverage.values()), coverage

    def test_every_attraction_matches_an_interest(self, dataset: Dataset) -> None:
        interests = {i.value for i in Interest}
        assert all(interests & set(a.tags) for a in dataset.attractions)

    def test_free_places_are_tagged(self, dataset: Dataset) -> None:
        for a in dataset.attractions:
            assert ("free" in a.tags) == a.is_free

    def test_osm_records_link_to_osm(self, dataset: Dataset) -> None:
        for p in [*dataset.attractions, *dataset.restaurants]:
            if p.source is Source.VERIFIED:
                assert p.reference and p.reference.startswith("https://www.openstreetmap.org/")
            else:
                assert p.source is Source.ESTIMATED

    def test_verified_prices_are_dated(self, dataset: Dataset) -> None:
        verified = [a for a in dataset.attractions if a.price.source is Source.VERIFIED]
        assert {a.id for a in verified} >= {"etihad-museum", "al-shindagha-museum"}
        assert all(a.price.as_of and a.price.reference for a in verified)

    def test_official_fares(self, dataset: Dataset) -> None:
        silver = {
            f.zone_band.value: f.fare_aed
            for f in dataset.transit_fares
            if f.mode.value == "metro" and f.product.value == "silver"
        }
        assert silver == {"1_zone": 3.0, "2_zones": 5.0, "3_plus_zones": 7.5}

    def test_climate_is_plausible(self, dataset: Dataset) -> None:
        by_month = {c.month: c for c in dataset.climate}
        assert by_month[1].temp_max_c < 28 < by_month[7].temp_max_c
        assert by_month[8].heat.value == "extreme"

    def test_problems_catches_bad_references(self, dataset: Dataset) -> None:
        bad = dataset.attractions[0].model_copy(update={"neighborhood_id": "atlantis-city"})
        broken = dataset.model_copy(update={"attractions": [bad, *dataset.attractions]})
        problems = broken.problems()
        assert any("unknown neighborhood" in p for p in problems)
        assert any("duplicate place ids" in p for p in problems)


class TestBuild:
    def test_build_writes_tables(self, seed_db: Path, dataset: Dataset) -> None:
        con = sqlite3.connect(seed_db)
        try:
            count = con.execute("SELECT COUNT(*) FROM attractions").fetchone()[0]
            tags = con.execute("SELECT COUNT(*) FROM attraction_tags WHERE tag='beach'").fetchone()
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()
        assert count == len(dataset.attractions)
        assert tags[0] == dataset.coverage()["beach"]
        assert meta["restaurants"] == str(len(dataset.restaurants))

    def test_build_refuses_invalid_seed(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed"
        shutil.copytree(SEED_DIR, seed)
        hotels = json.loads((seed / "hotels.json").read_text(encoding="utf-8"))
        hotels["hotel_areas"][0]["neighborhood_id"] = "nowhere"
        (seed / "hotels.json").write_text(json.dumps(hotels), encoding="utf-8")
        db = tmp_path / "x.sqlite"
        with pytest.raises(DatasetError, match="nowhere"):
            build(db, seed)
        assert not db.exists()

    def test_build_rejects_missing_provenance(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed"
        shutil.copytree(SEED_DIR, seed)
        pricing = json.loads((seed / "pricing.json").read_text(encoding="utf-8"))
        del pricing["transit_fares"][0]["as_of"]  # verified without a date
        (seed / "pricing.json").write_text(json.dumps(pricing), encoding="utf-8")
        with pytest.raises(ValueError, match="as_of"):
            build(tmp_path / "x.sqlite", seed)


class TestStaleness:
    def test_scan_counts_sources(self) -> None:
        items, sources = scan()
        assert sources["verified"] > 0 and sources["estimated"] > 0
        assert all(d.source in ("verified", "api", "estimated") for d in items)

    def test_months_between(self) -> None:
        assert months_between(date(2026, 1, 31), date(2026, 3, 1)) == 1
        assert months_between(date(2026, 1, 15), date(2026, 7, 15)) == 6

    def test_stale(self) -> None:
        old = Dated("f.json", "x", "verified", date(2025, 1, 1), "ref")
        new = Dated("f.json", "y", "verified", date(2026, 10, 1), "ref")
        assert stale([old, new], 6, today=date(2026, 11, 15)) == [old]
