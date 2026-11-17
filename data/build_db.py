"""Validate the seed JSON and build the SQLite database (architecture.md §9.3).

Usage::

    uv run python -m data.build_db [DB_PATH]      # default: settings.db_path (data/dubai.sqlite)

Each table keeps a few indexed columns for filtering plus the full validated record as JSON
in ``data``; the local provider reads records back through the same Pydantic models.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from core.config import get_settings
from data.dataset import SEED_DIR, Dataset

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE neighborhoods (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    metro_station TEXT, beach_access INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE attractions (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, neighborhood_id TEXT NOT NULL REFERENCES neighborhoods,
    lat REAL NOT NULL, lon REAL NOT NULL, category TEXT NOT NULL, crowd_level TEXT NOT NULL,
    crowd_rank INTEGER NOT NULL, indoor INTEGER NOT NULL, price_min REAL NOT NULL,
    price_max REAL NOT NULL, price_source TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE attraction_tags (
    attraction_id TEXT NOT NULL REFERENCES attractions, tag TEXT NOT NULL,
    PRIMARY KEY (attraction_id, tag));
CREATE TABLE restaurants (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, neighborhood_id TEXT NOT NULL REFERENCES neighborhoods,
    lat REAL NOT NULL, lon REAL NOT NULL, price_band TEXT NOT NULL, crowd_level TEXT NOT NULL,
    crowd_rank INTEGER NOT NULL, price_min REAL NOT NULL, price_max REAL NOT NULL,
    data TEXT NOT NULL);
CREATE TABLE restaurant_cuisines (
    restaurant_id TEXT NOT NULL REFERENCES restaurants, cuisine TEXT NOT NULL,
    PRIMARY KEY (restaurant_id, cuisine));
CREATE TABLE restaurant_meals (
    restaurant_id TEXT NOT NULL REFERENCES restaurants, meal TEXT NOT NULL,
    PRIMARY KEY (restaurant_id, meal));
CREATE TABLE hotel_areas (
    neighborhood_id TEXT NOT NULL REFERENCES neighborhoods, tier TEXT NOT NULL,
    nightly_min REAL NOT NULL, nightly_max REAL NOT NULL, metro_access INTEGER NOT NULL,
    beach_access INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (neighborhood_id, tier));
CREATE TABLE hotel_fees (tier TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE hotel_seasons (
    month INTEGER PRIMARY KEY, multiplier REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE transit_fares (
    mode TEXT NOT NULL, product TEXT NOT NULL, zone_band TEXT NOT NULL, fare_aed REAL NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY (mode, product, zone_band));
CREATE TABLE taxi_tariffs (kind TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE travel_matrix (
    from_id TEXT NOT NULL, to_id TEXT NOT NULL, mode TEXT NOT NULL, duration_min INTEGER NOT NULL,
    distance_km REAL NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, mode));
CREATE TABLE climate (month INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE INDEX attractions_hood ON attractions (neighborhood_id);
CREATE INDEX attraction_tags_tag ON attraction_tags (tag);
CREATE INDEX restaurants_hood ON restaurants (neighborhood_id);
CREATE INDEX restaurant_cuisines_cuisine ON restaurant_cuisines (cuisine);
"""

CROWD_RANK = {"low": 0, "medium": 1, "high": 2}


class DatasetError(ValueError):
    """The seed data failed validation."""


def build(db_path: Path, seed_dir: Path = SEED_DIR) -> Dataset:
    """Validate the seed data and (re)create ``db_path``. Raises :class:`DatasetError`."""
    ds = Dataset.load(seed_dir)
    problems = ds.problems()
    if problems:
        raise DatasetError("seed data is invalid:\n  - " + "\n  - ".join(problems))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        _insert(con, ds)
        con.commit()
    finally:
        con.close()
    tmp.replace(db_path)  # atomic: readers never see a half-built database
    return ds


def _insert(con: sqlite3.Connection, ds: Dataset) -> None:
    def j(model: object) -> str:
        return model.model_dump_json()  # type: ignore[attr-defined, no-any-return]

    con.executemany(
        "INSERT INTO neighborhoods VALUES (?,?,?,?,?,?,?)",
        [
            (h.id, h.name, h.location.lat, h.location.lon, h.metro_station, h.beach_access, j(h))
            for h in ds.neighborhoods
        ],
    )
    for a in ds.attractions:
        con.execute(
            "INSERT INTO attractions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                a.id,
                a.name,
                a.neighborhood_id,
                a.location.lat,
                a.location.lon,
                a.category,
                a.crowd_level.value,
                CROWD_RANK[a.crowd_level.value],
                a.indoor,
                a.price.value.min_aed,
                a.price.value.max_aed,
                a.price.source.value,
                j(a),
            ),
        )
        con.executemany("INSERT INTO attraction_tags VALUES (?,?)", [(a.id, t) for t in a.tags])
    for r in ds.restaurants:
        con.execute(
            "INSERT INTO restaurants VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.id,
                r.name,
                r.neighborhood_id,
                r.location.lat,
                r.location.lon,
                r.price_band.value,
                r.crowd_level.value,
                CROWD_RANK[r.crowd_level.value],
                r.price.value.min_aed,
                r.price.value.max_aed,
                j(r),
            ),
        )
        con.executemany(
            "INSERT INTO restaurant_cuisines VALUES (?,?)",
            [(r.id, c) for c in dict.fromkeys(r.cuisine)],
        )
        con.executemany(
            "INSERT INTO restaurant_meals VALUES (?,?)", [(r.id, m.value) for m in r.meal_types]
        )
    con.executemany(
        "INSERT INTO hotel_areas VALUES (?,?,?,?,?,?,?)",
        [
            (
                h.neighborhood_id,
                h.tier.value,
                h.nightly_rate.value.min_aed,
                h.nightly_rate.value.max_aed,
                h.metro_access,
                h.beach_access,
                j(h),
            )
            for h in ds.hotel_areas
        ],
    )
    con.executemany(
        "INSERT INTO hotel_fees VALUES (?,?)", [(f.tier.value, j(f)) for f in ds.hotel_fees]
    )
    con.executemany(
        "INSERT INTO hotel_seasons VALUES (?,?,?)",
        [(s.month, s.multiplier, j(s)) for s in ds.hotel_seasons],
    )
    con.executemany(
        "INSERT INTO transit_fares VALUES (?,?,?,?,?)",
        [
            (f.mode.value, f.product.value, f.zone_band.value, f.fare_aed, j(f))
            for f in ds.transit_fares
        ],
    )
    con.executemany(
        "INSERT INTO taxi_tariffs VALUES (?,?)", [(t.kind.value, j(t)) for t in ds.taxi_tariffs]
    )
    con.executemany(
        "INSERT INTO travel_matrix VALUES (?,?,?,?,?,?,?)",
        [
            (
                e.from_id,
                e.to_id,
                e.mode.value,
                e.duration_min.value,
                e.distance_km.value,
                e.duration_min.source.value,
                j(e),
            )
            for e in ds.travel_matrix.entries
        ],
    )
    con.executemany("INSERT INTO climate VALUES (?,?)", [(c.month, j(c)) for c in ds.climate])
    meta = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "attractions": str(len(ds.attractions)),
        "restaurants": str(len(ds.restaurants)),
        "neighborhoods": str(len(ds.neighborhoods)),
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())


def main(argv: list[str]) -> int:  # pragma: no cover
    db_path = Path(argv[0]) if argv else get_settings().db_path
    try:
        ds = build(db_path)
    except DatasetError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"built {db_path}")
    print(
        f"  {len(ds.neighborhoods)} neighborhoods, {len(ds.attractions)} attractions, "
        f"{len(ds.restaurants)} restaurants, {len(ds.hotel_areas)} hotel areas, "
        f"{len(ds.travel_matrix.entries)} matrix entries"
    )
    print(f"  coverage (need >= 5 each): {ds.coverage()}")
    print(f"  place price sources: {ds.source_counts()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
