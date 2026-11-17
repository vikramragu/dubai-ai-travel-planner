"""Destination Research agent: candidate attractions and restaurants for the request.

1. **Gather** (code): search the tools per interest, honour "avoid crowds", drop seasonal
   places out of season and anything a previous review flagged; restaurants by tier, local
   cuisines and diet.
2. **Coverage check** (code): an interest with fewer than 3 suitable places is recorded as
   "limited data", so Review doesn't keep asking for something the dataset can't give.
3. **Rank and justify** (LLM): pick from the compact candidate list and explain each pick using
   only dataset fields. Unknown ids are dropped (hallucination guard) and a short list is topped
   up from the ranked pool. Without an LLM the ranked pool is used as is.

The scheduler later chooses what fits from these picks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import Field

from agents.base import AgentContext, feedback, to_json
from agents.skills.calendar import SEASONAL_MONTHS, trip_calendar
from agents.skills.scheduler import MEAL_BANDS, score, suits_diet
from core.tracing import TraceKind
from data.dataset import LOCAL_CUISINES
from schemas import (
    Attraction,
    CrowdLevel,
    Interest,
    ResearchResult,
    ReviewIssue,
    TripRequest,
)
from schemas.base import StrictModel
from tools import search_attractions, search_restaurants
from tools.views import attraction_view, restaurant_view

AGENT = "destination"
MIN_PER_INTEREST = 3  # fewer suitable places than this is "limited data"
STOPS_PER_DAY = 4  # picks to offer the scheduler per trip day (it keeps what fits)
# Review findings that mean "don't offer this place again".
EXCLUDE_CODES = {"crowded_place", "duplicate_place", "closed_for_season", "unknown_place"}


class DestinationPicks(StrictModel):
    attraction_ids: list[str] = Field(description="chosen attraction ids, best first")
    restaurant_ids: list[str] = Field(
        default_factory=list, description="recommended restaurant ids"
    )
    rationale: dict[str, str] = Field(
        default_factory=dict, description="place id -> one sentence citing only the given fields"
    )


@dataclass
class DestinationResult:
    research: ResearchResult
    assumptions: list[str] = field(default_factory=list)
    dropped_ids: list[str] = field(default_factory=list)  # unknown ids returned by the LLM


@dataclass
class DestinationAgent:
    ctx: AgentContext

    def research(
        self,
        request: TripRequest,
        *,
        month: int | None = None,
        issues: list[ReviewIssue] | None = None,
        exclude_ids: frozenset[str] = frozenset(),
    ) -> DestinationResult:
        issues = issues or []
        c = request.constraints
        self.ctx.emit(AGENT, TraceKind.START, "Researching attractions and restaurants")
        trip_month = trip_calendar(c, month)[0].month
        excluded = set(exclude_ids) | {
            i.place_id for i in issues if i.code in EXCLUDE_CODES and i.place_id
        }

        # 1. gather
        pool: dict[str, Attraction] = {}
        for interest in c.interests:
            if interest is Interest.FOOD:
                continue
            for a in search_attractions(
                [interest],
                avoid_crowds=c.avoids_crowds,
                exclude_ids=sorted(excluded),
                limit=15,
                providers=self.ctx.providers,
            ):
                if "seasonal" in a.tags and trip_month not in SEASONAL_MONTHS:
                    continue
                pool.setdefault(a.id, a)
        if Interest.FOOD in c.interests:  # food-themed sights (spice souk, cultural meals)
            for a in search_attractions(
                [Interest.FOOD],
                avoid_crowds=c.avoids_crowds,
                exclude_ids=sorted(excluded),
                limit=5,
                providers=self.ctx.providers,
            ):
                pool.setdefault(a.id, a)
        ranked = sorted(pool.values(), key=lambda a: (-score(a, c, trip_month), a.id))
        restaurants = [
            r
            for r in search_restaurants(
                cuisines=sorted(LOCAL_CUISINES) if Interest.FOOD in c.interests else None,
                price_bands=list(MEAL_BANDS[c.hotel_tier]),
                avoid_crowds=c.avoids_crowds,
                exclude_ids=sorted(excluded),
                limit=40,
                providers=self.ctx.providers,
            )
            if suits_diet(r, c.dietary)
        ]

        # 2. coverage
        assumptions = []
        for interest in c.interests:
            if interest is Interest.FOOD:
                n = sum(bool(LOCAL_CUISINES & set(r.cuisine)) for r in restaurants)
            else:
                n = sum(interest.value in a.tags for a in ranked)
            if n < MIN_PER_INTEREST:
                why = " that aren't usually crowded" if c.avoids_crowds else ""
                assumptions.append(
                    f"Limited data for '{interest.value}': only {n} suitable place(s){why} "
                    "in the dataset"
                )
        if c.dietary and len(restaurants) < 3:
            assumptions.append(
                f"Few restaurants in the dataset are marked {', '.join(c.dietary)}; "
                "some meals may be far from the day's sights"
            )

        # 3. rank and justify
        wanted = c.days * STOPS_PER_DAY
        picks = self.ctx.ask(
            "destination",
            DestinationPicks,
            prompt="destination",
            user=to_json(
                {
                    "request": _request_view(request),
                    "pick_about": wanted,
                    "attractions": [attraction_view(a) for a in ranked],
                    "restaurants": [restaurant_view(r) for r in restaurants],
                }
            ),
            revision_feedback=feedback(issues),
        )
        dropped: list[str] = []
        rationale: dict[str, str] = {}
        if picks is None:
            chosen = ranked
            chosen_r = restaurants
        else:
            by_id = {a.id: a for a in ranked}
            by_r = {r.id: r for r in restaurants}
            dropped = [
                i
                for i in [*picks.attraction_ids, *picks.restaurant_ids]
                if i not in by_id and i not in by_r
            ]
            chosen = list({i: by_id[i] for i in picks.attraction_ids if i in by_id}.values())
            for a in ranked:  # top up a short list from the ranked pool
                if len(chosen) >= wanted:
                    break
                if a not in chosen:
                    chosen.append(a)
            chosen_r = [
                by_r[i] for i in dict.fromkeys(picks.restaurant_ids) if i in by_r
            ] or restaurants
            known = {a.id for a in chosen} | {r.id for r in chosen_r}
            rationale = {k: v for k, v in picks.rationale.items() if k in known}
            if dropped:
                self.ctx.emit(
                    AGENT,
                    TraceKind.ERROR,
                    f"Ignored {len(dropped)} id(s) not returned by the tools: "
                    + ", ".join(dropped[:5]),
                )
        for a in chosen:
            rationale.setdefault(a.id, _why(a, request))

        research = ResearchResult(
            attractions=chosen,
            restaurants=chosen_r,
            neighborhood_ids=sorted({a.neighborhood_id for a in chosen}),
            rationale=rationale,
        )
        self.ctx.emit(
            AGENT,
            TraceKind.OUTPUT,
            f"{len(chosen)} attractions, {len(chosen_r)} restaurants in "
            f"{len(research.neighborhood_ids)} areas"
            + (f"; {len(assumptions)} coverage note(s)" if assumptions else ""),
            {"attractions": [a.id for a in chosen], "assumptions": assumptions},
        )
        return DestinationResult(research, assumptions, dropped)


def _request_view(request: TripRequest) -> dict[str, object]:
    c = request.constraints
    return {
        "days": c.days,
        "interests": [i.value for i in c.interests],
        "avoid": [a.value for a in c.avoid],
        "pace": c.pace.value,
        "hotel_tier": c.hotel_tier.value,
        "travelers": c.travelers,
        "dietary": c.dietary,
        "budget_aed": c.budget_aed,
    }


def _why(a: Attraction, request: TripRequest) -> str:
    """Deterministic rationale from dataset fields only."""
    c = request.constraints
    matched = [i.value for i in c.interests if i.value in a.tags]
    crowd = {
        CrowdLevel.LOW: "usually quiet",
        CrowdLevel.MEDIUM: "moderately busy",
        CrowdLevel.HIGH: "often crowded",
    }[a.crowd_level]
    price = "free" if a.is_free else f"about AED {a.price.value.midpoint:,.0f} per person"
    return f"Matches {', '.join(matched) or 'your interests'}; {crowd}; {price}."
