"""The data scripts reproduce the committed seed files (no hand edits, nothing stale).

These run offline: they use the committed OSM export in ``data/raw/osm`` instead of the API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data.dataset import SEED_DIR, Dataset
from data.scripts import build_matrix, climate_normals, curate, osm_export


def _seed(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SEED_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return data


def test_curation_reproduces_seed_places() -> None:
    osm = curate.OsmIndex.load()
    hoods = {h.id: h for h in curate.build_neighborhoods()}
    report = curate.Report()
    attractions = curate.build_attractions(hoods, osm, report)
    restaurants = curate.build_restaurants(hoods, osm, report)
    assert not report.missing
    dump = [a.model_dump(mode="json", exclude_none=True) for a in attractions]
    assert dump == _seed("attractions")["attractions"]
    dump = [r.model_dump(mode="json", exclude_none=True) for r in restaurants]
    assert dump == _seed("restaurants")["restaurants"]
    assert report.matched >= 100  # nearly everything is located from OSM


def test_matrix_script_reproduces_seed_matrix() -> None:
    ds = Dataset.load()
    matrix = build_matrix.build(ds.neighborhoods, ds.road_times)
    assert (
        matrix.model_dump(mode="json", exclude_none=True)["entries"]
        == (_seed("travel_matrix")["entries"])
    )


def test_climate_normals_from_daily_series() -> None:
    days = [f"2020-{m:02d}-{d:02d}" for m in (1, 7) for d in (1, 2)]
    daily: dict[str, list[float | str | None]] = {
        "time": list(days),
        "temperature_2m_max": [24.0, 26.0, 41.0, 43.0],
        "temperature_2m_min": [14.0, 16.0, 30.0, None],
        "precipitation_sum": [5.0, 0.0, 0.0, None],
        "relative_humidity_2m_mean": [60.0, 70.0, 50.0, 50.0],
    }
    months = {m["month"]: m for m in climate_normals.normals(daily, 2020, 2020)}
    assert months[1]["temp_max_c"] == 25.0
    assert months[1]["precipitation_mm"] == 5.0 and months[1]["rainy_days"] == 1
    assert months[7]["temp_min_c"] == 30.0  # missing values are skipped
    assert months[1]["source"] == "verified"


def test_osm_compact_keeps_useful_tags() -> None:
    el = {
        "type": "way",
        "id": 7,
        "center": {"lat": 25.1, "lon": 55.2},
        "tags": {"name": "X", "cuisine": "emirati", "building": "yes"},
    }
    compact = osm_export._compact(el)
    assert compact == {
        "osm": "way/7",
        "lat": 25.1,
        "lon": 55.2,
        "tags": {"name": "X", "cuisine": "emirati"},
    }
    assert osm_export._compact({"type": "node", "id": 1, "tags": {}}) is None


def test_raw_exports_cover_every_neighborhood() -> None:
    raw = Path(curate.RAW)
    exported = {p.stem for p in raw.glob("*.json") if not p.name.startswith("_")}
    assert {h.id for h in Dataset.load().neighborhoods} <= exported
