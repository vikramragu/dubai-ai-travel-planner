"""Orchestrator agent: parse the request, assemble the draft, write the day themes.

``parse`` turns free text into a validated ``TripRequest``. The LLM (or, without one, a
keyword parser) only *extracts* what the traveller said into ``ParsedTrip``; the rules that
decide the final request live in :func:`normalize` (architecture.md §13):

- only Dubai is planned; another destination is a clear error;
- more than 7 days is capped at 7, a missing length defaults to 3 days (both recorded);
- a budget is required; other currencies are converted with ``settings.currency_rates``;
- a month without dates is kept for the scheduler and recorded (no schema change);
- diets the data can't check (anything but vegetarian) are recorded, not silently dropped.

``assemble`` runs the Phase 3 scheduler on the Destination agent's picks and the Logistics
agent's hotel area. ``theme_days`` gives the final plan short LLM-written day themes, once per
run in ``finalize`` (the scheduler's own themes are the fallback).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated

from pydantic import Field, PositiveInt

from agents.base import AgentContext, ClipTo, to_json
from agents.skills.budget_engine import BudgetOptions, Plan, PlanInputs, plan_trip
from agents.skills.calendar import DEFAULT_MONTH
from agents.skills.places_match import match_name, places_in_text
from agents.skills.scheduler import SchedulerOptions, TransportPolicy
from core.config import Settings
from core.llm import NARRATIVE_TEMPERATURE
from core.tracing import TraceKind
from data.providers.base import AttractionQuery
from schemas import (
    Avoidance,
    HotelTier,
    Interest,
    Itinerary,
    LogisticsResult,
    Pace,
    ResearchResult,
    ReviewIssue,
    TripConstraints,
    TripRequest,
)
from schemas.base import StrictModel
from schemas.request import MAX_MUST_VISIT

AGENT = "orchestrator"
MAX_DAYS = 7
DEFAULT_DAYS = 3
MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]  # fmt: skip


class RequestError(ValueError):
    """The request can't be planned (e.g. not Dubai, no budget). The message is user-facing."""


class ParsedTrip(StrictModel):
    """What the traveller said, before any defaults or rules are applied."""

    destination: str = "Dubai"
    days: int | None = None
    budget_amount: float | None = Field(default=None, gt=0)
    budget_currency: str = "AED"
    budget_excludes: list[str] = Field(default_factory=lambda: ["flights"])
    travelers: int | None = Field(default=None, ge=1, le=10)
    children: int | None = Field(default=None, ge=0, le=9)
    rooms: int | None = Field(default=None, ge=1, le=10)
    start_date: date | None = None
    month: int | None = Field(default=None, ge=1, le=12)
    interests: list[Interest] = Field(default_factory=list)
    hotel_tier: HotelTier | None = None
    avoid: list[Avoidance] = Field(default_factory=list)
    pace: Pace | None = None
    dietary: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)  # place names the traveller insists on
    notes: list[str] = Field(default_factory=list)  # anything the parser had to assume


@dataclass
class ParseResult:
    request: TripRequest
    month: int | None = None  # travel month when no dates were given
    parsed: ParsedTrip | None = None
    used_llm: bool = False


# ------------------------------------------------------------------ keyword fallback parser
_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a": 1,
}  # fmt: skip
_CURRENCY = {
    "aed": "AED", "dhs": "AED", "dh": "AED", "dirham": "AED", "dirhams": "AED",
    "usd": "USD", "$": "USD", "us$": "USD", "dollar": "USD", "dollars": "USD",
    "eur": "EUR", "€": "EUR", "euro": "EUR", "euros": "EUR",
    "gbp": "GBP", "£": "GBP", "pound": "GBP", "pounds": "GBP",
    "inr": "INR", "₹": "INR", "rs": "INR", "rupees": "INR",
    "sar": "SAR", "riyal": "SAR", "riyals": "SAR",
}  # fmt: skip
_CUR = (
    r"(aed|dhs?|dirhams?|us\$|usd|\$|dollars?|eur|€|euros?|gbp|£|pounds?"
    r"|inr|₹|rs|rupees|sar|riyals?)"
)
_AMOUNT = r"(\d[\d,]*(?:\.\d+)?)\s*(k)?"
# Currency words after a bare budget number that mean "not AED" (supported ones are in _CURRENCY).
_OTHER_CURRENCIES = (
    r"(?:yen|jpy|yuan|cny|rmb|rupees?|euros?|pounds?|riyals?|francs?|chf|won|krw|baht|"
    r"lira|pesos?|aud|cad|sgd|rubles?|rub)"
)
# Whole-word patterns (so "art" doesn't match "starting", "park" doesn't match "water park").
_INTEREST_WORDS: dict[Interest, str] = {
    Interest.CULTURE: r"\b(cultur\w*|heritage|museums?|histor\w*|traditional|mosques?|souks?)\b",
    Interest.FOOD: r"\b(food\w*|cuisines?|eat\w*|restaurants?|dishes)\b",
    Interest.BEACH: r"\b(beach\w*|seaside|swim\w*)\b",
    Interest.NATURE: r"\b(nature|(?<!water )parks?|desert|gardens?|wildlife|mountains?|hik\w*)\b",
    Interest.ADVENTURE: (
        r"\b(adventur\w*|thrill\w*|kayak\w*|ski\w*|water ?parks?|skydiv\w*|dunes?)\b"
    ),
    Interest.SHOPPING: r"\b(shop\w*|malls?|markets?)\b",
    Interest.ART: r"\b(arts?|galler\w*|design)\b",
    Interest.NIGHTLIFE: r"\b(nightlife|night life|bars|clubs|party\w*)\b",
    Interest.FAMILY: r"\b(family|kids|children)\b",
    Interest.RELAXATION: r"\b(relaxing|relaxation|chill\w*|spa|unwind\w*)\b",  # not "relaxed pace"
}
_NEG = r"(?:avoid|avoiding|no|not|don't|dont|hate|without|skip|away from)\W+(?:\w+\W+){0,4}?"
_AVOID_PATTERNS: dict[Avoidance, str] = {
    Avoidance.CROWDS: _NEG + r"(?:crowd|busy|touristy|tourist traps?)",
    Avoidance.LONG_WALKS: _NEG + r"(?:long walks?|walking|much walking)",
    Avoidance.EARLY_MORNINGS: _NEG + r"(?:early mornings?|early starts?|waking up early)",
    Avoidance.LATE_NIGHTS: _NEG + r"(?:late nights?|staying out late)",
}
_OTHER_CITIES = (
    "abu dhabi", "sharjah", "ras al khaimah", "doha", "riyadh", "muscat", "paris", "london",
    "istanbul", "bangkok", "singapore", "tokyo", "new york", "rome", "cairo", "bali",
)  # fmt: skip


def _num(token: str) -> int | None:
    token = token.lower()
    return int(token) if token.isdigit() else _WORD_NUM.get(token)


def keyword_parse(text: str, today: date | None = None) -> ParsedTrip:
    """A deterministic parser for the common phrasings (fallback when there is no LLM)."""
    t = " " + text.lower().replace("\u2013", "-") + " "  # en dash -> hyphen
    today = today or date.today()
    out: dict[str, object] = {}

    # destination
    if "dubai" not in t:
        other = next((c for c in _OTHER_CITIES if c in t), None)
        if other:
            out["destination"] = other.title()

    # length
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ]days?\b", t)
    if m:
        out["days"] = _num(m.group(1))
    elif re.search(r"\b(a|one) week\b", t):
        out["days"] = 7
    elif m := re.search(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ]nights?\b", t
    ):
        n = _num(m.group(1))
        out["days"] = None if n is None else n + 1

    # budget
    m = re.search(_CUR + r"\s*" + _AMOUNT, t) or None
    amount = cur = None
    if m:
        cur, amount, k = m.group(1), m.group(2), m.group(3)
    else:
        m = re.search(_AMOUNT + r"\s*" + _CUR + r"\b", t)
        if m:
            amount, k, cur = m.group(1), m.group(2), m.group(3)
    notes: list[str] = []
    if not (amount and cur):
        # "budget 5000", "budget of 5k", "budget: 5,000" -> AED. A number followed by another
        # currency (e.g. "5000 yen") is not taken: that currency isn't supported.
        m = re.search(
            r"\bbudget\b(?:\s*(?:of|is|around|about|approx\.?|roughly|:))*\s*"
            + _AMOUNT
            + r"(?!\d|[.,]\d|\s*(?:[$€£¥₹]|"
            + _OTHER_CURRENCIES
            + r"\b))",
            t,
        )
        if m:
            amount, k, cur = m.group(1), m.group(2), "aed"
            notes.append("No currency given for the budget: assumed AED")
    if amount and cur:
        value = float(amount.replace(",", "")) * (1000 if k else 1)
        out["budget_amount"] = value
        out["budget_currency"] = _CURRENCY.get(cur.lower(), "AED")
    if re.search(r"(excluding|without|not including|except)\W+(?:\w+\W+){0,2}flights?", t):
        out["budget_excludes"] = ["flights"]

    # party
    party = r"(?:people|persons|travell?ers|adults|friends|guests|of us)"
    couple = (
        r"\b(couple|my (wife|husband|partner|girlfriend|boyfriend)|honeymoon"
        r"|for two|the two of us)\b"
    )
    if (m := re.search(rf"\b(\d+|two|three|four|five|six)\s+{party}\b", t)) or (
        m := re.search(r"\bfamily of (\d+|three|four|five|six)\b", t)
    ):
        out["travelers"] = _num(m.group(1))
    elif re.search(couple, t):
        out["travelers"] = 2
    elif re.search(r"\b(solo|alone|by myself|just me)\b", t):
        out["travelers"] = 1
    kids = (
        r"(?:little |young |small )?"
        r"(?:kids?|children|child|toddlers?|bab(?:y|ies)|sons?|daughters?)"
    )
    if m := re.search(rf"\b(\d+|one|two|three|four|five|a)\s+{kids}\b", t):
        out["children"] = 1 if m.group(1) == "a" else _num(m.group(1))
    elif re.search(rf"\bwith (?:my|our|a|the) {kids}\b", t):
        out["children"] = 1
    if (m := re.search(r"\b(\d+|one|two|three|four|five|six)\s+adults?\b", t)) and out.get(
        "children"
    ):
        adults = _num(m.group(1))
        if adults is not None and isinstance(out["children"], int):
            out["travelers"] = adults + out["children"]
    if m := re.search(r"\b(\d+|one|two|three|four|a single|single)\s+(?:hotel )?rooms?\b", t):
        out["rooms"] = 1 if "single" in m.group(1) else _num(m.group(1))

    # dates / month
    if m := re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t):
        out["start_date"] = date.fromisoformat(m.group(1))
    else:
        months = "|".join(MONTHS)
        m = re.search(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({months})\b(?:\s+(\d{{4}}))?", t
        ) or re.search(rf"\b({months})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", t)
        if m:
            a, b, year = m.group(1), m.group(2), m.group(3)
            day, month_name = (int(a), b) if a.isdigit() else (int(b), a)
            month = MONTHS.index(month_name) + 1
            y = int(year) if year else today.year
            d = date(y, month, day)
            if not year and d < today:
                d = date(y + 1, month, day)
            out["start_date"] = d
        elif m := re.search(rf"\b(?:in|during|this|next)\s+({months})\b", t):
            out["month"] = MONTHS.index(m.group(1)) + 1

    # preferences
    out["interests"] = [i for i, rx in _INTEREST_WORDS.items() if re.search(rx, t)]
    if re.search(r"\b(luxury|luxurious|5-star|five-star|upscale|high-end)\b", t):
        out["hotel_tier"] = HotelTier.LUXURY
    elif re.search(
        r"\b(affordable|cheap|budget hotels?|low-cost|inexpensive|budget-friendly)\b", t
    ):
        out["hotel_tier"] = HotelTier.BUDGET
    elif re.search(r"\b(mid-range|midrange|4-star|four-star|moderate)\b", t):
        out["hotel_tier"] = HotelTier.MID
    out["avoid"] = [a for a, rx in _AVOID_PATTERNS.items() if re.search(rx, t)]
    if re.search(r"\b(relaxed|slow|easy-going|laid-back|leisurely)\b", t):
        out["pace"] = Pace.RELAXED
    elif re.search(r"\b(packed|see as much|as much as possible|intense|busy schedule)\b", t):
        out["pace"] = Pace.PACKED
    diets = []
    if re.search(r"\b(vegetarian|veggie)\b", t):
        diets.append("vegetarian")
    for word in ("vegan", "halal", "gluten-free", "kosher", "nut allergy"):
        if word in t:
            diets.append(word)
    out["dietary"] = diets
    out["notes"] = notes
    return ParsedTrip.model_validate(out)


# ------------------------------------------------------------------ the rules
def normalize(parsed: ParsedTrip, text: str, settings: Settings) -> tuple[TripRequest, int | None]:
    notes = list(parsed.notes)
    if "dubai" not in parsed.destination.lower():
        raise RequestError(
            f"This planner only covers Dubai, but the request is for {parsed.destination}."
        )
    days = parsed.days
    if days is None:
        days = DEFAULT_DAYS
        notes.append(f"No trip length given: planned {DEFAULT_DAYS} days")
    elif days < 1:
        raise RequestError("The trip must be at least 1 day long.")
    elif days > MAX_DAYS:
        notes.append(f"Asked for {days} days: planned the first {MAX_DAYS} (the maximum)")
        days = MAX_DAYS
    if parsed.budget_amount is None:
        raise RequestError("Please include a total budget, e.g. 'a budget of AED 5,000'.")
    currency = parsed.budget_currency.upper()
    budget = parsed.budget_amount
    if currency != "AED":
        rate = settings.currency_rates.get(currency)
        if rate is None:
            raise RequestError(
                f"Budgets in {currency} aren't supported; please give the budget in AED "
                f"or one of: {', '.join(sorted(settings.currency_rates))}."
            )
        budget = round(parsed.budget_amount * rate, 2)
        notes.append(
            f"Converted {currency} {parsed.budget_amount:,.0f} to AED {budget:,.0f} at {rate:g}"
        )
    travelers = parsed.travelers
    children = parsed.children or 0
    if travelers is None and children:
        adults = 2 if re.search(r"\b(family|we|our|us|couple|wife|husband)\b", text.lower()) else 1
        travelers = adults + children
        notes.append(f"Number of adults not given: assumed {adults}")
    elif travelers is None:
        travelers = 1
        notes.append("Number of travellers not given: assumed 1")
    if children >= travelers:  # "3 people with 3 kids": count the children as extra people
        travelers = children + 1
        notes.append(f"Counted {children} children plus 1 adult")
    travelers = min(travelers, 10)
    children = min(children, travelers - 1)
    if children:
        notes.append(CHILD_NOTE.format(n=children))
    rooms = parsed.rooms or max(1, math.ceil(travelers / 2))
    rooms = min(rooms, travelers)
    interests = list(dict.fromkeys(parsed.interests))
    if not interests:
        interests = [Interest.CULTURE, Interest.FOOD]
        notes.append("No interests given: assumed culture and food")
    tier = parsed.hotel_tier
    if tier is None:
        tier = HotelTier.MID
        notes.append("No hotel preference given: assumed mid-range")
    dietary, unsupported = [], []
    for d in parsed.dietary:
        key = d.strip().lower()
        if key in ("veg", "veggie", "vegetarian"):
            dietary.append("vegetarian")
        elif key:
            unsupported.append(key)
    for d in unsupported:
        notes.append(
            f"Dietary need '{d}' can't be checked against the data; please confirm with restaurants"
        )
    month = None
    if parsed.start_date is None:
        if parsed.month is not None:
            month = parsed.month
            notes.append(
                f"Travel month {MONTHS[month - 1].title()} (no exact dates): planned for that month"
            )
        else:
            notes.append(
                f"No travel dates: planned for {MONTHS[DEFAULT_MONTH - 1].title()} "
                "(opening hours, weather and hotel prices depend on the month)"
            )
    constraints = TripConstraints(
        days=days,
        budget_aed=budget,
        budget_excludes=parsed.budget_excludes or ["flights"],
        travelers=travelers,
        children=children,
        rooms=rooms,
        start_date=parsed.start_date,
        interests=interests,
        hotel_tier=tier,
        avoid=parsed.avoid,
        pace=parsed.pace or Pace.BALANCED,
        dietary=list(dict.fromkeys(dietary)),
    )
    return TripRequest(raw_text=text, constraints=constraints, assumptions=notes), month


CHILD_NOTE = (
    "{n} child(ren): charged adult prices (the dataset has no child prices); "
    "family-friendly places are preferred"
)


def form_request(constraints: TripConstraints) -> TripRequest:
    """A request built from the trip form: exact values, so nothing is parsed or assumed
    except what the data can't do (child prices)."""
    c = constraints
    when = f" from {c.start_date:%d %b %Y}" if c.start_date else ""
    party = f"{c.adults} adult(s)" + (f" and {c.children} child(ren)" if c.children else "")
    summary = (
        f"Trip form: {c.days} days in Dubai{when}, {party}, budget AED {c.budget_aed:,.0f}, "
        f"{c.hotel_tier.value} hotel, interests {', '.join(i.value for i in c.interests)}"
    )
    notes = [CHILD_NOTE.format(n=c.children)] if c.children else []
    return TripRequest(raw_text=summary, constraints=c, assumptions=notes)


# ------------------------------------------------------------------ LLM outputs
class DayTheme(StrictModel):
    day: PositiveInt
    theme: Annotated[str, ClipTo(80)] = Field(min_length=3)


class DayThemes(StrictModel):
    days: list[DayTheme]


@dataclass
class Orchestrator:
    ctx: AgentContext
    budget_options: BudgetOptions = field(default_factory=BudgetOptions)

    # -------------------------------------------------------------- parse
    def parse(self, text: str) -> ParseResult:
        self.ctx.emit(AGENT, TraceKind.START, "Parsing the request")
        parsed = self.ctx.ask(
            "orchestrator",
            ParsedTrip,
            prompt="orchestrator_parse",
            user=text,
            today=date.today().isoformat(),
        )
        used_llm = parsed is not None
        if parsed is None:
            parsed = keyword_parse(text)
        request, month = normalize(parsed, text, self.ctx.settings)
        request = self._must_visit(request, parsed)
        c = request.constraints
        self.ctx.emit(
            AGENT,
            TraceKind.OUTPUT,
            f"{c.days} days, AED {c.budget_aed:,.0f}, {', '.join(i.value for i in c.interests)}, "
            f"{c.hotel_tier.value} hotel"
            + (f", avoid {', '.join(a.value for a in c.avoid)}" if c.avoid else ""),
            {
                "constraints": c.model_dump(mode="json"),
                "assumptions": request.assumptions,
                "parser": "llm" if used_llm else "keywords",
            },
        )
        return ParseResult(request, month, parsed, used_llm)

    def _must_visit(self, request: TripRequest, parsed: ParsedTrip) -> TripRequest:
        """Places the traveller named: known names in the text, plus names the LLM extracted
        (matched to the dataset; unknown ones are recorded as assumptions)."""
        places = self.ctx.providers.places.search_attractions(AttractionQuery(limit=1000))
        ids = places_in_text(request.raw_text, places)
        notes = list(request.assumptions)
        for name in parsed.must_visit:
            pid = match_name(name, places)
            if pid is None:
                notes.append(f"'{name}' isn't in the dataset, so it can't be scheduled")
            elif pid not in ids:
                ids.append(pid)
        ids = ids[:MAX_MUST_VISIT]
        if not ids and notes == request.assumptions:
            return request
        c = request.constraints.model_copy(update={"must_visit": ids})
        return request.model_copy(update={"constraints": c, "assumptions": notes})

    # -------------------------------------------------------------- assemble
    def assemble(
        self,
        request: TripRequest,
        research: ResearchResult,
        logistics: LogisticsResult,
        *,
        month: int | None = None,
        exclude_ids: frozenset[str] = frozenset(),
        issues: list[ReviewIssue] | None = None,
    ) -> Plan:
        self.ctx.emit(AGENT, TraceKind.START, "Assembling the day-by-day plan")
        codes = {i.code for i in issues or []}
        # Too much time on the road: re-assemble with the quickest legs instead of repeating
        # the same plan (a revision that changes nothing can't fix anything).
        transport = (
            TransportPolicy.FASTEST
            if codes & {"day_transit_heavy", "day_overloaded"}
            else TransportPolicy.BALANCED
        )
        options = SchedulerOptions(
            transport=transport,
            month=month,
            run_id=self.ctx.run_id,
            exclude_ids=exclude_ids,
            preferred_restaurants=frozenset(r.id for r in research.restaurants),
        )
        inputs = PlanInputs(
            hotel=logistics.recommended_hotel, options=options, candidates=research.attractions
        )
        plan = plan_trip(request, inputs, self.ctx.providers, self.budget_options)
        # Day themes are written once, for the final plan (``theme_days`` in ``finalize``): a
        # draft may still be revised, and titles never change what the review checks.
        it = plan.schedule.itinerary
        self.ctx.emit(
            AGENT,
            TraceKind.OUTPUT,
            f"Draft: {len(it.days)} days, "
            f"{sum(1 for d in it.days for s in d.slots if s.kind.value == 'activity')} activities; "
            + "; ".join(f"day {d.day}: {d.theme}" for d in it.days),
            {"unscheduled": plan.schedule.unscheduled},
        )
        return plan

    def theme_days(self, it: Itinerary) -> Itinerary:
        """LLM-written day themes; the scheduler's own themes are kept if the LLM is unavailable."""
        days = [
            {
                "day": d.day,
                "area": d.base_area,
                "activities": [s.title for s in d.slots if s.kind.value == "activity"],
                "current_theme": d.theme,
            }
            for d in it.days
        ]
        answer = self.ctx.ask(
            "orchestrator",
            DayThemes,
            prompt="orchestrator_assemble",
            user=to_json(
                {"interests": [i.value for i in it.request.constraints.interests], "days": days}
            ),
            temperature=NARRATIVE_TEMPERATURE,
        )
        if answer is None:
            return it
        themes = {t.day: t.theme for t in answer.days}
        new_days = [d.model_copy(update={"theme": themes.get(d.day, d.theme)}) for d in it.days]
        return it.model_copy(update={"days": new_days})
