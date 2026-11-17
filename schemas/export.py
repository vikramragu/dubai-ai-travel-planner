"""JSON-schema export for prompts and docs.

Usage: ``uv run python -m schemas.export [OUT_DIR]`` (default ``build/schemas``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.tracing import RunMetrics, TraceEvent
from schemas.budget import BudgetAlternative, BudgetBreakdown
from schemas.itinerary import Itinerary, ItinerarySnapshot, ResearchResult
from schemas.logistics import LogisticsResult
from schemas.places import Attraction, HotelArea, Neighborhood, Restaurant
from schemas.pricing import HotelFees, TaxiTariff, TransitFare
from schemas.request import TripConstraints, TripRequest
from schemas.review import ReviewReport
from schemas.weather import DayWeather

EXPORTED_MODELS: dict[str, type[BaseModel]] = {
    "TripConstraints": TripConstraints,
    "TripRequest": TripRequest,
    "Neighborhood": Neighborhood,
    "Attraction": Attraction,
    "Restaurant": Restaurant,
    "HotelArea": HotelArea,
    "TransitFare": TransitFare,
    "TaxiTariff": TaxiTariff,
    "HotelFees": HotelFees,
    "DayWeather": DayWeather,
    "ResearchResult": ResearchResult,
    "LogisticsResult": LogisticsResult,
    "BudgetBreakdown": BudgetBreakdown,
    "BudgetAlternative": BudgetAlternative,
    "ReviewReport": ReviewReport,
    "Itinerary": Itinerary,
    "ItinerarySnapshot": ItinerarySnapshot,
    "TraceEvent": TraceEvent,
    "RunMetrics": RunMetrics,
}


def json_schemas() -> dict[str, dict[str, Any]]:
    return {name: model.model_json_schema() for name, model in EXPORTED_MODELS.items()}


def write_json_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, schema in json_schemas().items():
        path = out_dir / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str]) -> None:
    out_dir = Path(argv[0]) if argv else Path("build/schemas")
    for path in write_json_schemas(out_dir):
        print(path)


if __name__ == "__main__":
    main(sys.argv[1:])
