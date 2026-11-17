"""Logistics agent (parallel phase, architecture.md §5.1): hotel area, transport, travel matrix.

It runs without knowing which attractions Destination will pick, so it ranks hotel areas by
how central they are to the places that match the traveller's interests:

- **Rank** (code): metro access first, then average taxi time to matching places, then price.
  On revision, ``hotel_cost_driver`` puts price first and ``long_leg`` / ``day_transit_heavy``
  put centrality first.
- **Choose and explain** (LLM): reorder the areas and explain the trade-offs using only the
  data. Unknown areas are ignored; areas the LLM leaves out keep their deterministic order.
- **Transport options** come from the fare and tariff tables, so the numbers are exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from pydantic import Field

from agents.base import AgentContext, feedback, to_json
from agents.skills.calendar import trip_month
from agents.skills.scheduler import taxi_minutes_between
from core.tracing import TraceKind
from schemas import (
    HotelArea,
    Interest,
    LogisticsResult,
    ReviewIssue,
    TaxiKind,
    TransportMode,
    TransportOption,
    TripRequest,
)
from schemas.base import StrictModel
from schemas.pricing import FareProduct, ZoneBand
from tools import get_hotel_areas, list_neighborhoods, search_attractions, travel_matrix
from tools.views import hotel_view

AGENT = "logistics"


class LogisticsChoice(StrictModel):
    hotel_ranking: list[str] = Field(description="hotel area (neighborhood) ids, best first")
    rationale: dict[str, str] = Field(default_factory=dict, description="area id -> trade-offs")
    transport_notes: str = ""


@dataclass
class LogisticsAgent:
    ctx: AgentContext

    def plan(
        self,
        request: TripRequest,
        *,
        month: int | None = None,
        issues: list[ReviewIssue] | None = None,
        current_area: str | None = None,
    ) -> LogisticsResult:
        """Rank hotel areas. On a revision, ``current_area`` (the area Review blamed) goes last,
        whatever the LLM prefers, so the revision actually changes something."""
        issues = issues or []
        c = request.constraints
        self.ctx.emit(
            AGENT, TraceKind.START, f"Choosing a {c.hotel_tier.value} hotel area and transport"
        )
        p = self.ctx.providers
        areas = get_hotel_areas(c.hotel_tier, month=trip_month(c, month), providers=p)
        hoods = {h.id: h for h in list_neighborhoods(providers=p)}

        # how central is each area to places matching the interests?
        interests: list[Interest] = [i for i in c.interests if i is not Interest.FOOD]
        interests = interests or list(c.interests)
        targets = search_attractions(interests, avoid_crowds=c.avoids_crowds, limit=40, providers=p)
        centrality = {
            a.neighborhood_id: round(
                mean(
                    taxi_minutes_between(hoods[a.neighborhood_id].location, t.location)
                    for t in targets
                )
                if targets
                else 0.0,
                1,
            )
            for a in areas
        }
        codes = {i.code for i in issues}

        def rank(a: HotelArea) -> tuple[float, ...]:
            price, central = a.nightly_rate.value.midpoint, centrality[a.neighborhood_id]
            if "hotel_cost_driver" in codes:  # the hotel is why we're over budget
                return (price, central)
            if codes & {"long_leg", "day_transit_heavy"}:  # days spend too long travelling
                return (central, price)
            return (float(not a.metro_access), round(central / 5), price)

        ranked = sorted(areas, key=lambda a: (rank(a), a.neighborhood_id))

        choice = self.ctx.ask(
            "logistics",
            LogisticsChoice,
            prompt="logistics",
            user=to_json(
                {
                    "request": {
                        "days": c.days,
                        "travelers": c.travelers,
                        "rooms": c.rooms,
                        "hotel_tier": c.hotel_tier.value,
                        "interests": [i.value for i in c.interests],
                        "avoid": [a.value for a in c.avoid],
                        "budget_aed": c.budget_aed,
                    },
                    "hotel_areas": [
                        {
                            **hotel_view(a),
                            "avg_taxi_min_to_matching_places": centrality[a.neighborhood_id],
                        }
                        for a in ranked
                    ],
                }
            ),
            revision_feedback=feedback(issues),
        )
        rationale: dict[str, str] = {}
        notes = ""
        if choice is not None:
            by_id = {a.neighborhood_id: a for a in ranked}
            order = [by_id[i] for i in dict.fromkeys(choice.hotel_ranking) if i in by_id]
            ranked = order + [a for a in ranked if a not in order]
            rationale = {k: v for k, v in choice.rationale.items() if k in by_id}
            notes = choice.transport_notes
            unknown = [i for i in choice.hotel_ranking if i not in by_id]
            if unknown:
                self.ctx.emit(
                    AGENT, TraceKind.ERROR, f"Ignored unknown hotel areas: {', '.join(unknown)}"
                )
        if issues and current_area is not None and len(ranked) > 1:
            ranked = [a for a in ranked if a.neighborhood_id != current_area] + [
                a for a in ranked if a.neighborhood_id == current_area
            ]
        ranked = [
            a.model_copy(update={"rationale": rationale.get(a.neighborhood_id, a.rationale)})
            for a in ranked
        ]
        result = LogisticsResult(
            hotel_areas=ranked,
            transport_options=self._transport(),
            travel_matrix=travel_matrix(sorted(hoods), providers=p),
            notes=notes,
        )
        best = result.recommended_hotel
        self.ctx.emit(
            AGENT,
            TraceKind.OUTPUT,
            f"Hotel area: {best.neighborhood_id} (AED {best.nightly_rate.value.min_aed:,.0f}-"
            f"{best.nightly_rate.value.max_aed:,.0f}/night, "
            f"metro {'yes' if best.metro_access else 'no'}); "
            f"{len(result.travel_matrix.entries)} travel-matrix entries",
            {"hotel_ranking": [a.neighborhood_id for a in ranked]},
        )
        return result

    def _transport(self) -> list[TransportOption]:
        pricing = self.ctx.providers.pricing
        out = []
        fares = [
            pricing.transit_fare(TransportMode.METRO, FareProduct.SILVER, band)
            for band in (ZoneBand.ONE_ZONE, ZoneBand.TWO_ZONES, ZoneBand.MULTI_ZONE)
        ]
        if all(fares):
            out.append(
                TransportOption(
                    mode=TransportMode.METRO,
                    summary="Dubai Metro with a Silver nol card: AED "
                    + " / ".join(f"{f.fare_aed:g}" for f in fares if f)
                    + " for 1 / 2 / 3+ zones",
                    notes=(
                        "Cheapest for longer trips along Sheikh Zayed Road "
                        "and between Deira and Bur Dubai"
                    ),
                )
            )
        taxi = pricing.taxi_tariff(TaxiKind.STREET_DAY)
        if taxi is not None:
            out.append(
                TransportOption(
                    mode=TransportMode.TAXI,
                    summary=f"RTA taxi: AED {taxi.flagfall_aed:g} flag-fall "
                    f"+ AED {taxi.per_km_aed:g}/km, minimum AED {taxi.minimum_aed:g}",
                    notes="Fastest door to door; up to 4 passengers share a fare; Salik extra",
                )
            )
        abra = pricing.transit_fare(TransportMode.ABRA, FareProduct.CASH, ZoneBand.FLAT)
        if abra is not None:
            out.append(
                TransportOption(
                    mode=TransportMode.ABRA,
                    summary=f"Abra across Dubai Creek: AED {abra.fare_aed:g}",
                    notes="Between Bur Dubai / Al Shindagha and the Deira souks",
                )
            )
        out.append(
            TransportOption(
                mode=TransportMode.WALK,
                summary="Walking for short hops within the old town and along the waterfronts",
            )
        )
        return out
