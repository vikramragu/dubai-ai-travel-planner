"""Compact views for prompts and the LangChain tool wrappers."""

from __future__ import annotations

import json
from datetime import time

from data.providers import DataProviders
from schemas import Attraction, OpeningHours, Restaurant, TimeWindow, Weekday
from tools.langchain_tools import dubai_tools
from tools.views import attraction_view, hours_summary, price_summary, restaurant_view


def test_hours_summary() -> None:
    assert hours_summary(None) == "unknown"
    assert hours_summary(OpeningHours(always_open=True)) == "always open"
    w = TimeWindow(opens=time(8, 30), closes=time(20, 30))
    f = TimeWindow(opens=time(14, 30), closes=time(20, 30))
    hours = OpeningHours(
        weekly={**{d: [w] for d in Weekday if d is not Weekday.FRI}, Weekday.FRI: [f]}
    )
    assert hours_summary(hours) == "Mon,Tue,Wed,Thu,Sat,Sun 08:30-20:30; Fri 14:30-20:30"
    daily = OpeningHours(weekly={d: [w] for d in Weekday})
    assert hours_summary(daily) == "daily 08:30-20:30"


def test_views_are_compact(providers: DataProviders) -> None:
    museum = providers.places.get_place("al-shindagha-museum")
    ravi = providers.places.get_place("ravi-satwa")
    assert isinstance(museum, Attraction) and isinstance(ravi, Restaurant)
    view = attraction_view(museum)
    assert view["price_pp"] == "AED 50 (verified)"
    assert "location" not in view and "reference" not in view
    assert price_summary(providers.places.get_place("kite-beach").price) == "free"  # type: ignore[union-attr]
    assert restaurant_view(ravi)["hours"] == "unknown"
    full = len(museum.model_dump_json())
    assert len(json.dumps(view)) < full / 2  # the point of a compact view


def test_langchain_tools(providers: DataProviders) -> None:
    tools = {t.name: t for t in dubai_tools(providers)}
    assert set(tools) == {
        "search_attractions",
        "search_restaurants",
        "get_hotel_areas",
        "travel_time",
        "price_lookup",
    }
    found = tools["search_attractions"].invoke({"interests": ["beach"], "limit": 3})
    assert len(found) == 3 and all("beach" in a["tags"] for a in found)
    hotels = tools["get_hotel_areas"].invoke({"tier": "budget", "month": 12})
    assert hotels and {"area", "nightly", "metro"} <= set(hotels[0])
    legs = tools["travel_time"].invoke({"from_id": "al-ras", "to_id": "al-fahidi"})
    assert legs[0]["minutes"] <= legs[-1]["minutes"]
    assert tools["price_lookup"].invoke({"place_id": "etihad-museum"}) == "AED 25 (verified)"
    rest = tools["search_restaurants"].invoke({"cuisines": ["emirati"], "limit": 2})
    assert all("emirati" in r["cuisine"] for r in rest)
