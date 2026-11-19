# Demo script

A 10-minute walkthrough of the Dubai AI Travel Planner. It shows each success criterion of the
problem statement (§9) once, in an order that tells a story: a traveller plans a trip, then a
developer looks under the hood.

## Before the demo

1. **Refresh the saved AI answers** (once, after any prompt or data change):
   `uv run python -m core.warmup`. All four demos should print `100`. Commit
   `data/demo_llm_cache.json` and push, so the deployed app has them too.
2. **Check the data:** `uv run python -m data.scripts.staleness_report` should say
   `older than 6 months: 0`.
3. **Open the app** (the Streamlit Cloud URL, or `uv run streamlit run app/streamlit_app.py`)
   in a fresh tab. The sidebar should say *3 AI plan(s) left in this session*.
4. **Pick a theme:** ⋮ → Settings → Light or Dark. The skyline follows it (day or night).

The three typed requests below are answered from the saved answers: they come back in about a
second and use no AI quota. Anything else calls the models live (20–40 seconds).

| # | Request | Shows |
| --- | --- | --- |
| A | `Plan a 4-day trip to Dubai with a budget of AED 5,000, excluding flights. I enjoy cultural attractions, local food, and beaches. I prefer affordable hotels and want to avoid crowded places.` | The example from the problem statement |
| B | `4 days in Dubai, mid-range hotel, budget AED 3000, culture and beaches, avoid crowds` | A violation found and fixed by a revision |
| C | `3 days in Dubai for a family of four with two kids, budget AED 6000, culture and beaches, we want to see the Etihad Museum and Kite Beach` | Children and must-visit places |
| D | `7 days in Dubai, AED 300, culture` | An impossible budget, explained (turn the AI off first, see step 6) |

## Talk track

**1. The problem (30 s).** "Planning Dubai means juggling places, opening hours, heat, taxi
times and a budget. One chatbot answer looks fine but often breaks a rule. Here five
specialised agents work together, and a reviewer sends the plan back until it holds up."

**2. Plan from the traveller's side (2 min).** On **Ask the agents**, click the example chip
*Culture & beaches* (request A) and **Plan my trip**.

- The progress list shows the agents' steps.
- Point at the summary line: days, hotel area, total against the budget, review score.
- Open **Day 1**: numbered stops match the pins on the map; taxi and walking legs show minutes
  and AED; badges say whether a price is *verified* or *estimated*.
- Click **Why this?** on a stop: the Destination agent's reason.

**3. Change the plan (1 min).** Click **💸 Make it cheaper**. Only the Budget agent and the
Review run again; the note says how far the plan was cut. Then **Change → Remove this stop** on
any stop: the Orchestrator re-assembles that day without it.

**4. Under the hood (3 min).** Turn on **Dev mode** in the sidebar. Paste request **B** and
plan it.

- While it runs, the agent cards fill in and the **live graph** colours each node: finished in
  green, running in amber. Destination and Logistics run *in parallel*.
- **Review & revisions:** draft 0 was over the AED 3,000 budget (an `over_budget` blocker).
  The router sent it back to the agents responsible only (Logistics and Budget; Destination
  didn't re-run), and draft 1 fits with a cheaper hotel area. The diff shows what changed.
- **Graph:** drag the *Superstep* slider to replay the run from the checkpoints and show the
  shared state at each step.
- **Metrics:** AI calls, tokens, revisions, issues found and resolved, and why the loop stopped.

**5. Family trip (1 min).** Request **C**: children are counted in every cost, family-friendly
places are preferred, and the Etihad Museum and Kite Beach are pinned into the plan. If a
must-visit can't fit (closed that season, too far), the plan says why.

**6. Guardrails (1 min).** In the sidebar choose **Model → No LLM (deterministic)** (it saves
your quota), then type request **D**. A warning appears before planning (the cheapest hotels
alone cost more). After planning, the summary says the budget isn't enough, gives the cheapest
plan's price and suggests a budget. Mention: if Groq or Gemini fails or hits a limit, each agent
falls back to its deterministic path; if a live data source is down, the local dataset takes
over. A run never crashes.

**7. Wrap up (30 s).** Show **Browse Dubai** (every place, filters, *Add to my trip*) and
**Saved trips** (compare plans side by side, download one as a file). "All data is free and
open: curated places, OpenStreetMap, Open-Meteo and OSRM. Nothing is booked, and prices are
estimates."

## If something goes wrong

| Symptom | What to do |
| --- | --- |
| A demo request takes 20+ seconds | Its saved answers are stale: run the warm-up again, or carry on (it still works live). |
| *AI planning is off* in the sidebar | The session's 3 AI plans or the daily token budget are used up. Open a new tab, or present with *No LLM*; the plans are still complete. |
| Groq says "rate limit" | The app pauses AI calls by itself and keeps planning without them. |
| The live app shows "Oh no" | Check the logs under **Manage app** on Streamlit Cloud; a missing secret is the usual cause. |
