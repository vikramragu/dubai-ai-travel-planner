"""Monthly climate normals for Dubai from the free Open-Meteo historical archive (ERA5).

Writes ``data/seed/climate.json``. The local weather provider uses these averages for any
trip date (and, from Phase 7, for dates beyond the live forecast range).

Usage::

    uv run python -m data.scripts.climate_normals [START_YEAR END_YEAR]   # default 2015 2024

Data: Open-Meteo.com (CC BY 4.0), based on Copernicus ERA5 reanalysis.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean

import httpx

from core.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "seed" / "climate.json"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DUBAI = (25.2048, 55.2708)  # city centre


def fetch(
    start_year: int, end_year: int
) -> dict[str, list[float | str | None]]:  # pragma: no cover
    params: dict[str, str | float] = {
        "latitude": DUBAI[0],
        "longitude": DUBAI[1],
        "start_date": f"{start_year}-01-01",
        "end_date": f"{end_year}-12-31",
        "daily": (
            "temperature_2m_max,temperature_2m_min,precipitation_sum,relative_humidity_2m_mean"
        ),
        "timezone": "Asia/Dubai",
    }
    headers = {"User-Agent": get_settings().http_user_agent}
    resp = httpx.get(ARCHIVE_URL, params=params, headers=headers, timeout=120)
    resp.raise_for_status()
    daily: dict[str, list[float | str | None]] = resp.json()["daily"]
    return daily


def normals(
    daily: dict[str, list[float | str | None]], start: int, end: int
) -> list[dict[str, object]]:
    by_month: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    rain_by_year_month: dict[tuple[int, int], list[float]] = defaultdict(list)
    for i, day in enumerate(daily["time"]):
        d = date.fromisoformat(str(day))
        bucket = by_month[d.month]
        for key in ("temperature_2m_max", "temperature_2m_min", "relative_humidity_2m_mean"):
            v = daily[key][i]
            if v is not None:
                bucket[key].append(float(v))
        p = daily["precipitation_sum"][i]
        rain_by_year_month[(d.year, d.month)].append(float(p) if p is not None else 0.0)

    years = end - start + 1
    as_of = datetime.now(UTC).date().isoformat()
    reference = (
        f"Open-Meteo historical archive (ERA5), {DUBAI[0]},{DUBAI[1]}, "
        f"daily {start}-{end}: {ARCHIVE_URL}"
    )
    out = []
    for month in range(1, 13):
        b = by_month[month]
        if not b["temperature_2m_max"]:
            continue  # no data for this month (partial range); Dataset.problems() flags gaps
        month_totals = [sum(v) for (_, m), v in rain_by_year_month.items() if m == month]
        rainy = [
            sum(1 for x in v if x >= 1.0) for (_, m), v in rain_by_year_month.items() if m == month
        ]
        out.append(
            {
                "month": month,
                "temp_max_c": round(mean(b["temperature_2m_max"]), 1),
                "temp_min_c": round(mean(b["temperature_2m_min"]), 1),
                "precipitation_mm": round(sum(month_totals) / years, 1),
                "rainy_days": round(sum(rainy) / years, 1),
                "humidity_pct": round(mean(b["relative_humidity_2m_mean"]), 0),
                "source": "verified",
                "reference": reference,
                "as_of": as_of,
            }
        )
    return out


def main(argv: list[str]) -> int:  # pragma: no cover
    start, end = (int(argv[0]), int(argv[1])) if len(argv) == 2 else (2015, 2024)
    months = normals(fetch(start, end), start, end)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "_note": "Monthly climate normals. Data: Open-Meteo.com (CC BY 4.0), ERA5.",
                "months": months,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    for m in months:
        print(
            m["month"], m["temp_max_c"], m["temp_min_c"], m["precipitation_mm"], m["humidity_pct"]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
