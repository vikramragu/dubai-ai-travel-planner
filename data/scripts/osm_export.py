"""One-off OpenStreetMap export: points of interest around each curated neighborhood.

Queries the public Overpass API once per neighborhood and writes a compact JSON file per
neighborhood to ``data/raw/osm/``. These exports are the starting list for curation
(``data/scripts/curate.py`` takes coordinates, OSM ids and tags from them).

Usage::

    uv run python -m data.scripts.osm_export            # all neighborhoods
    uv run python -m data.scripts.osm_export al-ras     # just one

Be polite to the shared public server: one query at a time, a pause between queries,
and a descriptive User-Agent. OSM data is (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from core.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
CURATION = ROOT / "curation" / "neighborhoods.json"
OUT_DIR = ROOT / "raw" / "osm"

# Tags we keep from each element (everything else is dropped to keep exports small).
KEEP_TAGS = (
    "name",
    "name:en",
    "tourism",
    "historic",
    "amenity",
    "leisure",
    "natural",
    "shop",
    "cuisine",
    "opening_hours",
    "website",
    "contact:website",
    "fee",
    "wheelchair",
)

QUERY = """
[out:json][timeout:60];
(
  nwr(around:{r},{lat},{lon})["tourism"~"^(museum|attraction|gallery|viewpoint|artwork|theme_park|zoo|aquarium)$"]["name"];
  nwr(around:{r},{lat},{lon})["historic"]["name"];
  nwr(around:{r},{lat},{lon})["natural"="beach"]["name"];
  nwr(around:{r},{lat},{lon})["leisure"~"^(beach_resort|park|marina|nature_reserve|water_park)$"]["name"];
  nwr(around:{r},{lat},{lon})["amenity"~"^(restaurant|cafe|fast_food|food_court|place_of_worship|marketplace|arts_centre|theatre)$"]["name"];
  nwr(around:{r},{lat},{lon})["shop"~"^(gold|jewelry|spices|perfumery|mall)$"]["name"];
);
out tags center;
"""


def _compact(el: dict[str, Any]) -> dict[str, Any] | None:
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    if lat is None or lon is None:
        return None
    tags = el.get("tags", {})
    return {
        "osm": f"{el['type']}/{el['id']}",
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "tags": {k: tags[k] for k in KEEP_TAGS if k in tags},
    }


def export(
    neighborhood: dict[str, Any], client: httpx.Client, url: str
) -> Path:  # pragma: no cover
    q = QUERY.format(
        r=neighborhood["search_radius_m"], lat=neighborhood["lat"], lon=neighborhood["lon"]
    )
    for attempt in range(4):
        resp = client.post(url, data={"data": q})
        if resp.status_code == 200:
            break
        wait = 15 * (attempt + 1)
        print(f"  {neighborhood['id']}: HTTP {resp.status_code}, retrying in {wait}s")
        time.sleep(wait)
    resp.raise_for_status()
    payload = resp.json()
    elements = [c for el in payload.get("elements", []) if (c := _compact(el))]
    elements.sort(key=lambda e: e["tags"].get("name:en") or e["tags"].get("name", ""))
    out = OUT_DIR / f"{neighborhood['id']}.json"
    out.write_text(
        json.dumps(
            {
                "neighborhood_id": neighborhood["id"],
                "exported_at": datetime.now(UTC).date().isoformat(),
                "osm_base": payload.get("osm3s", {}).get("timestamp_osm_base"),
                "license": "ODbL — (c) OpenStreetMap contributors",
                "query": " ".join(q.split()),
                "elements": elements,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def main(argv: list[str]) -> int:  # pragma: no cover
    settings = get_settings()
    hoods = json.loads(CURATION.read_text(encoding="utf-8"))["neighborhoods"]
    if argv:
        hoods = [h for h in hoods if h["id"] in set(argv)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": settings.http_user_agent}
    with httpx.Client(headers=headers, timeout=90) as client:
        for i, hood in enumerate(hoods):
            if i:
                time.sleep(3)  # be polite to the shared public server
            path = export(hood, client, settings.overpass_url)
            count = len(json.loads(path.read_text(encoding="utf-8"))["elements"])
            print(f"{hood['id']}: {count} elements -> {path.relative_to(ROOT.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
