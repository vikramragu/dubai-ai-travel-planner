"""Merge hand-curated lists with the OpenStreetMap export into validated seed files.

Inputs:  ``data/curation/{neighborhoods,attractions,restaurants}.json`` (written by hand)
         ``data/raw/osm/*.json`` (from ``osm_export``)
Outputs: ``data/seed/{neighborhoods,attractions,restaurants}.json`` (validated with the
         Phase 1 schemas; reviewed and committed)

For every curated place the script finds its OSM element by name (``match`` regex), takes the
coordinates and OSM id from it, and uses the OSM ``opening_hours`` tag when it can be parsed.
Places that are not in the export can be looked up across all of Dubai with ``--lookup``
(cached in ``data/raw/osm/_lookups.json``); otherwise the curated ``fallback_location`` is used
and the record is tagged ``estimated``.

Usage::

    uv run python -m data.scripts.curate [--lookup]
"""

from __future__ import annotations

import json
import re
import sys
import time as time_mod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import httpx

from core.config import get_settings
from schemas import (
    Attraction,
    Interest,
    LatLon,
    MealType,
    Neighborhood,
    OpeningHours,
    PriceBand,
    PriceRange,
    Restaurant,
    Source,
    SourcedValue,
    TimeWindow,
    Weekday,
)

ROOT = Path(__file__).resolve().parents[1]
CURATION = ROOT / "curation"
RAW = ROOT / "raw" / "osm"
SEED = ROOT / "seed"
LOOKUPS = RAW / "_lookups.json"

MAX_MATCH_DISTANCE_KM = 6.0
WEEK = list(Weekday)
OSM_DAYS = {
    "mo": "mon",
    "tu": "tue",
    "we": "wed",
    "th": "thu",
    "fr": "fri",
    "sa": "sat",
    "su": "sun",
}
INTERESTS = {i.value for i in Interest}
RESTAURANT_DURATION = {"street": 40, "casual": 60, "mid": 75, "fine": 120}


# ------------------------------------------------------------------ opening hours
def _day_range(spec: str) -> list[Weekday]:
    """'sat-thu' -> [sat, sun, mon, tue, wed, thu] (wraps); 'fri' -> [fri]; 'daily' -> all."""
    spec = spec.strip().lower()
    if spec in ("daily", "mo-su", "mon-sun"):
        return WEEK
    if "-" in spec:
        a, b = (Weekday(OSM_DAYS.get(x, x)) for x in spec.split("-"))
        i, j = WEEK.index(a), WEEK.index(b)
        return [WEEK[(i + k) % 7] for k in range((j - i) % 7 + 1)]
    return [Weekday(OSM_DAYS.get(spec, spec))]


def _clock(s: str) -> time:
    hh, mm = s.strip().split(":")
    h = int(hh)
    if h == 24 and int(mm) == 0:
        return time(0, 0)
    return time(h, int(mm))


def _windows(spec: str) -> list[TimeWindow]:
    out = []
    for part in spec.split(","):
        opens, closes = (_clock(x) for x in part.split("-"))
        if opens == closes:  # 00:00-24:00
            closes = time(23, 59)
        out.append(TimeWindow(opens=opens, closes=closes))
    return out


def curated_hours(spec: str | dict[str, str] | None) -> OpeningHours | None:
    if spec is None:
        return None
    if spec == "always":
        return OpeningHours(always_open=True)
    assert isinstance(spec, dict)
    weekly: dict[Weekday, list[TimeWindow]] = {}
    for days, times in spec.items():
        for d in _day_range(days):
            weekly.setdefault(d, []).extend(_windows(times))
    return OpeningHours(weekly=weekly)


_OSM_RULE = re.compile(
    r"^(?:(?P<days>(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?"
    r"(?:,(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?)*)\s+)?"
    r"(?P<times>\d{1,2}:\d{2}-\d{1,2}:\d{2}(?:,\d{1,2}:\d{2}-\d{1,2}:\d{2})*|off|closed)$"
)


def osm_hours(tag: str | None) -> OpeningHours | None:
    """Parse the common subset of the OSM opening_hours syntax; ``None`` if unsupported."""
    if not tag:
        return None
    tag = tag.strip()
    if tag == "24/7":
        return OpeningHours(always_open=True)
    weekly: dict[Weekday, list[TimeWindow]] = {}
    try:
        for rule in (r.strip() for r in tag.split(";") if r.strip()):
            if rule.startswith("PH"):
                continue  # public-holiday rules are ignored
            m = _OSM_RULE.match(rule)
            if not m:
                return None
            days = (
                [d for spec in m["days"].split(",") for d in _day_range(spec.lower())]
                if m["days"]
                else WEEK
            )
            if m["times"] in ("off", "closed"):
                for d in days:
                    weekly.pop(d, None)
                continue
            for d in days:
                weekly[d] = _windows(m["times"])  # later rules override earlier ones
        if not weekly:
            return None
        if len(weekly) == 7 and all(
            w == [TimeWindow(opens=time(0), closes=time(23, 59))] for w in weekly.values()
        ):
            return OpeningHours(always_open=True)
        return OpeningHours(weekly=weekly)
    except (ValueError, KeyError):
        return None


# ------------------------------------------------------------------ OSM matching
@dataclass
class OsmIndex:
    elements: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> OsmIndex:
        idx = cls()
        for path in sorted(RAW.glob("*.json")):
            if path.name.startswith("_"):
                continue
            for el in json.loads(path.read_text(encoding="utf-8"))["elements"]:
                idx.elements.setdefault(el["osm"], el)
        if LOOKUPS.exists():
            for els in json.loads(LOOKUPS.read_text(encoding="utf-8")).values():
                for el in els:
                    idx.elements.setdefault(el["osm"], el)
        return idx

    def find(self, pattern: str, near: LatLon) -> tuple[dict[str, Any], float] | None:
        rx = re.compile(pattern, re.IGNORECASE)
        best: tuple[dict[str, Any], float] | None = None
        for el in self.elements.values():
            names = [el["tags"].get("name:en"), el["tags"].get("name")]
            if not any(n and rx.search(n) for n in names):
                continue
            km = near.distance_km(LatLon(lat=el["lat"], lon=el["lon"]))
            if best is None or km < best[1]:
                best = (el, km)
        return best if best and best[1] <= MAX_MATCH_DISTANCE_KM else None


LOOKUP_RADIUS_M = 8000


def overpass_lookup(entries: list[tuple[dict[str, Any], LatLon]]) -> None:  # pragma: no cover
    """Search within 8 km of the neighborhood for entries missing from the export."""
    settings = get_settings()
    cache: dict[str, list[dict[str, Any]]] = (
        json.loads(LOOKUPS.read_text(encoding="utf-8")) if LOOKUPS.exists() else {}
    )
    with httpx.Client(headers={"User-Agent": settings.http_user_agent}, timeout=120) as client:
        for entry, centre in entries:
            if entry["id"] in cache:
                continue
            rx = entry["match"].replace('"', "")
            around = f"around:{LOOKUP_RADIUS_M},{centre.lat},{centre.lon}"
            q = (
                f'[out:json][timeout:90];(nwr({around})["name"~"{rx}",i];'
                f'nwr({around})["name:en"~"{rx}",i];);out tags center 20;'
            )
            try:
                resp = client.post(settings.overpass_url, data={"data": q})
            except httpx.HTTPError as exc:
                print(f"  lookup {entry['id']}: {type(exc).__name__}, skipped")
                continue
            if resp.status_code != 200:
                print(f"  lookup {entry['id']}: HTTP {resp.status_code}, skipped")
                time_mod.sleep(10)
                continue
            found = []
            for el in resp.json().get("elements", []):
                lat = el.get("lat") or el.get("center", {}).get("lat")
                lon = el.get("lon") or el.get("center", {}).get("lon")
                if lat is None:
                    continue
                tags = el.get("tags", {})
                keep = ("name", "name:en", "opening_hours", "website", "cuisine", "tourism")
                found.append(
                    {
                        "osm": f"{el['type']}/{el['id']}",
                        "lat": round(lat, 6),
                        "lon": round(lon, 6),
                        "tags": {k: tags[k] for k in keep if k in tags},
                    }
                )
            cache[entry["id"]] = found
            print(f"  lookup {entry['id']}: {len(found)} candidates")
            LOOKUPS.write_text(
                json.dumps(cache, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
            time_mod.sleep(3)


# ------------------------------------------------------------------ building records
@dataclass
class Report:
    matched: int = 0
    fallback: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    hours_osm: int = 0
    hours_curated: int = 0
    hours_unknown: list[str] = field(default_factory=list)
    far: list[str] = field(default_factory=list)


def _locate(
    entry: dict[str, Any], hood: Neighborhood, osm: OsmIndex, report: Report
) -> tuple[LatLon, Source, str | None, dict[str, Any]] | None:
    hit = osm.find(entry["match"], hood.location)
    if hit:
        el, km = hit
        report.matched += 1
        if km > 3.0:
            report.far.append(f"{entry['id']} ({km:.1f} km from {hood.id})")
        url = f"https://www.openstreetmap.org/{el['osm']}"
        return LatLon(lat=el["lat"], lon=el["lon"]), Source.VERIFIED, url, el["tags"]
    if "fallback_location" in entry:
        lat, lon = entry["fallback_location"]
        report.fallback.append(entry["id"])
        return (
            LatLon(lat=lat, lon=lon),
            Source.ESTIMATED,
            "approximate location (not in OSM export)",
            {},
        )
    report.missing.append(entry["id"])
    return None


def _hours(entry: dict[str, Any], tags: dict[str, Any], report: Report) -> OpeningHours | None:
    parsed = None if entry.get("hours_from") == "curated" else osm_hours(tags.get("opening_hours"))
    if parsed is not None:
        report.hours_osm += 1
        return parsed
    curated = curated_hours(entry.get("hours"))
    if curated is not None:
        report.hours_curated += 1
    else:
        report.hours_unknown.append(entry["id"])
    return curated


def build_neighborhoods() -> list[Neighborhood]:
    raw = json.loads((CURATION / "neighborhoods.json").read_text(encoding="utf-8"))
    return [
        Neighborhood(
            id=h["id"],
            name=h["name"],
            location=LatLon(lat=h["lat"], lon=h["lon"]),
            vibe=h["vibe"],
            metro_station=h["metro_station"],
            beach_access=h["beach_access"],
        )
        for h in raw["neighborhoods"]
    ]


def build_attractions(
    hoods: dict[str, Neighborhood], osm: OsmIndex, report: Report
) -> list[Attraction]:
    raw = json.loads((CURATION / "attractions.json").read_text(encoding="utf-8"))
    refs = raw["price_references"]
    out = []
    for entry in raw["attractions"]:
        hood = hoods[entry["neighborhood_id"]]
        located = _locate(entry, hood, osm, report)
        if located is None:
            continue
        location, source, reference, tags = located
        if not INTERESTS & set(entry["tags"]):
            raise ValueError(f"{entry['id']}: tags must include at least one Interest value")
        lo, hi = entry["price"]
        out.append(
            Attraction(
                id=entry["id"],
                name=entry["name"],
                category=entry["category"],
                neighborhood_id=hood.id,
                location=location,
                tags=entry["tags"] + (["free"] if hi == 0 else []),
                crowd_level=entry["crowd_level"],
                opening_hours=_hours(entry, tags, report),
                typical_duration_min=entry["duration"],
                price=SourcedValue[PriceRange].model_validate(
                    {"value": {"min_aed": lo, "max_aed": hi}, **refs[entry["price_ref"]]}
                ),
                source=source,
                reference=reference,
                indoor=entry["indoor"],
            )
        )
    return out


def build_restaurants(
    hoods: dict[str, Neighborhood], osm: OsmIndex, report: Report
) -> list[Restaurant]:
    raw = json.loads((CURATION / "restaurants.json").read_text(encoding="utf-8"))
    bands = raw["price_bands"]
    out = []
    for entry in raw["restaurants"]:
        hood = hoods[entry["neighborhood_id"]]
        located = _locate(entry, hood, osm, report)
        if located is None:
            continue
        location, source, reference, tags = located
        lo, hi = bands[entry["band"]]
        out.append(
            Restaurant(
                id=entry["id"],
                name=entry["name"],
                category="restaurant",
                neighborhood_id=hood.id,
                location=location,
                tags=["food", *entry["cuisine"], *entry["tags"]],
                crowd_level=entry["crowd_level"],
                opening_hours=_hours(entry, tags, report),
                typical_duration_min=RESTAURANT_DURATION[entry["band"]],
                price=SourcedValue[PriceRange](
                    value=PriceRange(min_aed=lo, max_aed=hi),
                    source=Source.ESTIMATED,
                    reference=raw["price_reference"],
                ),
                source=source,
                reference=reference,
                cuisine=entry["cuisine"],
                meal_types=[MealType(m) for m in entry["meals"]],
                price_band=PriceBand(entry["band"]),
            )
        )
    return out


def _write(name: str, key: str, items: Iterable[Any], note: str) -> Path:  # pragma: no cover
    path = SEED / f"{name}.json"
    payload = {"_note": note, key: [i.model_dump(mode="json", exclude_none=True) for i in items]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def main(argv: list[str]) -> int:  # pragma: no cover
    if "--lookup" in argv:
        osm = OsmIndex.load()
        hoods = {h.id: h for h in build_neighborhoods()}
        pending = []
        for fname, key in (("attractions", "attractions"), ("restaurants", "restaurants")):
            for e in json.loads((CURATION / f"{fname}.json").read_text(encoding="utf-8"))[key]:
                centre = hoods[e["neighborhood_id"]].location
                if osm.find(e["match"], centre) is None:
                    pending.append((e, centre))
        print(f"looking up {len(pending)} places across Dubai")
        overpass_lookup(pending)

    osm = OsmIndex.load()
    neighborhoods = build_neighborhoods()
    hoods = {h.id: h for h in neighborhoods}
    report = Report()
    attractions = build_attractions(hoods, osm, report)
    restaurants = build_restaurants(hoods, osm, report)

    generated = (
        "Generated by data/scripts/curate.py from data/curation/ and the OSM export. "
        "Do not edit by hand."
    )
    SEED.mkdir(parents=True, exist_ok=True)
    _write("neighborhoods", "neighborhoods", neighborhoods, generated)
    _write(
        "attractions",
        "attractions",
        attractions,
        generated + " OSM data (c) OpenStreetMap contributors, ODbL.",
    )
    _write(
        "restaurants",
        "restaurants",
        restaurants,
        generated + " OSM data (c) OpenStreetMap contributors, ODbL.",
    )

    print(f"neighborhoods: {len(neighborhoods)}")
    print(f"attractions:   {len(attractions)}   restaurants: {len(restaurants)}")
    print(f"matched in OSM: {report.matched}   fallback location: {report.fallback}")
    print(
        f"hours from OSM: {report.hours_osm}   curated: {report.hours_curated}   "
        f"unknown: {len(report.hours_unknown)}"
    )
    if report.far:
        print("matched > 3 km from the neighborhood centre (check):", report.far)
    if report.missing:
        print("NOT FOUND (add fallback_location or fix 'match'):", report.missing)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
