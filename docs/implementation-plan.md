# Implementation Plan — Dubai AI Travel Planner

> Phase-wise build plan derived from [problemStatement.md](problemStatement.md),
> [architecture.md](architecture.md) and [research.md](research.md).
> Each phase ends with something runnable and testable. Later phases build on earlier ones;
> nothing in a phase depends on a later one.

**Guiding rules**

- Deterministic code first, LLM second, orchestration third, UI fourth, live data last.
- Free and open data sources only (see research.md cost policy). The LLM is the only metered dependency.
- The app must run end to end offline (`DATA_MODE=local`) from Phase 5 onwards.
- Every phase has unit tests; nothing merges with failing tests.

---

## Phase Overview

| # | Phase | Outcome | Est. effort |
|---|-------|---------|-------------|
| 0 | Project setup | Repo, tooling, config, CI skeleton | 0.5–1 day |
| 1 | Shared contracts | All Pydantic schemas with provenance | 1–2 days |
| 2 | Curated Dubai dataset | Seed JSON → validated SQLite + local providers | 3–5 days |
| 3 | Deterministic skills | Budget engine, rule checks, scheduler | 3–4 days |
| 4 | Agents + LLM layer | Five agents with prompts and tools; sequential baseline runner | 3–4 days |
| 5 | LangGraph orchestration | Stateful graph, parallel fan-out, router, review loop, streaming | 3–4 days |
| 6 | Streamlit UI | Timeline, graph view, itinerary, budget, review diff, metrics | 3–4 days |
| 7 | Free live data | Overpass, OSRM, Open-Meteo, (GTFS) behind flags + cache | 3–4 days |
| 8 | Traveller UX | Trip form, browse, saved trips, day cards, change requests, dev mode, day/night skyline, fewer LLM calls | 4–5 days |
| 9 | Hardening & demo | Scenario tests, baseline comparison, docs, demo script | 2–3 days |

Estimates are for one developer and are planning guides only.

```
P0 ─▶ P1 ─▶ P2 ─▶ P3 ─▶ P4 ─▶ P5 ─▶ P6 ─▶ P8 ─▶ P9
                   │                  ▲
                   └──────▶ P7 ───────┘   (P7 can start after P2, in parallel with P4–P6)
```

---

## Phase 0 — Project Setup

**Goal:** a clean, reproducible Python project that every later phase plugs into.

**Tasks**

- [x] Create the repository layout from architecture.md §3 (`app/`, `core/`, `schemas/`, `agents/`, `tools/`, `data/`, `graph/`, `tests/`).
- [x] `pyproject.toml` (Python 3.11+) with the dependencies: `langgraph`, `langchain`, LLM provider packages (`langchain-anthropic`, `langchain-openai`, …), `pydantic`, `pydantic-settings`, `streamlit`, `httpx`, `requests-cache`/`diskcache`, `pydeck` or `folium`; dev: `pytest`, `pytest-cov`, `ruff`, `mypy`, `vcrpy`/`respx`, `hypothesis`.
- [x] `core/config.py` with pydantic-settings, and `.env.example` matching architecture.md §14.
- [x] Ruff + mypy + pytest configured; pre-commit hooks.
- [x] CI workflow (lint, type-check, tests) — optional if no remote yet.
- [x] `README.md` with setup and run commands.

**Status: done.** Choices made: `diskcache`, `pydeck` and `respx`; `langchain-openai` and
Ollama are optional extras; `DATA_MODE` defaults to `local` until Phase 7. Pre-commit hooks
are configured but not installed, because the project is not a git repository yet.

**Deliverables:** an installable project skeleton; `pytest` runs (no tests yet); config loads from `.env`.

**Exit criteria:** `ruff`, `mypy` and `pytest` all pass on an empty project; settings load correctly with and without a `.env`.

---

## Phase 1 — Shared Contracts (Pydantic Schemas)

**Goal:** define every data shape once, so agents, graph, UI and tests agree.

**Tasks**

- [x] `schemas/provenance.py` — `Source` enum (`verified`, `api`, `estimated`), generic `SourcedValue[T]`.
- [x] `schemas/request.py` — `TripConstraints` (1–7 days, AED budget, interests, hotel tier, avoid, pace, dietary), `TripRequest`.
- [x] `schemas/places.py` — `Place`, `Attraction`, `Restaurant`, `HotelArea`, `PriceRange`, `Neighborhood`.
- [x] `schemas/logistics.py` — `Leg`, `TransportOption`, `TravelMatrix`, `LogisticsResult`.
- [x] `schemas/budget.py` — `CostLine`, `BudgetBreakdown`, `BudgetAlternative`.
- [x] `schemas/review.py` — `ReviewIssue` (severity, `route_to`), `ReviewReport`.
- [x] `schemas/itinerary.py` — `Slot`, `DayPlan`, `Itinerary`, `ItinerarySnapshot`; `ResearchResult`.
- [x] `core/tracing.py` — `TraceEvent` model (architecture.md §11.1); `RunMetrics`.
- [x] Validators: time ordering in slots, non-negative costs, `total = Σ lines`, `days` within range.

**Status: done** (architecture.md §4 updated to match). Design changes made during this phase:
`location: LatLon` limited to Dubai; per-weekday `OpeningHours` (overnight windows carry
into the next day); `avoid` as a fixed `Avoidance` enum; `rooms` and `nights` on the request;
`Itinerary.budget`/`review` optional for drafts, with `is_final`; `within_budget` uses an
exact comparison in both `from_lines` and the validator (a Hypothesis test caught the mismatch).

**Deliverables:** a complete schema package with JSON-schema export (useful for prompts).

**Exit criteria:** unit tests cover valid/invalid examples for each model; a hand-written sample `Itinerary` for the example request validates.

---

## Phase 2 — Curated Dubai Dataset & Local Providers

**Goal:** a reliable offline data source that fully covers the example request (research.md §3.1 Phase 1, §3.2).

**Tasks — data collection**

- [x] Choose ~15–20 neighborhoods (research.md §3.2 step 1) with coordinates, nearest metro station, beach access.
- [x] Bootstrap POIs with one Overpass export per neighborhood (a one-off script in `data/scripts/`).
- [x] Curate ~60 attractions: tags, `crowd_level`, `typical_duration_min`, opening hours per weekday (or `always_open`; Friday hours often differ), coordinates inside Dubai, entry prices from official sources, `source_url`, `as_of`. Mark free places explicitly.
- [x] Curate ~60 restaurants: cuisine, price band, average meal AED, meal types.
- [x] Build the `hotel_areas` table: neighborhood × tier → nightly range, season multiplier, notes; Tourism Dirham, service charge and VAT add-ons.
- [x] Record RTA fares (`transit_fares`) and taxi tariffs (`taxi_tariffs`) from rta.ae, dated.
- [x] Build the `travel_matrix` for neighborhood pairs × modes (taxi, walk, metro): heuristic values first (tagged `estimated`); refine with OSRM/GTFS in Phase 7.
- [x] Settle the open questions that block data entry: target demo season, current RTA fares, Tourism Dirham rates.

**Tasks — code**

- [x] `data/seed/*.json` + `data/build_db.py`: validate with the Phase 1 schemas, fail on missing provenance, build `dubai.sqlite`.
- [x] `data/providers/base.py` — `PlacesProvider`, `RoutesProvider`, `WeatherProvider`, `HotelProvider` protocols.
- [x] `data/providers/local.py` — SQLite-backed implementations of all four, plus a haversine × mode-speed fallback for routes and monthly climate averages for weather.
- [x] Chained-provider wrappers (`ChainedPlacesProvider`, `ChainedRoutesProvider`) with fallback trace events — only the local provider is wired in for now.
- [x] `tools/` — typed functions: `search_attractions`, `search_restaurants`, `get_hotel_areas`, `travel_time`, `transit_fare`, `weather_forecast`, `price_lookup`.
- [x] A staleness report script: list records with `as_of` older than N months.

**Deliverables:** `dubai.sqlite` built from version-controlled JSON; tools return typed, sourced results offline.

**Exit criteria:** `build_db.py` passes validation; tool unit tests pass; the example request's needs (culture, local food, beaches, budget hotels, low-crowd options) are each covered by at least 5 candidates.

**Status: done** (details in [data/README.md](../data/README.md), sources in research.md §3.5).
21 neighborhoods, 50 attractions and 62 restaurants (109 of 112 matched to OpenStreetMap),
17 hotel areas, official RTA fares and Dubai Taxi tariffs, Open-Meteo climate normals and a
287-entry heuristic travel matrix. Coverage: culture 26, beach 9, local food 44, budget hotel
areas 5, low-crowd 24. Choices made: curation inputs in `data/curation/` are merged with the OSM
export by `curate.py` (a test fails if the committed seed drifts from its inputs); places that
match OSM are tagged `verified` with the OSM link; a `PricingProvider` was added for fares and
tariffs; leg costs are per person; demo season is modelled per month (monthly hotel multipliers
and climate). Still `estimated`: most attraction prices, all hotel rates and fees, 46 unknown
opening hours (mostly restaurants).

---

## Phase 3 — Deterministic Skills

**Goal:** everything involving money, time and rules works without an LLM (architecture.md §5.2–5.4).

**Tasks**

- [x] `agents/skills/budget_engine.py`
  - [x] Accommodation (rate × nights × rooms + Tourism Dirham + service charge/VAT), food, transport (RTA fares, taxi formula, airport transfers), activities, contingency %.
  - [x] Alternative generator: ranked swaps with AED savings (cheaper hotel area, taxi → metro, paid → free attraction with the same tag, cheaper restaurant band).
- [x] `agents/skills/scheduler.py`
  - [x] Candidate scoring (`interest_match × (1 − crowd_penalty) × quality`).
  - [x] Neighborhood clustering and day assignment by pace (6 / 8 / 10 h).
  - [x] Stop ordering (nearest-neighbor + 2-opt) from the travel matrix, starting and ending at the hotel area.
  - [x] Opening-hours and heat-aware slotting; meal insertion near the preceding stop; a `Leg` on every move.
- [x] `agents/skills/rule_checks.py` — every rule in architecture.md §5.4 with severity and `route_to`.
- [x] A "scripted planner" test harness: request → scheduler → budget → rules, with no LLM.

**Deliverables:** a fully deterministic draft itinerary for the example request.

**Exit criteria:**

- Unit + property tests: `total = Σ lines`, no overlapping slots, slots inside opening hours, no duplicate places.
- A forced over-budget fixture triggers the `total > budget` blocker, and applying the generated alternatives brings it under budget.
- An "avoid crowds" fixture with a high-crowd place triggers the correct major issue.

**Status: done.** `agents/skills/` has `calendar.py`, `scheduler.py`, `budget_engine.py`,
`rule_checks.py` and `scripted.py` (`uv run python -m agents.skills.scripted` prints the
example plan: 4 days, AED ~2,380 of 5,000, review score 100). A Hypothesis test plans 30 random
requests per run (300 checked once) and asserts totals add up, opening hours, pace, no repeated
attractions or restaurants, and a leg on every move. The forced over-budget fixture (mid tier,
AED 3,000, costed at ~AED 5,670) raises the `over_budget` blocker and `fit_to_budget` brings it to ~AED 2,510.
Choices made: cluster-first day assignment (each day anchored on its best neighborhood, topped
up from areas within 25 min); alternatives' savings are measured by re-planning, not estimated;
contingency applies to estimated prices only; breakfast, lunch and dinner are all scheduled;
meal type is inferred from the slot time; trips without a start date assume the first Monday of
the month (default December). Found and fixed on the way: taxi-time bands made some longer
trips faster than shorter ones (now applied per stretch, so time always grows with distance).

---

## Phase 4 — Agents & LLM Layer

**Goal:** five specialised agents that combine LLM reasoning with the Phase 2–3 tools and skills (architecture.md §5, §10).

**Tasks**

- [x] `core/llm.py` — `get_llm(role)` using `init_chat_model`, per-role model overrides, temperatures per role, structured output with one validation-error retry. The model stays a configuration choice (see Open Decisions).
- [x] LangChain tool wrappers over `tools/` that return **compact views** for the LLM (id, name, neighborhood, tags, crowd level, price range + source, duration, opening-hours summary). The full records (~11k tokens for all attractions, ~11k for restaurants) stay in code.
- [x] `agents/prompts/*.md` — one prompt per agent, including the schema, rules ("only use `place_id`s returned by tools") and a `revision_feedback` section. **Data-grounding rules:** justify picks only with dataset fields (tags, crowd level, price, duration, hours — there are no descriptions or ratings); recommend hotel *areas*, never named hotels; don't state restaurant hours when they're unknown (46 of 62).
- [x] `agents/orchestrator.py` — `parse()` NL → `TripRequest` (with assumptions); `assemble()` research + logistics → draft `Itinerary` via the scheduler, LLM writes day themes and notes.
  - [x] Map phrases to the fixed enums (e.g. "museums" → culture, "affordable" → budget tier, "busy places" → crowds).
  - [x] Travel month without dates ("in March"): record it in `assumptions` and pass it to the scheduler (`SchedulerOptions.month`); no schema change. Without a month, the December assumption is recorded.
  - [x] Dietary: normalise to what the data supports (`vegetarian`); anything else (e.g. vegan) is recorded as an assumption that it can't be guaranteed.
- [x] `agents/destination.py` — calls the search tools, LLM ranks and justifies picks against interests and "avoid"; its picks become the scheduler's candidates; accepts review issues on revision.
  - [x] Coverage check: when an interest has fewer than 2 suitable places (e.g. nightlife or adventure with "avoid crowds"), record "limited data for X" as an assumption instead of leaving it for Review to raise every round.
- [x] `agents/logistics.py` — hotel-area choice, transport options, travel matrix; LLM explains trade-offs.
- [x] `agents/budget.py` — runs the budget engine; LLM picks and phrases alternatives (no arithmetic by the LLM). Prefer alternatives within the requested hotel tier; propose a downgrade only with a stated reason.
- [x] `agents/review.py` — rule checks first (authoritative), then the LLM adds qualitative issues; computes `passed` and a score.
- [x] Guardrails: reject unknown `place_id`s, deterministic fallback when LLM output is still invalid after the retry (reuse the Phase 3 scripted planner), input normalisation (non-Dubai, > 7 days, non-AED).
  - [x] Currency rates in `core/config.py` (USD at the official 3.6725 peg, plus a few configurable rates); a converted budget is flagged in `assumptions`.
- [x] Scheduler: filter meal restaurants by `dietary` (small addition to the Phase 3 scheduler).
- [x] `core/runner.py` (temporary) — a plain sequential runner: parse → destination → logistics → assemble → budget → review → (one revise pass).

**Deliverables:** the example request produces a valid `Itinerary` through the sequential runner.

**Exit criteria:**

- Agent tests pass with a stubbed LLM (canned structured outputs).
- One real-LLM smoke test passes for the example request — **passed 2026-11-17** (Groq + Gemini).
- The parser extracts the correct constraints from 5 varied sample requests that cover the data's edge cases: a month or dates, a USD budget, more than 7 days, a non-Dubai destination, a vegetarian diet, 2 travellers in 1 room, and phrases that map to interests ("museums", "nightlife").

**Status: done** (the real-LLM smoke test passed on 2026-11-17; see the note at the end). `core/llm.py` (`get_llm`, `LangChainLLM` with one retry,
`ScriptedLLM` for tests), `tools/views.py` + `tools/langchain_tools.py`, six prompts, the five
agents and `core/runner.py` (`uv run python -m core.runner ["request"] [--no-llm]`). Agent
tests use canned LLM answers; the parser passes the 5 sample requests (keyword parser). Without
an API key every agent uses its deterministic fallback, so the runner works offline: the
example request passes with score 100, and a mid-tier AED 3,000 request is revised once
(Budget + Logistics) to within budget.
Choices made: agents call the tools in code and the LLM ranks, picks and explains from compact
views (no free-form tool loop); LLM review comments are always minor (decision of 2026-11-15);
the coverage threshold is "fewer than 3" places (2 never fired on the real data); the Budget
agent rejects an alternative that would create a new blocking issue and goes straight to a tier
downgrade only when no same-tier combination can close the gap; a rule `dietary_not_met`
(minor) was added. Scheduler fixes found by running the agents: places that open later in the
day (dhow cruise, Global Village) are now scheduled around dinner — before, the "nightlife"
interest could never be met; a restaurant may be reused on another day rather than skip a meal
(few vegetarian places); areas more than 60 min from the hotel (Hatta) are no longer planned,
because every leg there breaks the long-leg rule.
Plan-quality fixes after reviewing real runs (2026-11-17): no walking across Dubai Creek
(old-town crossings use the abra; taxis pay a tunnel/bridge detour) and meals stay on the
hotel's side; stop ordering penalises creek crossings and keeps beaches last while finishing
the day near them; a day only tops up from areas within 18 min, a fill pass and a carry-over
use spare afternoon time, and a coverage repair guarantees each interest a nearby stop; lunch
comes before a long stop that would skip it; swap descriptions separate the ticket saving from
the re-planned saving; heavy-travel days caused by a far hotel are routed to Logistics, which
then won't pick the blamed area again, and other heavy days drop their longest detour.
Real-LLM run (2026-11-17, Groq `openai/gpt-oss-120b` + Gemini): the first run failed. Destination's
prompt was ~8.5k tokens, over Groq's free-tier 8,000 tokens per minute; Gemini returned 503;
and retries stretched the run to 6 min. Fixes: places go to the LLM as compact tables (column
names once, then rows) with the top 20 restaurants (Destination ~6.9k → ~2.7k tokens) and all
prompt JSON is unindented; per-call limits (`LLM_TIMEOUT_S` 60, `LLM_MAX_RETRIES` 2,
`LLM_MAX_TOKENS` 1500, `LLM_REASONING_EFFORT` low for gpt-oss); review moved to
`gemini-3.5-flash-lite` (the `gemini-flash-latest` alias returned 404 for this key and the larger
Flash models were overloaded); LLM-written text over its length limit is now trimmed
(`ClipTo`) instead of rejecting the whole answer (Gemini wrote a 500-character summary). Now:
example request ~20 s, over-budget request ~35 s with 11 LLM calls and no fallbacks. The guardrails
were exercised for real: gpt-oss invented a place id (dropped) and picked a cheaper option that
caused a 60+ min leg (rejected by the Budget agent).

---

## Phase 5 — LangGraph Orchestration

**Goal:** replace the sequential runner with the stateful graph that demonstrates the core LangGraph capabilities (architecture.md §6–7).

**Tasks**

- [x] `graph/state.py` — `PlannerState` with `add` reducers for `trace` and `history`.
- [x] `graph/nodes.py` — thin wrappers: `parse`, `orchestrate`, `destination`, `logistics`, `assemble`, `budget`, `review`, `finalize`; each emits `TraceEvent`s via the stream writer.
- [x] `graph/routing.py`
  - [x] `orchestrate` fan-out: both agents on the first round; only the targeted ones on revision rounds.
  - [x] `route_after_review`: finalize / budget / destination+logistics / assemble.
  - [x] Loop termination: `MAX_REVISIONS` and "same issues twice in a row" detection; `finalize` picks the best draft from `history` if capped.
- [x] `graph/build.py` — build and compile the `StateGraph` with a checkpointer (`MemorySaver`; `SqliteSaver` optional).
- [x] `core/runner.py` — `run(request) -> RunResult{itinerary, trace, metrics}` using `graph.stream(..., stream_mode=["updates", "custom"])`; collect `RunMetrics` (time, LLM calls, tokens, revisions, score).
- [x] Export the graph's Mermaid diagram for docs and the UI.

**Deliverables:** the graph runs the example request end to end and revises at least once.

**Exit criteria (graph tests):**

- `destination` and `logistics` execute in the same superstep.
- Each issue type routes to the correct node; only targeted nodes re-run on revisions.
- The loop stops at `MAX_REVISIONS`, and on "no improvement".
- The checkpointer history contains every draft (needed for the diff view).
- **Success criterion met:** the forced over-budget scenario is detected and revised to within budget.

**Status: done.** `graph/state.py` (`PlannerState`, plus `GraphContext` for providers, LLM and
settings, passed as LangGraph's runtime context so checkpoints hold only data),
`graph/nodes.py`, `graph/routing.py`, `graph/build.py` (`InMemorySaver` with our types
allow-listed for deserialisation) and `core/runner.py` (`run()`, replacing the sequential
runner; `TraceBus` in `core/tracing.py`). Diagram: [graph.mmd](graph.mmd)
(`uv run python -m graph.build`). Offline, the example request passes first time and the
mid-tier AED 3,000 request goes review → orchestrate → logistics → assemble → budget → review
and passes (AED ~2,433); destination is not re-run. Graph tests are in `tests/graph/`.
Choices made: the review node decides whether to revise (passed / `MAX_REVISIONS` / the same
blocking issues twice) and the router only maps that onto nodes; destination/logistics revisions
go back through `orchestrate`, which fans out to the targeted agents only; plain edges (not a
join) lead into `assemble`, so a round where only one research agent runs still continues; an
over-budget plan is costed but not revised in round 1, so Review reports the violation before it
is fixed; when the loop is capped, `finalize` returns the best-scoring draft and records the
unresolved issues in `assumptions`. `SqliteSaver` was not added (needs an extra package).
LLMs (decision of 2026-11-17): Groq `openai/gpt-oss-120b` for the orchestrator, destination,
logistics and budget agents and Gemini (`gemini-3.5-flash-lite`) for review, set per role with
`LLM_PROVIDER_<ROLE>`/`LLM_MODEL_<ROLE>`; Groq uses JSON-schema structured output. A role whose
key is missing falls back to deterministic code while the others still use their LLM.

---

## Phase 6 — Streamlit UI

**Goal:** make agent execution, shared state and revisions visible to technical and non-technical audiences (architecture.md §11.2).

**Tasks**

- [x] `app/streamlit_app.py` — run the graph in a worker thread and read the `TraceBus` queue for live updates.
- [x] **Plan screen:** request text area (pre-filled with the example request), options (max revisions, data mode, model).
- [x] **Live agent timeline:** per-agent status (waiting / running / done / revising), summary line, expandable output; parallel agents side by side.
- [x] **Graph view:** the Mermaid diagram with the active node highlighted and the revision loop visible.
- [x] **Itinerary:** day tabs, slot timeline, map of each day's route, provenance badges, OSM attribution.
- [x] **Budget:** stacked bar by category against the budget line, cost-line table, alternatives with savings, verified vs estimated split.
- [x] **Review & revisions:** issues per round, the agent each was routed to, diff of draft *n* vs *n+1*.
- [x] **Run metrics** panel.
- [x] A disclaimer that prices are estimates and nothing is booked.

**Deliverables:** an interactive demo app runnable with `streamlit run app/streamlit_app.py`.

**Exit criteria:** the example request runs from the UI in offline mode; a viewer can see which agent is executing, what it produced, and how the plan changed during review (problem statement §7).

**Status: done.** `app/streamlit_app.py` (thin page), `app/state.py` (`RunHandle`: the graph
runs in a daemon thread; the script drains the `TraceBus` every 0.25 s and redraws) and
`app/components/` — `timeline.py`, `graph_view.py`, `itinerary.py`, `budget.py`, `review.py`,
`metrics.py`. Data shaping is in plain functions with unit tests, and a headless `AppTest` plans
the over-budget request offline end to end (`tests/unit/test_app.py`). Result tabs: Itinerary
(day tabs, slot table with cost and provenance, pydeck route map from and back to the hotel
area, OSM/CARTO attribution), Budget (stacked bar with the budget line, category metrics,
verified/api/estimated split, cost lines, alternatives with "applied"), Review & revisions
(issues per round with their route, the router's decision, and the draft n → n+1 diff: hotel,
costs, score, places added/removed/re-timed), Agent timeline, Graph and Metrics.
Choices made: trace events now carry their graph node in the payload (`AgentContext.node`), so
the graph view can highlight the running node(s) — both research nodes at once while they run
in parallel; besides the live view, a "superstep" slider replays the checkpoints and shows the
shared state at each one; Mermaid is drawn in an iframe with mermaid.js from jsDelivr (needs
internet; the rest of the UI works offline); the data-mode option is shown but only `local`
exists until Phase 7; the model option is "configured LLMs" or "no LLM", with each agent's
provider and key status listed.

---

## Phase 7 — Free Live Data Integration

**Goal:** add live, key-free data behind feature flags without breaking offline reliability (research.md §2.3–2.4, §2.8, §3.4). Can start after Phase 2.

**Tasks**

- [x] `data/providers/overpass.py` — POI search by category and radius; merge with curated records (curated prices and crowd levels win).
- [x] `data/providers/osrm.py` — `/route` and `/table` for car and foot; peak-hour multiplier for car times.
  - [x] Decide self-hosted (Docker + Geofabrik GCC extract) vs public demo server for the demo.
  - [x] Refresh the stored `travel_matrix` with OSRM values (tagged accordingly).
- [x] ~~`data/providers/gtfs.py`~~ (not built: no open feed, see status) — only if an RTA GTFS feed is confirmed: metro/tram station-to-station times; otherwise curate metro times manually.
- [x] `data/providers/open_meteo.py` — daily forecast; climate-average fallback beyond the forecast range; heat rules feed the scheduler.
- [x] Wire live providers into the chained providers; `DATA_MODE=local|hybrid|live`.
- [x] HTTP cache with per-source TTLs; rate limiting, generic User-Agent, per-run external-call budget; log every call and fallback in the trace.
- [x] Recorded HTTP fixtures for tests; simulated timeouts/429s to test fallback.
- [ ] Optional: Amadeus test-environment provider (hotels), clearly marked optional.

**Deliverables:** hybrid mode uses live POIs, routing and weather, and falls back silently to local data on failure.

**Exit criteria:** provider tests pass from fixtures; with the network disabled the app still completes in hybrid mode (fallback events visible in the trace); provenance badges show `api` for live values.

**Status: done** (details in [data/README.md](../data/README.md#live-data-phase-7)). Stored
routes: `data/scripts/osrm_tables.py` fetched real driving and walking times between all 133
curated points from the public OSRM servers (~32 requests, once) into `seed/road_times.json`;
every taxi/walking leg between curated places now uses them (before, only neighborhood pairs used
the matrix and every place-to-place leg was the haversine heuristic), and `build_matrix.py`
derives the neighborhood matrix from them. Live providers: `osrm.py`, `open_meteo.py`,
`overpass.py`, on a shared `http.py` (disk cache with per-source TTLs, 1 request/s per host,
generic User-Agent, per-run call budget, a trace event per call) and `runtime.py` (per-run
context: graph nodes route data events into their own trace, even in parallel). The UI's data
mode now switches the chains, and the Metrics tab lists network calls and fallbacks. Measured:
hybrid ~13 s with 5 live places and 9 live legs; live ~70 s the first time (92 OSRM calls),
instant when cached. Tests: 24 offline provider/graph tests from recorded responses, including
the two exit criteria (network down → hybrid run passes with fallbacks in the trace; live values
carry `api` and the UI badge). 386 tests pass; the 300-trip property test passes.
Choices made: OSRM uses the **public servers** (car: router.project-osrm.org, foot:
routing.openstreetmap.de) for a one-off stored table plus light live use, since Streamlit Cloud
can't run Docker (`OSRM_BASE_URL` became `OSRM_CAR_URL` / `OSRM_FOOT_URL`); stored OSRM routes
are tagged `estimated`, not `api` (research §3.3: `api` = fetched in this run); taxi legs add a
5-min pickup and a ×1.35 city-traffic factor that fades beyond ~10 km; walking keeps the creek
rule (OSRM's foot profile crosses on the abra ferry); **no GTFS**: the RTA feed on Dubai Pulse
needs a permission request and public copies are token-gated or stale, so metro times stay
heuristic; hybrid routes check the stored data first and use live OSRM only for live places,
live mode routes every road leg; Overpass is one city-wide query cached for a week and
downloaded in the background (public servers took 27–60 s), so runs never wait for it; live
places are attractions only, a few per search, dropped when a curated record covers them;
restaurants stay curated; a provider that doesn't handle a request (`NotApplicable`) hands over
silently, "no provider handles it" means "not available", and an unexpected error in a live
provider becomes a fallback. Found and fixed on the way: OSRM's slower desert-highway times made
a 1-day Al Qudra trip break the long-leg rule, so the traffic factor now fades on highways and
the scheduler's "within an hour of the hotel" check uses the same routes as the legs; the first
live place in a schedule crashed a run (no provider for metro to an unknown place), fixed by the
"not available" rule plus an estimating last link in the routes chain. Amadeus was not added.

---

## Phase 8 — Traveller UX

**Goal:** a planner a non-technical traveller can use without typing, with the technical views
one switch away (architecture.md §4.3, §6.3, §11.2; agreed 2026-11-18). The design rule is
"plan first, details when you need them".

**Tasks — contracts and agents**

- [x] `schemas/request.py` — `TripConstraints.children` (fewer than `travelers`, so at least one adult; `adults` property) and `must_visit: list[Slug]` (at most 10, de-duplicated); `IssueOrigin.USER` in `schemas/review.py`.
- [x] Orchestrator: the keyword and LLM parsers read children ("2 kids", "a family of four with two children") and must-visit places named in the text (matched to dataset names); an unknown must-visit place is recorded in `assumptions`.
- [x] Children: count as travellers for every cost and charge the adult price (no child prices in the dataset), with an assumption saying so; family-tagged places score higher.
- [x] Destination: `must_visit` places are always in the research result, first, whatever the interest and crowd filters; the LLM is told they are required.
- [x] Scheduler: must-visit places are placed first (their area anchors a day) and are never the stop dropped to fit the pace; one that can't be placed (closed every trip day, over an hour from the hotel) is reported with the reason.
- [x] Rule check `must_visit_missing` (major → Assemble).
- [x] Graph: a run can start with `request` already set (the trip form); `parse` then passes it through without the LLM.
- [x] Fewer LLM calls: reuse the stored plan for the same request and settings within 24 h (no LLM calls); the trip form skips parsing; revision rounds don't rewrite day themes after a budget-only fix and don't repeat the LLM review comments (the rules still check every round).
- [x] Quota guard for the public app (Groq's free tier allows ~200k tokens/day, ~15–25 plans): an app-wide daily budget of LLM tokens (`LLM_DAILY_TOKEN_BUDGET`, default 150,000) after which every run plans without the LLM until the next day (UTC); at most 3 AI plans per browser session (`LLM_PLANS_PER_SESSION`); when Groq or Gemini answers "limit reached", AI planning pauses at once for everyone instead of each run retrying; the sidebar shows how much of today's allowance is used.
- [x] Demo warm-up: `uv run python -m core.warmup` runs the demo requests once so they are served from the 24 h plan cache during a demo (no LLM calls, nothing can hit a limit live).
- [x] Change requests (`core/runner.py`): "make it cheaper" (a target, default 15% below the current total → Budget), "remove this stop" (→ Assemble, excluded) and "swap this stop" (→ Destination, excluded and replaced). Each is written into the last review as a `user` issue with `update_state`, and the graph resumes from the router.

**Tasks — UI**

- [x] Navigation (`st.navigation`): **Plan a trip**, **Ask the agents**, **Browse Dubai**, **Saved trips**; **Travel data** in dev mode only.
- [x] A **Dev mode** switch: off hides the model, data mode and revision settings, the Review & revisions, Agent timeline, Graph and Metrics tabs, the raw assumptions and the Travel data page; on shows them.
- [x] **Day/night skyline** that follows Streamlit's own light/dark theme (⋮ → Settings; read with `st.context.theme.type`, made instant with CSS where possible): an illustrated Dubai skyline (SVG, drawn for the project, licence-free), the same scene by day and by night, animating between them (sky, sun and moon, window lights); content on translucent cards; text contrast checked in both themes. No custom theme switch: Streamlit's built-in widgets then always match.
- [x] **Plan a trip** — form: start date and number of days, adults, children, budget (AED), hotel type, interests, pace, things to avoid, diet, must-visit places (multi-select from the dataset, pre-filled from Browse Dubai).
- [x] **Ask the agents** — the text request, example request chips ("Family weekend", "Luxury 5 days", "Budget culture trip"), and checks before planning (budget, destination, length) shown under the box.
- [x] **Browse Dubai** — map and filterable list of all places (interest, area, free, indoor, crowd level), each with "Add to my trip".
- [x] **Saved trips** — save a plan in this session, compare saved plans side by side, download a trip as JSON and upload it again (validated as an `Itinerary`).
- [x] **Travel data** *(dev)* — data sources and their licences, freshness per source (staleness report), current data mode and live-source status, cache size and a "clear cache" button.
- [x] Progress view for travellers while planning (understanding the request → finding places → choosing a hotel area → building the days → checking the budget → final review); the agent timeline and live graph stay in dev mode.
- [x] Results: a summary (days, hotel area, total against budget, verdict); one card per day (time, place, how to get there, cost; weather badge; day cost); a map per day with numbered pins matching the cards; "Why this?" with the agents' reasons for each place and the hotel area; change-request buttons; Budget tab.

**Deliverables:** the redesigned app on Streamlit Cloud.

**Status: done** (2026-11-19). Contracts: `TripConstraints.children` and `must_visit`,
`IssueOrigin.USER`; the parser reads children ("2 adults and 2 kids", "with my son") and named
places (`agents/skills/places_match.py`; "avoid X" is not a must-visit; the longest matching
name wins). Destination always keeps must-visit places; the scheduler pins them first (they
take the day's one beach slot, are never dropped for pace) and explains one that can't fit;
`must_visit_missing` is minor with a stated reason (closed that season, over an hour from the
hotel) and major otherwise. Graph: the trip form skips parsing; change requests (`revise()`)
fork the checkpointed run, write a `user` issue with `update_state` and resume from the
router ("cheaper" → Budget only, with a target that is not the budget; "remove" → Assemble;
"swap" → Destination); `finalize` only picks drafts made for the change. LLM protection:
`CachingLLM` (identical questions answered from a 24 h disk cache or the committed
`data/demo_llm_cache.json`), `QuotaGuard` (150k tokens/day for the whole app, kept across
restarts; an instant pause on "limit reached"), 3 AI plans per browser session, LLM review
comments only on passing drafts, day themes rewritten only when a budget fix changes the
days; `uv run python -m core.warmup` fills the demo cache (4 demo plans, all score 100 with
the real models). UI (`app/pages/`, `app/components/`): Plan a trip (form), Ask the agents
(examples, checks before planning), Browse Dubai (map, filters, add to trip), Saved trips
(session, compare, download/upload), Travel data (dev); progress steps for travellers, day
cards with numbered map pins, weather, "Why this?", Change (swap/remove), Make it cheaper,
Save, Download; dev mode adds settings and the Phase 6 views. Skyline: an SVG drawn for the
project that follows Streamlit's light/dark theme (a script reads the text colour and
cross-fades day and night); `st.html` strips SVG and markup-bearing scripts, so the drawing
is inserted base64-encoded by our script and attached to `<body>`. Checked in a real browser
(Playwright + Chrome): both themes, a plan, the Change menu and a removal. Tests: 431 pass
(+45: family/must-visit, LLM protection, change requests, UI pages); the 300-trip property
test passes. Found and fixed on the way: a must-visit beach lost its slot to a better-scoring
beach; "Hatta Heritage Village" also pinned the Shindagha village; "make it cheaper" handed back
the original plan when the target couldn't be fully met; a change request rewrote the
original run (now forked); the dev-mode switch lost its state on some reruns; with the real
LLMs the over-budget demo stayed over budget because the Budget LLM chose small swaps before a
needed hotel downgrade, after which every hotel move broke the transit rule (the engine's tier
decision is now binding; a regression test fails on the old code).

**Exit criteria:**

- A traveller can plan a trip from the form without typing, see the day cards and maps, ask for a change and save or download the result.
- With dev mode off, no technical view is visible; with it on, every Phase 6 view is available.
- Must-visit places appear in the plan, or the plan says why one couldn't be placed; children show in the costs and the assumptions.
- Each change request re-runs only the agents responsible (graph test), and the revised plan passes review.
- Switching Streamlit's theme switches the skyline between day and night, and text stays readable in both.
- A repeated request within 24 h makes no LLM calls; a revision round makes fewer calls than the first round.
- Every earlier test still passes; new page and component tests cover the form, browse, saved trips (download → upload round trip), dev mode and the day/night skyline.

---

## Phase 9 — Hardening, Evaluation & Demo

**Goal:** a stable, explainable demo that meets every success criterion.

**Tasks**

- [ ] Scenario suite (`tests/scenarios/`): the example request, a 1-day low-budget trip, a 7-day luxury trip, a forced over-budget case, an "avoid crowds" case.
- [ ] Optional single-LLM baseline (architecture.md §8): one prompt → `Itinerary`, scored by the same rule checks; show it next to the graph result in the metrics panel.
- [ ] Optional LLM-as-judge rubric for preference fit on a small request set.
- [ ] Performance pass: cache warm-up, cheaper model for parsing, fewer LLM calls on revision rounds.
- [ ] Error-path review: invalid LLM output, all providers down, impossible budgets (e.g. AED 300 for 7 days) → clear messages.
- [ ] Data refresh: run the staleness report; re-verify fares and prices before the demo.
- [ ] Docs: README (setup, run, configuration), a demo script (talk track + requests to type), update architecture.md with any design changes.
- [x] Deploy the Streamlit app to Streamlit Community Cloud — done 2026-11-18 (keys as app secrets; redeploys on every push to `main`).
- [ ] Stretch goals: human approval before `finalize` (`interrupt_before`), destination research as a subgraph.

**Deliverables:** tagged demo release, demo script, passing scenario suite.

**Exit criteria:** all success criteria below are checked off.

---

## Success Criteria Traceability

| Success criterion (problem statement §9) | Delivered in | Verified by |
|------------------------------------------|--------------|-------------|
| Accept natural-language requirements and produce a structured Dubai itinerary | P1, P4, P5 | Parser tests; scenario suite |
| Demonstrate meaningful collaboration among specialized agents | P4, P5, P6 | Graph tests (parallel superstep, targeted revisions); live timeline |
| Generate realistic schedules and estimated budgets using available data | P2, P3, P7 | Scheduler/budget property tests; provenance badges |
| Detect and revise at least one itinerary constraint violation | P3, P5, P8 | Forced over-budget and "avoid crowds" scenarios; traveller change requests |
| Visualize agent execution, shared state, and plan changes across review iterations | P5, P6, P8 | UI timeline, graph view, review diff; traveller progress view and dev mode |

---

## Key Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Curated data collection takes longer than planned | Delays P2 and everything after | Start with the neighborhoods the example request needs; expand later. Heuristic matrix first. |
| LLM returns invalid structured output | Broken runs | Schema-constrained output, one retry with the error, deterministic fallback (P4). |
| Review loop doesn't converge | Long or endless runs | `MAX_REVISIONS`, no-improvement detection, best-draft selection (P5). |
| Public Overpass/OSRM endpoints rate-limit or go down | Flaky demo | Cache, matrix-first logistics, self-hosting option, offline mode as the demo default (P7). |
| No RTA GTFS feed available | Less accurate transit times | Manually curated metro times from the RTA journey planner (research.md §2.4). |
| Prices and fares go stale | Unrealistic budgets | `as_of` on every value, staleness report, `estimated` tags, contingency buffer. |
| Streamlit live updates from a background thread are awkward | Weak visualisation | Queue-based `TraceBus` polled by the UI; fall back to step-by-step replay from checkpoints. |
| LLM cost during development | Budget overrun | Stubbed LLM in tests, cheaper model for parsing, cache LLM responses in dev. |
| The public app spends the owner's free LLM quota (Groq: ~15–25 plans/day) | AI planning stops for the day | Plan cache, fewer calls per plan, an app-wide daily token budget, 3 AI plans per visitor, an instant pause on "limit reached", demo warm-up; planning without the LLM always works (P8). |

---

## Open Decisions to Settle Early

- [x] Default LLM provider and model for the demo — Groq `openai/gpt-oss-120b` for four agents, Gemini for review (2026-11-17). The real-LLM smoke test runs once both keys are set.
- [x] Target travel season for the demo — modelled per month in Phase 2; demo defaults to the cool season (Nov–Mar).
- [x] Self-hosted OSRM vs public demo server / OpenRouteService — public OSRM servers, used for a stored table plus light live use (2026-11-18).
- [x] Whether RTA publishes a GTFS feed — only behind a Dubai Pulse permission request; not used, metro times stay heuristic (2026-11-18).
- [ ] Whether to keep the optional Amadeus test-environment provider at all.
