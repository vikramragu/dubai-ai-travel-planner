# Dubai AI Travel Planner

A LangGraph multi-agent system that turns a natural-language travel request into a
personalised, budget-aware and validated Dubai itinerary.

- Problem statement: [docs/problemStatement.md](docs/problemStatement.md)
- Architecture: [docs/architecture.md](docs/architecture.md)
- Data sources (free and open only): [docs/research.md](docs/research.md)
- Build plan: [docs/implementation-plan.md](docs/implementation-plan.md)

> **Status:** Phases 0–6 complete: project setup, shared Pydantic contracts, the curated
> Dubai dataset with local providers and tools, the deterministic skills (scheduler, budget
> engine, rule checks), the five agents, the LangGraph graph that runs them (parallel
> research, review loop, checkpoints, streaming) and the Streamlit UI. Free live data
> (Phase 7) comes next. Without API keys the agents run in deterministic mode.

- Export the JSON schemas (useful for prompts): `uv run python -m schemas.export build/schemas`
- Run the graph on a request: `uv run python -m core.runner "4 days in Dubai, AED 5,000, culture"`
  (add `--no-llm` to force deterministic mode; with API keys in `.env` the LLMs are used)
- Export the graph diagram: `uv run python -m graph.build` (writes [docs/graph.mmd](docs/graph.mmd))
- Plan the example request without an LLM: `uv run python -m agents.skills.scripted`
  (try `--tier mid --budget 3000` to see the over-budget fix, or `--no-fit`)
- Build the local database: `uv run python -m data.build_db` (also built automatically on
  first use). The dataset, its sources and refresh commands: [data/README.md](data/README.md)

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync                      # create .venv and install app + dev dependencies
cp .env.example .env         # then set GROQ_API_KEY and GOOGLE_API_KEY
```

Optional LLM providers: `uv sync --extra openai` or `uv sync --extra ollama`.

## Run

```bash
uv run streamlit run app/streamlit_app.py
```

Type a request (the example is pre-filled), choose "No LLM" in the sidebar to run offline, and
press **Plan my trip**. While it runs you see each agent's status and the active graph node;
afterwards: itinerary with maps, budget, review rounds with the draft-to-draft diff, agent
timeline, a checkpoint replay of the graph, and run metrics. Prices are estimates and nothing
is booked.

## Develop

```bash
uv run pytest                # tests
uv run pytest --cov          # tests with coverage
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # type check (strict)
uv run pre-commit install    # git hooks (needs a git repository)
```

## Configuration

All settings live in `core/config.py` and are read from environment variables or `.env`.
See [.env.example](.env.example) for the full list. Key options:

| Variable | Default | Meaning |
|----------|---------|---------|
| `LLM_PROVIDER` / `LLM_MODEL` | `groq` / `openai/gpt-oss-120b` | Model for the orchestrator, destination, logistics and budget agents |
| `LLM_PROVIDER_REVIEW` / `LLM_MODEL_REVIEW` | `google_genai` / `gemini-flash-latest` | Model for the review agent |
| `LLM_PROVIDER_<ROLE>` / `LLM_MODEL_<ROLE>` | — | Per-agent override (`ORCHESTRATOR`, `DESTINATION`, `LOGISTICS`, `BUDGET`, `REVIEW`) |
| `GROQ_API_KEY` / `GOOGLE_API_KEY` | — | Keys for Groq and Gemini (`GEMINI_API_KEY` also works) |
| `DATA_MODE` | `local` | `local` (offline curated data), `hybrid` or `live` (free live sources + fallback) |
| `MAX_REVISIONS` | `3` | Review-loop cap |
| `CONTINGENCY_PCT` | `10` | Budget buffer for estimated prices |

## Project layout

```
app/        Streamlit UI
core/       config, LLM factory, runner, tracing
schemas/    shared Pydantic contracts
agents/     agent logic, deterministic skills, prompts
tools/      typed tool functions over data providers
data/       curated seed data, SQLite build, providers
graph/      LangGraph state, nodes, routing, graph build
tests/      unit, graph and scenario tests
docs/       project documentation
```
