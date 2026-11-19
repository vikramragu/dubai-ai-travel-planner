# Architecture — Dubai AI Travel Planner (Multi-Agent)

> Companion to [problemStatement.md](problemStatement.md) and [research.md](research.md).
> This document describes how the system is built: the layers, the shared contracts, the
> LangGraph orchestration, the data layer, and how agent execution is made visible in the UI.

---

## 1. Design Goals

| Goal | What it means for the architecture |
|------|------------------------------------|
| **Graph-orchestrated, logic-decoupled** | LangGraph owns orchestration (state, parallelism, routing, loops). Agent logic, tools and schemas are plain Python modules that graph nodes call, so they can be unit-tested without the graph. |
| **Deterministic where it matters** | Money, travel-time arithmetic and constraint checks are plain Python, not LLM output. LLMs do language understanding, recommendation and writing. |
| **Demo-reliable** | Every external API has a local fallback (curated JSON/SQLite). The app runs end to end with no API keys except the LLM. |
| **Provenance everywhere** | Every fact carries a source tag: `verified`, `api`, or `estimated` (problem statement §5). |
| **Observable** | Every agent step emits a trace event so the UI can show who is running, what they produced, and what changed during review. |
| **Provider-agnostic LLM** | The LLM is chosen by configuration (Anthropic, OpenAI, Gemini, local Ollama, etc.). |

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         Streamlit UI (app/)                              │
│  Request box · Options · Live agent timeline · Graph view                │
│  Itinerary view · Budget chart · Review diff · Run metrics               │
└───────────────┬───────────────────────────────────────▲──────────────────┘
                │ TripRequest (text)                    │ TraceEvents + Itinerary
                ▼                                       │
┌──────────────────────────────────────────────────────────────────────────┐
│                     Runner (core/runner.py)                              │
│           run(request) -> RunResult{itinerary, trace, metrics}           │
└───────────────┬──────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     LangGraph Orchestration (graph/)                     │
│  StateGraph · PlannerState · parallel fan-out · conditional router ·     │
│  review→revise cycle · checkpointer · streaming                          │
└───────────────┬──────────────────────────────────────────────────────────┘
                │  nodes call agent logic
                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     Agent Core (agents/)                                 │
│  Orchestrator · DestinationResearch · Logistics · Budget · Review        │
│  prompts/ · pure-Python skills (budget engine, rule checks, scheduler)   │
└───────────────┬──────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     Tools Layer (tools/)                                 │
│  search_attractions · search_restaurants · get_hotel_areas ·             │
│  travel_time · transit_fare · weather_forecast · price_lookup            │
└───────────────┬──────────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     Data Layer (data/)                                   │
│  Provider interfaces → [Live API provider] ──fail──▶ [Local provider]    │
│  OSM Overpass · OSRM routing · Open-Meteo · (Amadeus)                    │
│  Curated SQLite/JSON: attractions, restaurants, hotels, prices, fares,   │
│  neighborhood travel-time matrix · HTTP cache                            │
└──────────────────────────────────────────────────────────────────────────┘
                ▲
                │  Shared contracts (schemas/) — Pydantic models used by every layer
```

The key idea: **LangGraph decides how agents are wired together** (order, parallelism,
routing, loops), while each agent's reasoning and every tool live in plain modules. Nodes stay
thin, and the business logic can be tested without running the graph.

---

## 3. Repository Layout

```
muti-agent/
├── app/
│   ├── streamlit_app.py          # entry point: navigation, theme and dev-mode toggles
│   ├── pages/                    # plan_trip (form), ask_agents (text), browse, saved, travel_data (dev)
│   ├── components/               # day cards, progress, map, budget, review diff, timeline, graph, skyline
│   └── state.py                  # session-state helpers (current run, saved trips, must-visit list)
├── core/
│   ├── config.py                 # pydantic-settings: LLM provider, API keys, flags
│   ├── llm.py                    # provider-agnostic LLM factory
│   ├── runner.py                 # run(request) -> RunResult
│   └── tracing.py                # TraceEvent, TraceBus, LangGraph stream adapter
├── schemas/
│   ├── base.py                   # StrictModel (rejects unknown fields)
│   ├── common.py                 # Slug, AED, LatLon, PriceRange, OpeningHours, enums
│   ├── provenance.py             # Source enum + SourcedValue[T]
│   ├── request.py                # TripRequest, TripConstraints
│   ├── places.py                 # Place, Attraction, Restaurant, Neighborhood, HotelArea
│   ├── logistics.py              # Leg, TransportOption, TravelMatrix, LogisticsResult
│   ├── budget.py                 # CostLine, BudgetBreakdown, BudgetAlternative
│   ├── review.py                 # ReviewIssue, ReviewReport
│   ├── itinerary.py              # ResearchResult, Slot, DayPlan, Itinerary, ItinerarySnapshot
│   └── export.py                 # JSON-schema export for prompts
├── agents/
│   ├── orchestrator.py
│   ├── destination.py
│   ├── logistics.py
│   ├── budget.py
│   ├── review.py
│   ├── skills/
│   │   ├── calendar.py           # weekday/month of each trip day (assumed if no start date)
│   │   ├── budget_engine.py      # deterministic cost math + measured alternatives
│   │   ├── rule_checks.py        # deterministic review rules
│   │   ├── scheduler.py          # geo-clustering + day sequencing
│   │   └── scripted.py           # no-LLM planner: schedule → budget → fit → rules
│   └── prompts/                  # one .md prompt per agent
├── tools/                        # thin, typed wrappers over data providers
│   ├── places.py                 # search_attractions, search_restaurants, price_lookup, ...
│   └── logistics.py              # get_hotel_areas, hotel_fees, travel_time, transit_fare, weather_forecast
├── data/
│   ├── curation/                 # hand-written place lists + OSM match rules
│   ├── raw/osm/                  # OpenStreetMap export (one file per neighborhood)
│   ├── seed/                     # validated seed JSON (version-controlled)
│   ├── scripts/                  # osm_export, curate, build_matrix, climate_normals, staleness_report
│   ├── providers/
│   │   ├── base.py               # Places/Routes/Weather/Hotel/Pricing protocols + query models
│   │   ├── local.py              # curated SQLite provider (always available)
│   │   ├── chained.py            # fallback chains that emit `fallback` trace events
│   │   ├── overpass.py           # (Phase 7) OSM places/restaurants
│   │   ├── osrm.py               # (Phase 7) road/walk routing + matrix
│   │   ├── gtfs.py               # (Phase 7) metro/tram times, if RTA GTFS is available
│   │   ├── open_meteo.py         # (Phase 7) live forecast
│   │   └── amadeus.py            # (Phase 7, optional)
│   ├── heuristics.py             # haversine travel-time estimates (matrix + last-resort routing)
│   ├── dataset.py                # load + cross-validate seed, coverage report
│   ├── build_db.py               # seed JSON → SQLite
│   └── dubai.sqlite              # generated (git-ignored)
├── graph/
│   ├── state.py                  # PlannerState (TypedDict + reducers)
│   ├── nodes.py                  # node wrappers around agents/*
│   ├── routing.py                # orchestrate fan-out + route_after_review
│   └── build.py                  # StateGraph build + compile (+ checkpointer)
├── tests/
│   ├── unit/                     # budget engine, rule checks, scheduler, providers
│   ├── graph/                    # routing, parallel branches, loop termination
│   └── scenarios/                # golden requests incl. forced budget violation
├── docs/
├── .env.example
└── pyproject.toml
```

---

## 4. Shared Contracts (Pydantic Schemas)

The graph's final output is an `Itinerary`. All intermediate agent outputs are typed too,
so they can be validated, diffed and rendered. The code in `schemas/` is the source of truth;
the listings below are abridged (validators are summarised in comments).

All models extend `StrictModel` (`extra="forbid"`, whitespace stripped, re-validated on
assignment), so LLM output that invents a field fails loudly instead of being silently dropped.
`uv run python -m schemas.export` writes every model's JSON schema for use in prompts.

### 4.1 Provenance

```python
class Source(StrEnum):
    VERIFIED = "verified"    # copied from an official source and dated (Visit Dubai, RTA, venue)
    API = "api"              # returned by a live API call in this run
    ESTIMATED = "estimated"  # derived/assumed (price range midpoint, heuristic travel time)

class SourcedValue(StrictModel, Generic[T]):
    value: T
    source: Source
    reference: str | None = None   # URL, dataset id, or API name
    as_of: date | None = None
    # verified → needs reference + as_of; api → needs reference (the API name)

# helpers: verified(value, reference, as_of), from_api(value, api_name), estimated(value, basis)
```

### 4.2 Shared types (`schemas/common.py`)

```python
Slug = str  # pattern ^[a-z0-9][a-z0-9_-]*$, ≤ 80 chars, e.g. "al-fahidi-historical-district"
AED = NonNegativeFloat

class LatLon(StrictModel):
    lat: float; lon: float     # must fall inside Dubai (lat 24.6–25.5, lon 54.8–56.4)
    def distance_km(self, other: LatLon) -> float: ...   # haversine

class PriceRange(StrictModel):
    min_aed: AED = 0; max_aed: AED = 0   # min ≤ max; 0–0 means free
    # fixed(amount), midpoint, is_free

class TimeWindow(StrictModel):
    opens: time; closes: time  # closes ≤ opens means it runs past midnight (18:00–02:00)

class OpeningHours(StrictModel):
    always_open: bool = False                          # beaches, districts, walks
    weekly: dict[Weekday, list[TimeWindow]] = {}       # several windows = a midday break
    def is_open(self, day: Weekday, start: time, end: time) -> bool: ...
    # also checks the previous day's overnight windows (Fri 18:00–02:00 → open Sat 00:30)

# Enums: Interest, Avoidance, HotelTier, Pace (activity_hours 6/8/10), CrowdLevel, Weekday
```

Coordinates are one `location: LatLon` field rather than flat `lat`/`lon`, so swapped or
invented coordinates are rejected. Opening hours are structured per weekday so the
"slot outside opening hours" rule (§5.4) is a simple check rather than text parsing.

### 4.3 Request and constraints

```python
class Avoidance(StrEnum):
    CROWDS = "crowds"; LONG_WALKS = "long_walks"
    EARLY_MORNINGS = "early_mornings"; LATE_NIGHTS = "late_nights"

class TripConstraints(StrictModel):
    days: int                                 # 1–7
    budget_aed: PositiveFloat
    budget_excludes: list[str] = ["flights"]
    travelers: int = 1                        # 1–10, everyone including children
    children: int = 0                         # < travelers (at least one adult); adults = travelers − children
    rooms: int = 1                            # ≤ travelers
    nights: int | None = None                 # ≤ days; None → days − 1 (night_count)
    start_date: date | None = None
    interests: list[Interest]                 # at least one
    hotel_tier: HotelTier = HotelTier.MID
    avoid: list[Avoidance] = []               # fixed choices, not free text
    pace: Pace = Pace.BALANCED
    dietary: list[str] = []
    must_visit: list[Slug] = []               # place ids the plan must include (≤ 10)
    # lists are de-duplicated; properties: night_count, end_date, avoids_crowds, adults

class TripRequest(StrictModel):
    raw_text: str
    constraints: TripConstraints
    assumptions: list[str] = []               # what the parser had to assume
```

`avoid` is an enum so the parser must map phrases like "I hate busy places" to `crowds`,
and every rule check can rely on one spelling.

A request comes from one of two places: the **text** box (the Orchestrator parses it) or the
**trip form** (the UI builds `TripConstraints` directly; `raw_text` is a generated one-line
summary and parsing is skipped). Children count as travellers for every cost (taxi seats,
tickets, meals) and charge the adult price, because the dataset has no child prices; the
itinerary says so in `assumptions`. With children, family-tagged places score higher.

### 4.4 Domain objects (abridged)

```python
class Neighborhood(StrictModel):
    id: Slug; name: str; location: LatLon
    vibe: str = ""; metro_station: str | None = None; beach_access: bool = False

class Place(StrictModel):
    id: Slug; name: str; category: str
    neighborhood_id: Slug
    location: LatLon
    tags: list[str]                            # lower-cased, de-duplicated
    crowd_level: CrowdLevel
    opening_hours: OpeningHours | None         # None = unknown
    typical_duration_min: int                  # 10–720
    price: SourcedValue[PriceRange]            # per person
    source: Source                             # where the record as a whole came from
    reference: str | None = None

class Attraction(Place):
    indoor: bool = False                       # heat-aware scheduling; is_free property
class Restaurant(Place):
    cuisine: list[str]; meal_types: list[MealType]; price_band: PriceBand
class HotelArea(StrictModel):
    neighborhood_id: Slug; tier: HotelTier
    nightly_rate: SourcedValue[PriceRange]     # per room per night, before fees
    metro_access: bool; beach_access: bool; rationale: str = ""

class Leg(StrictModel):
    from_id: Slug; to_id: Slug                 # must differ
    mode: TransportMode                        # metro, tram, bus, taxi, ride_hail, walk, abra
    duration_min: SourcedValue[NonNegativeInt]
    distance_km: SourcedValue[NonNegativeFloat]
    cost_aed: SourcedValue[NonNegativeFloat]   # per person

class TravelMatrix(StrictModel):
    entries: list[TravelMatrixEntry]           # unique (from_id, to_id, mode)
    def lookup(self, from_id, to_id, mode, *, symmetric=True) -> TravelMatrixEntry | None: ...

class ResearchResult(StrictModel):             # Destination agent output
    attractions: list[Attraction]; restaurants: list[Restaurant]
    neighborhood_ids: list[Slug]
    rationale: dict[str, str]                  # place id → why; ids must be known
class LogisticsResult(StrictModel):            # Logistics agent output (parallel phase)
    hotel_areas: list[HotelArea]               # ≥ 1, ranked; recommended_hotel = first
    transport_options: list[TransportOption]
    travel_matrix: TravelMatrix
```

### 4.5 Budget and review

```python
class CostLine(StrictModel):
    category: BudgetCategory                   # accommodation | food | transport | activities | contingency
    description: str
    amount_aed: SourcedValue[NonNegativeFloat] # for the whole party
    day: int | None = None; ref_id: str | None = None

class BudgetBreakdown(StrictModel):
    budget_aed: PositiveFloat
    lines: list[CostLine]                      # every line has a Source
    accommodation: AED; food: AED; transport: AED; activities: AED; contingency: AED
    total: AED
    within_budget: bool
    # build with BudgetBreakdown.from_lines(lines, budget_aed).
    # Validator: each category = Σ its lines and total = Σ categories (±0.01 for float
    # rounding); within_budget == (total <= budget_aed) exactly — 1 fils over is over.
    # remaining_aed, total_by_source(source)

class ReviewIssue(StrictModel):
    code: str                                  # snake_case, e.g. "over_budget"
    severity: Severity                         # blocker | major | minor
    message: str
    route_to: RouteTarget                      # destination | logistics | assemble | budget
    origin: IssueOrigin = IssueOrigin.RULE     # rule | llm | user (a change the traveller asked for)
    day: int | None = None; place_id: str | None = None

class ReviewReport(StrictModel):
    issues: list[ReviewIssue]; score: float    # 0–100
    passed: bool                               # must equal "no blocker or major issues"
    summary: str = ""
    # from_issues(issues, score), revision_targets, by_severity(severity)
```

### 4.6 Final output

```python
class Slot(StrictModel):
    start: time; end: time                     # end > start; slots do not cross midnight
    kind: SlotKind                             # activity | meal | transit | rest
    title: str
    place_id: Slug | None = None               # required for activity and meal
    notes: str = ""
    leg: Leg | None = None                     # required for transit; how you get here

class DayPlan(StrictModel):
    day: int; theme: str; date: date | None = None
    base_area: Slug                            # neighborhood id
    slots: list[Slot]                          # ≥ 1, in order, no overlaps
    day_cost_aed: AED = 0

class Itinerary(StrictModel):
    run_id: str
    request: TripRequest
    hotel: HotelArea
    days: list[DayPlan]                        # count = request.days, numbered 1..n,
                                               # dates follow start_date
    budget: BudgetBreakdown | None = None      # set by the Budget node
    alternatives: list[BudgetAlternative] = [] # cheaper swaps when over budget
    review: ReviewReport | None = None         # set by the Review node
    revisions: int = 0
    # budget.budget_aed must equal the request budget
    # is_final = budget and review are both present; place_ids

class ItinerarySnapshot(StrictModel):          # one entry in PlannerState.history
    revision: int; itinerary: Itinerary; created_at: datetime; note: str = ""
```

`budget` and `review` are optional because `assemble` produces the draft before the Budget
and Review nodes run; drafts are still fully validated (days, dates, slot order).
`is_final` tells the UI whether it is showing a draft or a finished plan.

---

## 5. Agent Design

Each agent lives in `agents/` as a plain class with a single typed entry point. LangGraph
nodes in `graph/nodes.py` are thin wrappers: read from state → call the agent → return a state update.

| Agent | Input | Output | LLM role | Deterministic skills / tools |
|-------|-------|--------|----------|------------------------------|
| **Orchestrator** | raw text, (later) all agent outputs | `TripRequest`; final `Itinerary` | Parse NL into `TripConstraints` (structured output); write the day themes once, for the final plan (in `finalize`) | Constraint defaults & validation (1–7 days, AED), `scheduler.py` for final assembly |
| **Destination Research** | `TripRequest`, optional `ReviewIssue`s | `ResearchResult{attractions, restaurants, neighborhoods}` | Rank candidates against interests & "avoid" list; justify picks. `must_visit` places are always kept, first, whatever the filters | `search_attractions`, `search_restaurants`, `weather_forecast` |
| **Logistics** | `TripRequest`, optional issues | `LogisticsResult{hotel_areas, transport_options, travel_matrix}` | Pick hotel area trade-offs; explain transport choices | `get_hotel_areas`, `travel_time`, `transit_fare` |
| **Budget** | Draft itinerary | `BudgetBreakdown`, `alternatives` | Phrase alternatives; choose among cheaper swaps | `budget_engine.py`, `price_lookup`, `transit_fare` |
| **Review** | Draft itinerary + budget | `ReviewReport{passed, issues[], score}` | Judge preference alignment and "feel" of the plan | `rule_checks.py` (runs first, always) |

### 5.1 Resolving the Destination ∥ Logistics dependency

The workflow runs Destination and Logistics **in parallel**, but sequencing activities needs
both the chosen attractions and travel times. We split logistics work in two:

1. **Logistics (parallel phase)** — independent of the chosen attractions: hotel areas
   for the tier, transport modes, neighborhood-to-neighborhood travel-time matrix
   (pre-computed or fetched once).
2. **Assembly (join phase)** — performed by the Orchestrator using `scheduler.py`:
   cluster attractions by neighborhood, assign clusters to days, order stops to minimise
   travel time from the matrix, insert meals near each cluster, and attach a `Leg` to every move.

This keeps the parallel fan-out honest (no hidden dependency) and puts geometry in
deterministic code.

### 5.2 Scheduler algorithm (`agents/skills/scheduler.py`)

1. Score candidates: `interest_match × (1 − crowd_penalty) × quality`, where
   `interest_match = 0.5 + 0.5 × matched/asked`, the crowd penalty is stronger when the traveller
   avoids crowds, and `quality` is 0 for seasonal places out of season (Oct–May) and lower for
   half-day+ places or walking tours when long walks are avoided.
2. **Must-visit places first:** each `must_visit` place is placed before anything else (its
   neighborhood anchors a day) and is never the stop dropped to fit the pace limit. One that
   can't be placed at all (closed every trip day, or over an hour from the hotel) is reported.
3. **Cluster-first day assignment:** each day takes the neighborhood with the most value
   (sum of scores that fit, ×1.5 per interest not yet covered), then tops up from neighborhoods
   within 25 taxi-minutes of it. A day holds at most one beach / mall / water park. The pace
   limit is on activity + transit time: relaxed 6 h, balanced 8 h, packed 10 h.
4. Within a day, order stops with nearest-neighbor + 2-opt (taxi-time heuristic), starting
   and ending at the hotel area; beaches and sunset spots move to the end of the day.
5. Build the timeline with real legs from the routes provider: start 09:00 (10:00 if early
   mornings are avoided), wait up to 60 min for a place to open, otherwise skip it; on hot days
   keep outdoor stops out of 11:00–16:00 when an indoor one can go first. If a day still
   exceeds the pace limit, drop its weakest stop and rebuild. Dropped stops are reported.
6. Meals: breakfast near the hotel, lunch from 12:15 near the previous stop, dinner from 18:30
   near the last stop; open at that time, in the tier's price bands, local cuisines first when
   food is an interest, never the same restaurant twice in a day (breakfast spots may repeat
   across days).
7. Legs follow a `TransportPolicy`: `balanced` (walk ≤ 15 min, abra/metro when within 10 min
   of a taxi), `public_first` (walk ≤ 20 min, abra/metro when within ~2× taxi time) or
   `fastest`. Walks are capped at 10 min on hot days or when long walks are avoided.
8. With no `start_date`, a trip starting on the first Monday of the month (default December)
   is assumed for weekdays, weather and prices, and the itinerary says so.

### 5.3 Budget engine (`agents/skills/budget_engine.py`)

Pure functions, unit-tested:

- **Accommodation** = nightly rate (season-adjusted area midpoint) × nights × rooms, plus a fees
  line: service charge + municipality fee + VAT (% of the room total) + Tourism Dirham per
  room-night. Nights = `days − 1` unless `nights` is given.
- **Food** = Σ meal slots × the restaurant's per-person midpoint × travellers.
- **Transport** = Σ leg costs (per person × travellers; taxis split four to a cab) + airport
  transfers (DXB airport tariff on arrival, street tariff on departure).
- **Activities** = Σ entry prices × travellers (published price when verified, else midpoint).
- **Contingency** = configurable % (default 10%) of the **estimated** part only.
- Every line keeps provenance: `verified` only when all its inputs are verified (e.g. the
  Al Shindagha ticket), otherwise `estimated`. Days get a `day_cost_aed`.
- **Alternatives** are measured, not guessed: each candidate changes one planning choice
  (cheaper hotel area, same tier or one tier down; `public_first` transport; cheaper meal bands;
  a paid attraction swapped for the nearest free one sharing an interest; or dropping it), the
  plan is re-scheduled and re-costed, and the difference is the saving. `fit_to_budget` applies
  the biggest saving and repeats until the plan fits or nothing helps. The Budget agent's LLM
  picks and phrases them; the arithmetic stays in Python.

### 5.4 Review rules (`agents/skills/rule_checks.py`)

Rule checks run before the LLM reviewer and are authoritative.

| Rule (`code`) | Severity | Routes revision to |
|------|----------|--------------------|
| `total > budget` (`over_budget`) | blocker | Budget |
| Accommodation > 50% of the budget (`hotel_cost_driver`, only when over budget) | major | Logistics |
| Day overloaded: activity + transit > pace limit (`day_overloaded`) | blocker | Assembly |
| Slot outside opening hours (`outside_opening_hours`) | blocker | Assembly |
| Seasonal place out of season (`closed_for_season`) | blocker | Destination |
| Place id not in the dataset (`unknown_place`) | blocker | Destination |
| Single leg > 60 min (`long_leg`) | major | Logistics |
| Day transit > 2.5 h (`day_transit_heavy`) | major | Assembly |
| Duplicate attraction across days (`duplicate_place`) | major | Destination |
| High-crowd place when user said "avoid crowds" (`crowded_place`) | major | Destination |
| Interest not represented, e.g. no beach (`interest_missing`) | major | Destination |
| A `must_visit` place is not in the plan (`must_visit_missing`) | major | Assemble |
| No lunch or dinner (`missing_meal`), move without a leg (`missing_leg`) | minor | Assembly |
| Same restaurant for several lunches/dinners (`repeated_restaurant`) | minor | Assembly |
| No budget attached yet (`budget_missing`) | minor | Budget |
| Outdoor slot 11:00–16:00 on a hot/extreme day (`outdoor_midday_heat`) | minor | Assembly |
| Starts before 09:00 / ends after 22:00 when avoided (`early_start`, `late_finish`) | minor | Assembly |

Meal type is read from the slot's start time (titles may be rewritten by the LLM). Places with
unknown opening hours are not flagged. Score = 100 − 30 per blocker − 12 per major − 3 per minor.

The LLM reviewer then adds qualitative issues (variety, pacing, preference fit) but **cannot
override a failed blocker**. `ReviewReport.passed = no blockers and no majors`.

---

## 6. Orchestration with LangGraph

### 6.1 State

```python
class PlannerState(TypedDict):
    raw_request: str
    request: TripRequest | None
    research: ResearchResult | None
    logistics: LogisticsResult | None
    draft: Itinerary | None
    budget: BudgetBreakdown | None
    review: ReviewReport | None
    revision: int
    revision_targets: list[str]                       # which nodes must re-run
    history: Annotated[list[ItinerarySnapshot], add]  # every draft, for the diff view
    trace: Annotated[list[TraceEvent], add]           # reducer appends from parallel nodes
```

`Annotated[..., add]` reducers let the two parallel branches write to `trace` at the same time
without conflicts. When the trip form is used, the run starts with `request` already set and
`parse` passes it through without calling the LLM.

### 6.2 Graph

```
            ┌────────┐
 START ───▶ │ parse  │  (Orchestrator: NL → TripRequest)
            └───┬────┘
                ▼
          ┌────────────┐
          │orchestrate │  (plan, decide which agents run this round)
          └──┬──────┬──┘
     fan-out │      │ fan-out
             ▼      ▼
   ┌────────────┐ ┌───────────┐
   │destination │ │ logistics │      ← run in parallel (same superstep)
   └─────┬──────┘ └─────┬─────┘
         └──────┬───────┘
                ▼
          ┌──────────┐
          │ assemble │  (Orchestrator + scheduler → draft Itinerary)
          └────┬─────┘
               ▼
          ┌──────────┐
          │  budget  │
          └────┬─────┘
               ▼
          ┌──────────┐
          │  review  │
          └────┬─────┘
               ▼
        route_after_review()
     ┌─────────┼──────────────┬───────────────┐
     ▼         ▼              ▼               ▼
  finalize  → budget     → destination    → assemble
     │     (cost issues) (content issues) (schedule issues)
     ▼
    END   (finalize picks the best draft and writes its day themes: one LLM call per run)
```

### 6.3 Conditional routing

```python
MAX_REVISIONS = 3

def route_after_review(state: PlannerState) -> str | list[str]:
    r = state["review"]
    if r.passed or state["revision"] >= MAX_REVISIONS:
        return "finalize"                  # finalize marks unresolved issues if capped
    targets = {issue.route_to for issue in r.issues if issue.severity != "minor"}
    if "destination" in targets or "logistics" in targets:
        return [t for t in ("destination", "logistics") if t in targets]   # partial re-fan-out
    if "assemble" in targets:
        return "assemble"
    return "budget"
```

- Only the agents responsible for an issue re-run; their prompts receive the issues as
  `revision_feedback`.
- `MAX_REVISIONS` guarantees termination. If the cap is hit, `finalize` attaches the
  outstanding issues and the best-scoring draft from `history`.
- **Changes the traveller asks for** after seeing a plan ("make it cheaper", "remove this
  stop", "swap this stop") enter the same loop: the UI adds them to the last review as issues
  with `origin=user` (budget target → Budget; removal → Assemble; swap → Destination) through
  the checkpointer (`update_state`), and the graph resumes from the router. Only the agents
  responsible re-run, exactly as for a failed review.
- A **checkpointer** (`MemorySaver`, or `SqliteSaver` for persistence) lets the UI replay
  each superstep and optionally pause for human approval before `finalize` (stretch goal:
  `interrupt_before=["finalize"]`).

### 6.4 Streaming to the UI

`graph.stream(input, stream_mode=["updates", "custom"])` gives per-node updates. Nodes also
emit custom `TraceEvent`s (tool calls, data source used, fallback triggered) via the stream
writer. The runner forwards them to the `TraceBus`.

---

## 7. LangGraph Capabilities Demonstrated

| Capability | How this project uses it |
|------------|--------------------------|
| **Shared state** | `PlannerState` is the single source of truth; every node reads from it and returns partial updates. The UI shows the state after each superstep. |
| **Parallel execution** | `orchestrate` fans out to `destination` and `logistics`, which run in the same superstep; reducers merge their trace events. |
| **Conditional decisions** | `route_after_review` picks finalize / budget / destination / assemble; on revision rounds only the responsible agents re-run. |
| **Feedback loop** | review → targeted node(s) → … → review cycle, bounded by `MAX_REVISIONS` and "no improvement" detection. |
| **Persistence & replay** | A checkpointer stores every superstep; the review-diff view uses the stored state history (time travel). |
| **Streaming** | `stream_mode=["updates", "custom"]` drives the live agent timeline. |
| **Human-in-the-loop** | The traveller's change requests are written into the checkpointed state (`update_state`) and the graph resumes from the router, re-running only the responsible agents. *(Stretch: `interrupt_before=["finalize"]` to approve a plan before it is finalised.)* |
| **Subgraphs** *(stretch)* | Destination research as a nested graph (attractions ∥ restaurants ∥ weather) to show composition. |

---

## 8. Run Metrics

The runner records `RunMetrics{wall_time, llm_calls, llm_cache_hits, tokens_in/out,
est_llm_cost, revisions, final_score, issues_found, issues_resolved}` for every run and shows
them in the UI (Dev mode → Metrics).

A single-LLM baseline (one prompt → `Itinerary`) was considered and left out: the graph's
review loop and its metrics already show what each agent adds.

---

## 9. Data Layer

See [research.md](research.md) for the per-source details. Architecturally:

### 9.1 Provider interfaces

```python
class PlacesProvider(Protocol):
    def neighborhoods(self) -> list[Neighborhood]: ...
    def search_attractions(self, query: AttractionQuery) -> list[Attraction]: ...
    def search_restaurants(self, query: RestaurantQuery) -> list[Restaurant]: ...
    def get_place(self, place_id: str) -> Attraction | Restaurant | None: ...

class RoutesProvider(Protocol):
    # ids are place or neighborhood ids; None when the mode isn't an option (e.g. walk 20 km)
    def travel(self, from_id: str, to_id: str, mode: TransportMode, *, party_size: int = 1) -> Leg | None: ...
    def matrix(self, neighborhood_ids: Sequence[str], modes: Sequence[TransportMode]) -> TravelMatrix: ...

class WeatherProvider(Protocol):
    def forecast(self, start: date, days: int) -> list[DayWeather]: ...

class HotelProvider(Protocol):
    def areas(self, tier: HotelTier | None = None) -> list[HotelArea]: ...
    def fees(self, tier: HotelTier) -> HotelFees: ...          # Tourism Dirham, service, municipality, VAT
    def season(self, month: int) -> HotelSeason: ...           # monthly rate multiplier

class PricingProvider(Protocol):
    def transit_fare(self, mode, product: FareProduct, zone_band: ZoneBand) -> TransitFare | None: ...
    def taxi_tariff(self, kind: TaxiKind) -> TaxiTariff | None: ...
```

`AttractionQuery` / `RestaurantQuery` are validated models (interests, cuisines, neighborhoods,
`near` + `radius_km`, `max_crowd`, free/indoor, exclusions, limit). Results rank by number of
matched tags, then lower crowd level, then distance. Leg costs are per person (taxi fares are
split across the party, four per cab). Price-table models (`TransitFare`, `TaxiTariff`,
`HotelFees`, `HotelSeason`) and weather models (`ClimateMonth`, `DayWeather`) live in
`schemas/pricing.py` and `schemas/weather.py`; records sourced as a whole extend `Provenance`.

### 9.2 Fallback chain

```
tools.search_attractions()
   └─ ChainedPlacesProvider([Overpass?, Local])
        • try each enabled provider in order
        • on error/timeout/quota/empty → log TraceEvent(kind="fallback") → next
        • Local always succeeds (curated dataset)
        • results merged with curated records by name/coords, curated fields win
          for prices & crowd levels (source = verified/estimated)
```

- Routing uses the same pattern: `ChainedRoutesProvider([OSRM?, GTFS?, LocalMatrix])`, with
  the haversine heuristic as the last resort.
- **Free sources only:** no paid APIs are used; see
  [research.md](research.md).
- Providers are enabled by feature flags in `core/config.py`.
- An on-disk HTTP cache (`requests-cache` / `diskcache`, TTL ≈ 7 days for places, 1 day for
  weather) keeps demos fast and quota-friendly.
- **Offline demo mode** (`DATA_MODE=local`) forces the local provider for fully reproducible runs.

### 9.3 Local dataset (SQLite)

Each table has flat, indexed columns for filtering plus the full validated record as JSON in
`data`; the provider reads records back through the same Pydantic models (so `location: LatLon`,
`OpeningHours` and every `SourcedValue` round-trip unchanged).

| Table | Filter columns (+ `data` JSON) |
|-------|-------------|
| `neighborhoods` | id, name, lat, lon, metro_station, beach_access |
| `attractions` (+ `attraction_tags`) | id, name, neighborhood_id, lat, lon, category, crowd_level, crowd_rank, indoor, price_min, price_max, price_source |
| `restaurants` (+ `restaurant_cuisines`, `restaurant_meals`) | id, name, neighborhood_id, lat, lon, price_band, crowd_level, crowd_rank, price_min, price_max |
| `hotel_areas` | neighborhood_id, tier, nightly_min, nightly_max, metro_access, beach_access |
| `hotel_fees`, `hotel_seasons` | tier; month, multiplier |
| `transit_fares` | mode, product (red/silver/gold/blue/cash), zone_band, fare_aed |
| `taxi_tariffs` | kind (street day/night, app peak/off-peak/night, airport) |
| `travel_matrix` | from_id, to_id, mode, duration_min, distance_km, source |
| `climate` | month |

Data pipeline (details in [data/README.md](../data/README.md)):

```
data/curation/*.json ─┐
                      ├─ scripts/curate.py ──▶ seed/{neighborhoods,attractions,restaurants}.json
raw/osm/*.json ───────┘   (match by name → coordinates, OSM id, opening_hours)
seed/neighborhoods.json ── scripts/build_matrix.py ──▶ seed/travel_matrix.json
Open-Meteo archive ─────── scripts/climate_normals.py ─▶ seed/climate.json
hand-curated ───────────────────────────────────────▶ seed/{hotels,pricing}.json
seed/*.json ── build_db.py (Dataset.problems() must be empty) ──▶ dubai.sqlite
```

Seed data is reviewed JSON in `data/seed/`. `build_db.py` validates it with the Pydantic models
plus cross-record checks (unique ids, known neighborhoods, provenance, complete fare/season/
climate tables) and refuses to build on any problem. `get_providers()` builds the database on
first use if it is missing.

---

## 10. LLM Layer

- `core/llm.py` exposes `make_llm()`. Each agent role can use a different provider and model.
  Defaults: Groq `openai/gpt-oss-120b` for the Orchestrator, Destination, Logistics and Budget
  agents, Gemini `gemini-3.5-flash-lite` for Review (both free tiers).
- Config (`.env`): `LLM_PROVIDER`, `LLM_MODEL`, optional `LLM_PROVIDER_<ROLE>` /
  `LLM_MODEL_<ROLE>`, API keys. A role without a key uses its deterministic fallback; the
  other roles still use their LLM.
- Groq uses `json_schema` structured output. Every call has a timeout, a retry limit, an output
  cap and (gpt-oss) low reasoning effort, so one call stays within Groq's 8,000 tokens per
  minute. Prompts send places as compact tables.
- Models are created with LangChain's `init_chat_model("provider:model")`, so switching
  provider is a configuration change.
- All structured outputs use `with_structured_output(<PydanticModel>)`, with one automatic
  retry that sends back the validation error.
- `temperature` is low (0–0.3) for parsing, budget and review, and moderate (≈0.6) for
  narrative text.
- **Free-tier protection** (`CachingLLM`, `QuotaGuard`):
  - an identical question is answered from a disk cache (`LLM_CACHE_HOURS`, default 24);
  - the whole app uses at most `LLM_DAILY_TOKEN_BUDGET` tokens a day (150,000);
  - each visitor session gets `LLM_PLANS_PER_SESSION` AI plans (3);
  - a provider's "rate limit" or "quota" answer pauses AI calls at once.

  Past any limit, planning continues without AI. The demo requests' answers are stored in
  `data/demo_llm_cache.json` by `python -m core.warmup`, so a demo makes no LLM calls.

---

## 11. Observability & UI

### 11.1 Trace events

```python
class TraceEvent(BaseModel):
    run_id: str; ts: datetime
    agent: str                                # orchestrator | destination | ...
    kind: Literal["start", "tool_call", "tool_result", "fallback",
                  "output", "review", "route", "revision", "end", "error"]
    summary: str                              # one human-readable line
    payload: dict | None                      # compact typed output
    revision: int
```

`TraceBus` is an in-process queue; the Streamlit app reads it while the run is in progress
(the runner runs in a worker thread and the UI polls the queue / uses `st.status` containers).

### 11.2 Streamlit screens

The app is "plan first, details when you need them": a traveller sees only what helps them
plan; a **Dev mode** switch reveals the technical views. The background is an illustrated
Dubai skyline (drawn in SVG, licence-free) that follows Streamlit's own light/dark theme: the
same scene by day and by night, animating between them; content sits on translucent cards so
it stays readable.

**Pages** (sidebar navigation, `st.navigation`):

1. **Plan a trip** — a form, no typing: dates, adults and children, budget, hotel type,
   interests, pace, things to avoid, diet, and must-visit places (picked from the dataset).
2. **Ask the agents** — the free-text request (pre-filled with the example request), example
   request chips, and checks before planning (e.g. "add a budget").
3. **Browse Dubai** — a map and filterable list of every place, each with "Add to my trip"
   (feeds the must-visit list).
4. **Saved trips** — plans saved in this session, side by side; download a trip as a file and
   upload it again (no accounts: §17).
5. **Travel data** *(dev mode)* — data sources, how fresh each one is (staleness report), data
   mode and live-source status, cache.

**While a plan is being built:** a progress view for travellers (understanding the request →
finding places → choosing a hotel area → building the days → checking the budget → final
review); in dev mode, the live agent timeline (one row per agent with status waiting / running /
done / revising, parallel agents side by side) and the graph with the active node highlighted.

**Results:**

- **Your plan** — a summary (days, hotel area, total against the budget, review verdict), then
  one card per day: time, place, how you get there and the cost, with a weather badge and the
  day's cost; a map per day with numbered pins matching the cards; provenance badges
  (`verified`, `api`, `estimated`); "Why this?" with the agents' reason for each place and for
  the hotel area.
- **Change requests** — "Make it cheaper", "Remove this stop", "Swap this stop" (§6.3).
- **Budget** — stacked bar by category against the budget line, cost lines, alternatives.
- *Dev mode:* **Review & revisions** (issues per round, where each was routed, the draft *n* →
  *n+1* diff), **Agent timeline**, **Graph** (Mermaid diagram plus a replay of every checkpoint
  with the shared state), **Metrics** (latency, LLM calls, tokens, revisions, score, live-data
  calls and fallbacks), assumptions, and the settings: model, data mode, max revisions.

---

## 12. End-to-End Sequence (example request)

```
User ──text──▶ Runner
Runner ─▶ Orchestrator.parse        → days=4, budget=5000, interests=[culture, food, beach],
                                      hotel_tier=budget, avoid=[crowds]
Orchestrator ─▶ [Destination ∥ Logistics]
   Destination → 14 attractions, 10 restaurants (Al Fahidi, Al Seef, Jumeirah, Kite Beach, Deira …)
   Logistics   → hotel areas: Al Barsha / Deira / Bur Dubai (budget), metro-first transport,
                 neighborhood matrix
Orchestrator.assemble → 4 DayPlans (Old Dubai · Deira souks · Jumeirah beaches · Hatta/Al Qudra or Marina)
Budget  → total AED 5,640 > 5,000  ✗   alternatives: switch hotel area (-AED 450),
          taxi→metro on day 2 (-AED 120), swap a paid attraction for a free one (-AED 150)
Review  → blocker: over budget; major: Dubai Mall slot conflicts with "avoid crowds"
route   → budget + destination
Revision 1 → Destination swaps Dubai Mall for Al Shindagha Museum; Budget applies swaps → AED 4,820
Review  → passed ✓  (minor: add sunscreen/heat note for beach day)
finalize → Itinerary (revisions=1)
```

This scenario is also a regression test (`tests/scenarios/example_request.py`) and covers
success criterion "detect and revise at least one constraint violation".

---

## 13. Error Handling & Guardrails

- **Input guardrails**: non-Dubai destination, > 7 days, or a non-AED currency → the parser
  normalises the request (converting currency with a configured rate and flagging it) or
  returns a clear validation message.
- **Tool failures** → fallback chain; never fail a run because an external API is down.
- **LLM output invalid** → one retry with the validation error, then fall back to a
  deterministic default (e.g. the top-k ranked by score) and log a `TraceEvent(kind="error")`.
- **Hallucination control**: agents may only reference `place_id`s returned by tools; the
  assembler rejects unknown IDs, and the Review rules flag missing provenance.
- **Loop termination**: `MAX_REVISIONS`, plus "no improvement" detection (the same issues two rounds in a row → stop).
- **Budget safety**: prices that are `estimated` get the contingency buffer; the UI states clearly that prices are not guaranteed.
- **Impossible budgets**: a budget below the cheapest hotels for the trip (the lowest nightly
  rate × nights × rooms) is warned about before planning. A plan still over budget after the
  loop says so plainly: the cheapest plan's price, the gap, and a budget that would work.

---

## 14. Configuration

`.env.example` (abridged; the full list with comments is in the file)

```
LLM_PROVIDER=groq                    # groq | google_genai | anthropic | openai | ollama
LLM_MODEL=openai/gpt-oss-120b
LLM_PROVIDER_REVIEW=google_genai     # optional per-role overrides: LLM_PROVIDER_<ROLE>
LLM_MODEL_REVIEW=gemini-3.5-flash-lite
GROQ_API_KEY=
GOOGLE_API_KEY=
# LLM_TIMEOUT_S=60  LLM_MAX_RETRIES=2  LLM_MAX_TOKENS=1500  LLM_REASONING_EFFORT=low
# LLM_CACHE_HOURS=24  LLM_DAILY_TOKEN_BUDGET=150000  LLM_PLANS_PER_SESSION=3

DATA_MODE=local                      # local | hybrid | live
USE_OVERPASS=true
USE_OSRM=true
OSRM_CAR_URL=https://router.project-osrm.org        # public demo server: light use only
OSRM_FOOT_URL=https://routing.openstreetmap.de/routed-foot
USE_OPEN_METEO=true
# EXTERNAL_CALL_BUDGET=40  MIN_REQUEST_INTERVAL_S=1

MAX_REVISIONS=3
CONTINGENCY_PCT=10
CACHE_DIR=.cache
```

On Streamlit Community Cloud the keys go in the app's **Secrets** instead of `.env`.

---

## 15. Testing Strategy

| Layer | Tests |
|-------|-------|
| Budget engine, rule checks, scheduler | Pure unit tests with fixtures; property tests (total = Σ lines, no slot overlaps) |
| Providers | Recorded HTTP fixtures (VCR-style); fallback triggered on simulated 429/timeout |
| Agents | LLM stubbed with canned structured outputs to test the wiring |
| Graph | Compiled graph returns a valid `Itinerary`; both parallel branches run; the router sends each issue type to the right node; the loop stops at `MAX_REVISIONS` |
| Scenarios (`tests/scenarios/`) | Golden requests: the example request, a 1-day low-budget trip, a 7-day luxury trip, a forced over-budget case (must revise, only the responsible agents re-run), and an "avoid crowds" case (no high-crowd places, while the same trip without it has some). Error paths: made-up LLM ids, a broken or rate-limited LLM, every live service down, an impossible budget |
| UI | Streamlit `AppTest` runs the pages headlessly: form planning, Dev mode, change requests, saved trips, pre-planning checks |
| Real LLM (`-m llm`) | One smoke run with the configured providers; excluded by default |

---

## 16. Delivery Milestones

1. **Foundation** — schemas, config, local dataset + `build_db.py`, budget engine, rule checks, scheduler (no LLM).
2. **Agents core** — five agents with prompts and tools; a single sequential runner as a baseline.
3. **LangGraph orchestration** — state, parallel fan-out, router, revision loop, checkpointer, streaming.
4. **UI** — timeline, graph view, itinerary, budget, review diff, run metrics.
5. **Live data** — Overpass, OSRM, Open-Meteo (and RTA GTFS if available) behind feature flags + cache.
6. **Traveller UX** — trip form, browse, saved trips, day cards, change requests, dev mode, light/dark skyline.
7. **Hardening** — scenario tests, error paths, fewer LLM calls, demo script.

---

## 17. Out of Scope (MVP)

Live booking or payments, guaranteed real-time prices, flights, multi-city trips, user
accounts/persistence beyond local checkpoints (saved trips live in the browser session or a
downloaded file), child-specific prices, and destinations other than Dubai.
