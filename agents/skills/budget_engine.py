"""Budget engine: deterministic trip costing and cheaper alternatives (architecture.md §5.3).

All arithmetic happens here, never in the LLM:

- **Accommodation** = nightly midpoint x nights x rooms, plus service charge, municipality fee,
  VAT and the Tourism Dirham (``HotelFees``).
- **Food** = each meal slot's per-person midpoint x travellers.
- **Transport** = each leg's per-person cost x travellers, plus airport transfers.
- **Activities** = each activity's per-person midpoint x travellers.
- **Contingency** = ``contingency_pct`` of the *estimated* part of the budget (verified prices
  don't need a buffer).

Every cost line carries a source: ``verified`` only when every input was verified.

Alternatives are found by changing one planning choice, re-scheduling and re-costing, so each
saving is exact for that change: a cheaper hotel area, public transport instead of taxis, a
free attraction instead of a paid one with a shared interest, cheaper restaurant bands, or
dropping an expensive activity. :func:`fit_to_budget` applies them, biggest saving first, until
the plan fits.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from agents.skills.calendar import trip_month
from agents.skills.scheduler import (
    MEAL_BANDS,
    ScheduleResult,
    SchedulerOptions,
    TransportPolicy,
    schedule,
)
from data.heuristics import road_km
from data.providers import AttractionQuery, DataProviders
from schemas import (
    AlternativeKind,
    Attraction,
    BudgetAlternative,
    BudgetBreakdown,
    BudgetCategory,
    CostLine,
    CrowdLevel,
    HotelArea,
    HotelTier,
    Interest,
    Itinerary,
    LatLon,
    PriceBand,
    SlotKind,
    Source,
    SourcedValue,
    TaxiKind,
    TransportMode,
    TripRequest,
    estimated,
)
from tools import get_hotel_areas

DXB = LatLon(lat=25.2532, lon=55.3657)  # Dubai International Airport
TAXI_SEATS = 4
CHEAPER_BANDS = (PriceBand.STREET, PriceBand.CASUAL)
TIER_ORDER = [HotelTier.BUDGET, HotelTier.MID, HotelTier.LUXURY]
HOTEL_OPTIONS_PER_TIER = 2


@dataclass(frozen=True)
class BudgetOptions:
    contingency_pct: float = 10.0
    airport_transfers: bool = True


# ------------------------------------------------------------------ provenance helpers
def _combine(parts: Iterable[SourcedValue[float]], value: float, note: str) -> SourcedValue[float]:
    """A total is verified only when every part is verified; otherwise it's an estimate."""
    parts = list(parts)
    value = round(value, 2)
    if parts and all(p.source is Source.VERIFIED for p in parts):
        refs = sorted({p.reference or "" for p in parts})
        dates = [p.as_of for p in parts if p.as_of is not None]
        return SourcedValue[float](
            value=value,
            source=Source.VERIFIED,
            reference=f"{note}; " + "; ".join(refs),
            as_of=min(dates) if dates else None,
        )
    return estimated(value, note)


def _line(
    category: BudgetCategory,
    description: str,
    amount: SourcedValue[float],
    *,
    day: int | None = None,
    ref_id: str | None = None,
) -> CostLine:
    return CostLine(
        category=category, description=description, amount_aed=amount, day=day, ref_id=ref_id
    )


# ------------------------------------------------------------------ the cost lines
def accommodation_lines(itinerary: Itinerary, providers: DataProviders) -> list[CostLine]:
    c = itinerary.request.constraints
    hotel = itinerary.hotel
    nights, rooms = c.night_count, c.rooms
    if nights == 0:
        return []
    rate = hotel.nightly_rate.value.midpoint
    room_total = rate * nights * rooms
    fees = providers.hotels.fees(hotel.tier)
    pct = fees.service_charge_pct + fees.municipality_fee_pct + fees.vat_pct
    fee_total = room_total * pct / 100 + fees.tourism_dirham_per_room_night * nights * rooms
    rate_src = SourcedValue[float](
        value=rate,
        source=hotel.nightly_rate.source,
        reference=hotel.nightly_rate.reference,
        as_of=hotel.nightly_rate.as_of,
    )
    fee_src = SourcedValue[float](
        value=fee_total, source=fees.source, reference=fees.reference, as_of=fees.as_of
    )
    return [
        _line(
            BudgetCategory.ACCOMMODATION,
            f"{hotel.tier.value.title()} hotel in {hotel.neighborhood_id}: "
            f"{nights} night(s) x {rooms} room(s) x AED {rate:,.0f}",
            _combine([rate_src], room_total, "nightly range midpoint"),
            ref_id=f"{hotel.neighborhood_id}/{hotel.tier.value}",
        ),
        _line(
            BudgetCategory.ACCOMMODATION,
            f"Hotel fees: {pct:g}% service/municipality/VAT + Tourism Dirham "
            f"AED {fees.tourism_dirham_per_room_night:g}/room-night",
            _combine([fee_src], fee_total, "hotel fees"),
            ref_id=f"{hotel.neighborhood_id}/{hotel.tier.value}",
        ),
    ]


def place_lines(itinerary: Itinerary, providers: DataProviders) -> list[CostLine]:
    """Meals (food) and activities: per-person midpoint x travellers."""
    travelers = itinerary.request.constraints.travelers
    out = []
    for day in itinerary.days:
        for slot in day.slots:
            if slot.kind not in (SlotKind.MEAL, SlotKind.ACTIVITY) or slot.place_id is None:
                continue
            place = providers.places.get_place(slot.place_id)
            if place is None:
                raise ValueError(f"unknown place id in itinerary: {slot.place_id}")
            price = place.price
            each = price.value.midpoint
            if each == 0:
                continue
            src = SourcedValue[float](
                value=each, source=price.source, reference=price.reference, as_of=price.as_of
            )
            category = (
                BudgetCategory.FOOD if slot.kind is SlotKind.MEAL else BudgetCategory.ACTIVITIES
            )
            out.append(
                _line(
                    category,
                    f"{slot.title}: {travelers} x AED {each:,.0f}",
                    _combine([src], each * travelers, "per-person price x travellers"),
                    day=day.day,
                    ref_id=place.id,
                )
            )
    return out


def transport_lines(
    itinerary: Itinerary, providers: DataProviders, options: BudgetOptions
) -> list[CostLine]:
    travelers = itinerary.request.constraints.travelers
    out = []
    for day in itinerary.days:
        for slot in day.slots:
            if slot.kind is not SlotKind.TRANSIT or slot.leg is None:
                continue
            leg = slot.leg
            if leg.cost_aed.value == 0:
                continue
            out.append(
                _line(
                    BudgetCategory.TRANSPORT,
                    f"{slot.title} ({leg.mode.value}): {travelers} x AED {leg.cost_aed.value:,.2f}",
                    _combine(
                        [leg.cost_aed], leg.cost_aed.value * travelers, "leg cost x travellers"
                    ),
                    day=day.day,
                    ref_id=f"{leg.from_id}->{leg.to_id}",
                )
            )
    if options.airport_transfers:
        out += _airport_lines(itinerary, providers)
    return out


def _airport_lines(itinerary: Itinerary, providers: DataProviders) -> list[CostLine]:
    hoods = {h.id: h for h in providers.places.neighborhoods()}
    hood = hoods.get(itinerary.hotel.neighborhood_id)
    if hood is None:
        return []
    km = road_km(DXB, hood.location, TransportMode.TAXI)
    cabs = math.ceil(itinerary.request.constraints.travelers / TAXI_SEATS)
    out = []
    last = len(itinerary.days)
    for kind, day, label in (
        (TaxiKind.AIRPORT, 1, "Airport taxi (DXB) to hotel"),
        (TaxiKind.STREET_DAY, last, "Taxi from hotel to the airport (DXB)"),
    ):
        tariff = providers.pricing.taxi_tariff(kind)
        if tariff is None:
            continue
        fare = tariff.fare(km) * cabs
        out.append(
            _line(
                BudgetCategory.TRANSPORT,
                f"{label}: {cabs} cab(s), ~{km:.0f} km",
                estimated(round(fare, 2), f"{kind.value} tariff x estimated {km} km"),
                day=day,
                ref_id="airport",
            )
        )
    return out


def contingency_line(lines: list[CostLine], pct: float) -> CostLine | None:
    est = sum(ln.amount_aed.value for ln in lines if ln.amount_aed.source is Source.ESTIMATED)
    if pct <= 0 or est == 0:
        return None
    return _line(
        BudgetCategory.CONTINGENCY,
        f"Contingency: {pct:g}% of AED {est:,.0f} in estimated prices",
        estimated(round(est * pct / 100, 2), f"{pct:g}% buffer on estimated prices"),
    )


def compute_budget(
    itinerary: Itinerary, providers: DataProviders, options: BudgetOptions | None = None
) -> BudgetBreakdown:
    options = options or BudgetOptions()
    lines = [
        *accommodation_lines(itinerary, providers),
        *place_lines(itinerary, providers),
        *transport_lines(itinerary, providers, options),
    ]
    buffer = contingency_line(lines, options.contingency_pct)
    if buffer is not None:
        lines.append(buffer)
    return BudgetBreakdown.from_lines(lines, budget_aed=itinerary.request.constraints.budget_aed)


def with_budget(itinerary: Itinerary, breakdown: BudgetBreakdown) -> Itinerary:
    """Attach the budget and fill in each day's cost (contingency is trip-level)."""
    per_day: dict[int, float] = {}
    nights = max(1, itinerary.request.constraints.night_count)
    for ln in breakdown.lines:
        if ln.day is not None:
            per_day[ln.day] = per_day.get(ln.day, 0.0) + ln.amount_aed.value
    hotel_per_night = breakdown.accommodation / nights
    days = [
        d.model_copy(
            update={
                "day_cost_aed": round(
                    per_day.get(d.day, 0.0) + (hotel_per_night if d.day <= nights else 0.0), 2
                )
            }
        )
        for d in itinerary.days
    ]
    return itinerary.model_copy(update={"days": days, "budget": breakdown})


# ------------------------------------------------------------------ alternatives
@dataclass
class PlanInputs:
    """Everything that decides a plan; alternatives change one field at a time."""

    hotel: HotelArea
    options: SchedulerOptions = field(default_factory=SchedulerOptions)
    candidates: list[Attraction] | None = None


@dataclass
class Plan:
    inputs: PlanInputs
    schedule: ScheduleResult
    budget: BudgetBreakdown

    @property
    def itinerary(self) -> Itinerary:
        return with_budget(self.schedule.itinerary, self.budget)


def plan_trip(
    request: TripRequest,
    inputs: PlanInputs,
    providers: DataProviders,
    budget_options: BudgetOptions | None = None,
) -> Plan:
    """Schedule and cost one set of planning choices."""
    result = schedule(
        request, inputs.hotel, providers, candidates=inputs.candidates, options=inputs.options
    )
    return Plan(inputs, result, compute_budget(result.itinerary, providers, budget_options))


Change = Callable[[PlanInputs], PlanInputs]


def _use_hotel(area: HotelArea) -> Change:
    return lambda i: replace(i, hotel=area)


def _use_transport(policy: TransportPolicy) -> Change:
    return lambda i: replace(i, options=replace(i.options, transport=policy))


def _use_meal_bands(bands: tuple[PriceBand, ...]) -> Change:
    return lambda i: replace(i, options=replace(i.options, meal_bands=bands))


def _swap(old: str, new: str) -> Change:
    return lambda i: replace(i, options=replace(i.options, swaps=(*i.options.swaps, (old, new))))


def _exclude(old: str) -> Change:
    return lambda i: replace(
        i, options=replace(i.options, exclude_ids=i.options.exclude_ids | {old})
    )


@dataclass(frozen=True)
class _Candidate:
    alternative: BudgetAlternative
    change: Change


def _hotel_changes(plan: Plan, providers: DataProviders) -> list[tuple[str, str, HotelArea]]:
    """Cheaper hotel areas: same tier first, then one tier down."""
    c = plan.schedule.itinerary.request.constraints
    month = trip_month(c, plan.inputs.options.month)
    current = plan.inputs.hotel
    tiers = [current.tier]
    idx = TIER_ORDER.index(current.tier)
    if idx > 0:
        tiers.append(TIER_ORDER[idx - 1])
    out = []
    for tier in tiers:
        cheaper = sorted(
            (
                a
                for a in get_hotel_areas(tier, month=month, providers=providers)
                if a.nightly_rate.value.midpoint < current.nightly_rate.value.midpoint
            ),
            key=lambda a: (a.nightly_rate.value.midpoint, a.neighborhood_id),
        )
        label = "a cheaper" if tier is current.tier else f"a {tier.value}"
        out += [
            (f"{a.neighborhood_id}/{tier.value}", label, a)
            for a in cheaper[:HOTEL_OPTIONS_PER_TIER]
        ]
    return out


def _candidates(plan: Plan, providers: DataProviders) -> list[_Candidate]:
    itinerary = plan.schedule.itinerary
    c = itinerary.request.constraints
    opts = plan.inputs.options
    out: list[_Candidate] = []

    for key, label, area in _hotel_changes(plan, providers):
        out.append(
            _Candidate(
                BudgetAlternative(
                    id=f"hotel-{key.replace('/', '-')}",
                    kind=AlternativeKind.CHEAPER_HOTEL,
                    description=f"Stay in {label} hotel area: {area.neighborhood_id} "
                    f"(AED {area.nightly_rate.value.min_aed:,.0f}-"
                    f"{area.nightly_rate.value.max_aed:,.0f}/night)",
                    saving_aed=1,  # placeholder; replaced by the measured saving
                    replaces_id=f"{plan.inputs.hotel.neighborhood_id}/{plan.inputs.hotel.tier.value}",
                    replacement_id=key,
                ),
                _use_hotel(area),
            )
        )

    if opts.transport is not TransportPolicy.PUBLIC_FIRST:
        out.append(
            _Candidate(
                BudgetAlternative(
                    id="public-transport",
                    kind=AlternativeKind.CHEAPER_TRANSPORT,
                    description=(
                        "Use the metro, abra and short walks instead of taxis where reasonable"
                    ),
                    saving_aed=1,
                    replaces_id="taxi",
                    replacement_id=TransportPolicy.PUBLIC_FIRST.value,
                ),
                _use_transport(TransportPolicy.PUBLIC_FIRST),
            )
        )

    bands = opts.meal_bands or MEAL_BANDS[c.hotel_tier]
    if any(b not in CHEAPER_BANDS for b in bands):
        out.append(
            _Candidate(
                BudgetAlternative(
                    id="cheaper-food",
                    kind=AlternativeKind.CHEAPER_FOOD,
                    description=(
                        "Eat at casual restaurants and cafeterias instead of sit-down/fine dining"
                    ),
                    saving_aed=1,
                    replaces_id="meal_bands",
                    replacement_id=",".join(b.value for b in CHEAPER_BANDS),
                ),
                _use_meal_bands(CHEAPER_BANDS),
            )
        )

    in_plan = {s.place_id for d in itinerary.days for s in d.slots if s.place_id}
    for day in itinerary.days:
        for slot in day.slots:
            if slot.kind is not SlotKind.ACTIVITY or slot.place_id is None:
                continue
            place = providers.places.get_place(slot.place_id)
            if not isinstance(place, Attraction) or place.is_free:
                continue
            free = _free_substitute(place, in_plan, providers, avoid_crowds=c.avoids_crowds)
            if free is not None:
                out.append(
                    _Candidate(
                        BudgetAlternative(
                            id=f"free-{free.id}"[:80],
                            kind=AlternativeKind.FREE_ATTRACTION,
                            description=f"Swap {place.name} for {free.name} (free)",
                            saving_aed=1,
                            replaces_id=place.id,
                            replacement_id=free.id,
                            day=day.day,
                        ),
                        _swap(place.id, free.id),
                    )
                )
            else:
                out.append(
                    _Candidate(
                        BudgetAlternative(
                            id=f"drop-{place.id}"[:80],
                            kind=AlternativeKind.DROP_ACTIVITY,
                            description=f"Skip {place.name}",
                            saving_aed=1,
                            replaces_id=place.id,
                            day=day.day,
                        ),
                        _exclude(place.id),
                    )
                )
    return out


def _free_substitute(
    place: Attraction, exclude: set[str], providers: DataProviders, *, avoid_crowds: bool
) -> Attraction | None:
    """The nearest free attraction sharing an interest tag with ``place``."""
    interests = [Interest(t) for t in place.tags if t in {i.value for i in Interest}]
    if not interests:
        return None
    found = providers.places.search_attractions(
        AttractionQuery(
            interests=interests,
            free_only=True,
            near=place.location,
            radius_km=15,
            max_crowd=CrowdLevel.MEDIUM if avoid_crowds else None,
            exclude_ids=sorted(exclude),
            limit=50,
        )
    )
    if not found:
        return None
    return min(found, key=lambda a: (place.location.distance_km(a.location), a.id))


def _ticket_saving(alt: BudgetAlternative, plan: Plan, providers: DataProviders) -> float | None:
    """The direct entry-price saving of a swap or drop (``None`` for other kinds)."""
    if alt.kind not in (AlternativeKind.FREE_ATTRACTION, AlternativeKind.DROP_ACTIVITY):
        return None
    place = providers.places.get_place(alt.replaces_id or "")
    if place is None:
        return None
    travelers = plan.schedule.itinerary.request.constraints.travelers
    return round(place.price.value.midpoint * travelers, 2)


def generate_alternatives(
    plan: Plan,
    request: TripRequest,
    providers: DataProviders,
    budget_options: BudgetOptions | None = None,
) -> list[tuple[BudgetAlternative, PlanInputs]]:
    """Cheaper options with their measured saving, biggest first (only real savings kept)."""
    results = []
    seen: set[str] = set()
    for cand in _candidates(plan, providers):
        if cand.alternative.id in seen:
            continue
        seen.add(cand.alternative.id)
        inputs = cand.change(plan.inputs)
        trial = plan_trip(request, inputs, providers, budget_options)
        saving = round(plan.budget.total - trial.budget.total, 2)
        if saving >= 1:
            update: dict[str, object] = {"saving_aed": saving}
            direct = _ticket_saving(cand.alternative, plan, providers)
            if direct is not None and abs(saving - direct) >= 10:
                # Re-planning moves meals and taxis too; say so instead of crediting the swap.
                update["description"] = (
                    f"{cand.alternative.description}: AED {direct:,.0f} in tickets, "
                    f"AED {saving:,.0f} less overall after re-planning"
                )
            alt = cand.alternative.model_copy(update=update)
            results.append((alt, inputs))
    results.sort(key=lambda r: (-r[0].saving_aed, r[0].id))
    return results


@dataclass
class FitResult:
    plan: Plan
    applied: list[BudgetAlternative]
    offered: list[BudgetAlternative]  # alternatives available for the original plan

    @property
    def itinerary(self) -> Itinerary:
        it = self.plan.itinerary
        applied_ids = {a.id for a in self.applied}
        alts = [a.model_copy(update={"applied": a.id in applied_ids}) for a in self.offered]
        extra = [a for a in self.applied if a.id not in {o.id for o in self.offered}]
        return it.model_copy(update={"alternatives": [*alts, *extra]})


def fit_to_budget(
    plan: Plan,
    request: TripRequest,
    providers: DataProviders,
    budget_options: BudgetOptions | None = None,
    *,
    max_steps: int = 6,
) -> FitResult:
    """Apply the biggest saving, re-plan, repeat, until within budget (or nothing helps)."""
    offered = [a for a, _ in generate_alternatives(plan, request, providers, budget_options)]
    applied: list[BudgetAlternative] = []
    current = plan
    for _ in range(max_steps):
        if current.budget.within_budget:
            break
        options = generate_alternatives(current, request, providers, budget_options)
        if not options:
            break
        alt, inputs = options[0]
        current = plan_trip(request, inputs, providers, budget_options)
        applied.append(alt.model_copy(update={"applied": True}))
    return FitResult(current, applied, offered)
