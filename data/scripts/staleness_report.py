"""List seed values whose ``as_of`` date is older than N months, and count sources.

Usage::

    uv run python -m data.scripts.staleness_report [MONTHS]     # default 6

Exit code 1 when anything is stale, so it can gate a demo build.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from data.dataset import SEED_DIR


@dataclass(frozen=True)
class Dated:
    file: str
    path: str
    source: str
    as_of: date
    reference: str


def _walk(node: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        if "source" in node:
            yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            label = v.get("id") if isinstance(v, dict) and isinstance(v.get("id"), str) else i
            yield from _walk(v, f"{path}[{label}]")


def scan(seed_dir: Path = SEED_DIR) -> tuple[list[Dated], Counter[str]]:
    dated: list[Dated] = []
    sources: Counter[str] = Counter()
    for f in sorted(seed_dir.glob("*.json")):
        for path, node in _walk(json.loads(f.read_text(encoding="utf-8")), ""):
            src = node.get("source")
            if src not in ("verified", "api", "estimated"):
                continue  # e.g. a Place's "source" is the record source, counted the same way
            sources[src] += 1
            if node.get("as_of"):
                dated.append(
                    Dated(
                        f.name,
                        path,
                        src,
                        date.fromisoformat(node["as_of"]),
                        node.get("reference", ""),
                    )
                )
    return dated, sources


def months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) - (b.day < a.day)


def stale(items: list[Dated], months: int, today: date | None = None) -> list[Dated]:
    today = today or date.today()
    return [d for d in items if months_between(d.as_of, today) >= months]


def main(argv: list[str]) -> int:  # pragma: no cover
    months = int(argv[0]) if argv else 6
    items, sources = scan()
    old = stale(items, months)
    print(f"source tags: {dict(sources)}")
    print(f"dated values: {len(items)}; older than {months} months: {len(old)}")
    for d in sorted(old, key=lambda d: d.as_of):
        print(f"  {d.as_of}  {d.file}:{d.path}  ({d.source}) {d.reference[:80]}")
    return 1 if old else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
