"""Travel data (dev mode): sources, freshness, data mode, live-source and AI status, caches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from app.context import app_context
from core.llm import quota_guard
from data.scripts.staleness_report import scan, stale

SOURCES = [
    (
        "Places (attractions, restaurants)",
        "Curated, matched to OpenStreetMap",
        "ODbL (© OpenStreetMap contributors)",
    ),
    ("Live places (hybrid/live)", "Overpass API (OpenStreetMap)", "ODbL"),
    (
        "Road and walking times",
        "OSRM (stored tables; live in hybrid/live)",
        "OSRM BSD-2; data ODbL",
    ),
    (
        "Metro, bus, abra fares; taxi tariffs",
        "RTA and Dubai Taxi official tables",
        "Public information",
    ),
    ("Hotel areas and fees", "Curated estimates", "Project data"),
    ("Climate and forecasts", "Open-Meteo (ERA5 normals; forecast in hybrid/live)", "CC BY 4.0"),
]


def folder_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def freshness(months: int = 6) -> dict[str, Any]:
    items, sources = scan()
    old = stale(items, months)
    return {"sources": dict(sources), "dated": len(items), "stale": old, "months": months}


def page() -> None:
    ctx = app_context()
    st.title("Travel data")
    st.caption("Dev mode: where the data comes from and how fresh it is.")
    st.subheader("Sources")
    st.dataframe(
        [{"data": d, "source": s, "licence": lic} for d, s, lic in SOURCES], hide_index=True
    )
    st.subheader("Freshness")
    months = st.slider("Flag values older than (months)", 1, 24, 6)
    f = freshness(months)
    a, b, c = st.columns(3)
    a.metric("Dated values", f["dated"])
    b.metric(f"Older than {months} months", len(f["stale"]))
    c.metric("Provenance tags", ", ".join(f"{k} {v}" for k, v in f["sources"].items()))
    if f["stale"]:
        st.dataframe(
            [
                {"as_of": d.as_of, "file": d.file, "value": d.path, "source": d.source}
                for d in f["stale"]
            ],
            hide_index=True,
        )
    st.subheader("Data mode and live sources")
    s = ctx.settings
    st.markdown(
        f"- Mode: **{ctx.mode.value}**\n"
        f"- Overpass: {'on' if s.use_overpass else 'off'} · OSRM: {'on' if s.use_osrm else 'off'}"
        f" · Open-Meteo: {'on' if s.use_open_meteo else 'off'}\n"
        f"- Live-call budget per run (hybrid): {s.external_call_budget}"
    )
    st.subheader("AI allowance and caches")
    q = quota_guard(s).status()
    st.progress(min(1.0, q["share"]), text=f"{q['used']:,} of {q['budget']:,} LLM tokens today")
    if q["paused"]:
        st.warning(f"AI planning is paused: {q['reason']}")
    http_dir, llm_dir = s.cache_dir / "http", s.cache_dir / "llm"
    a, b = st.columns(2)
    a.metric("Live-data cache", f"{folder_size(http_dir) / 1e6:.1f} MB")
    b.metric("LLM answer cache", f"{folder_size(llm_dir) / 1e6:.1f} MB")
    if ctx.providers.http is not None and st.button("Clear the live-data cache"):
        ctx.providers.http.clear()
        st.toast("Live-data cache cleared (it refills on the next hybrid/live run).")
