"""Budget agent: cost the draft, offer cheaper alternatives, apply them when asked.

All arithmetic is the Phase 3 budget engine's; the LLM only chooses *which* alternatives to
apply and phrases the explanation (architecture.md §5.3).

- ``evaluate`` costs a plan and, when it is over budget, lists alternatives with measured
  savings (first pass: nothing is applied yet; Review decides whether a revision is needed).
- ``revise`` applies alternatives until the plan fits. Alternatives that keep the requested
  hotel tier go first; dropping a tier is only used when no same-tier option is left, and the
  reason is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import Field

from agents.base import AgentContext, feedback, to_json
from agents.skills.budget_engine import (
    BudgetOptions,
    Plan,
    PlanInputs,
    generate_alternatives,
    plan_trip,
)
from agents.skills.rule_checks import check_itinerary
from core.tracing import TraceKind
from schemas import (
    AlternativeKind,
    BudgetAlternative,
    BudgetBreakdown,
    ReviewIssue,
    Source,
    TripRequest,
)
from schemas.base import StrictModel

AGENT = "budget"
MAX_STEPS = 6
BUDGET_CODES = {"over_budget", "hotel_cost_driver"}


class BudgetChoice(StrictModel):
    apply_ids: list[str] = Field(description="alternative ids to apply, in order of preference")
    explanation: str = Field(default="", max_length=600)


@dataclass
class BudgetEvaluation:
    budget: BudgetBreakdown
    alternatives: list[BudgetAlternative] = field(default_factory=list)


@dataclass
class BudgetRevision:
    plan: Plan
    applied: list[BudgetAlternative] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def is_downgrade(alt: BudgetAlternative, plan: Plan) -> bool:
    if alt.kind is not AlternativeKind.CHEAPER_HOTEL or not alt.replacement_id:
        return False
    return not alt.replacement_id.endswith("/" + plan.inputs.hotel.tier.value)


@dataclass
class BudgetAgent:
    ctx: AgentContext
    options: BudgetOptions = field(default_factory=BudgetOptions)

    def evaluate(self, plan: Plan, request: TripRequest) -> BudgetEvaluation:
        b = plan.budget
        self.ctx.emit(AGENT, TraceKind.START, "Costing the plan")
        alternatives: list[BudgetAlternative] = []
        if not b.within_budget:
            alternatives = [a for a, _ in self._ordered(plan, request)]
        self.ctx.emit(
            AGENT,
            TraceKind.OUTPUT,
            f"Total AED {b.total:,.0f} of AED {b.budget_aed:,.0f} "
            f"({'within budget' if b.within_budget else 'OVER budget'})"
            + (f"; {len(alternatives)} cheaper alternative(s)" if alternatives else ""),
            {
                "accommodation": b.accommodation,
                "food": b.food,
                "transport": b.transport,
                "activities": b.activities,
                "contingency": b.contingency,
                "verified_aed": b.total_by_source(Source.VERIFIED),
            },
        )
        return BudgetEvaluation(b, alternatives)

    def _ordered(
        self, plan: Plan, request: TripRequest
    ) -> list[tuple[BudgetAlternative, PlanInputs]]:
        """Same-tier alternatives first (biggest saving first), then tier downgrades."""
        options = generate_alternatives(plan, request, self.ctx.providers, self.options)
        return sorted(options, key=lambda o: (is_downgrade(o[0], plan), -o[0].saving_aed, o[0].id))

    def revise(
        self, plan: Plan, request: TripRequest, issues: list[ReviewIssue] | None = None
    ) -> BudgetRevision:
        self.ctx.emit(AGENT, TraceKind.REVISION, "Applying cheaper alternatives")
        applied: list[BudgetAlternative] = []
        notes: list[str] = []
        rejected: set[str] = set()
        current = plan
        for _ in range(MAX_STEPS):
            if current.budget.within_budget:
                break
            options = [o for o in self._ordered(current, request) if o[0].id not in rejected]
            if not options:
                notes.append("No cheaper alternative is left; the plan is still over budget")
                break
            options = self._downgrade_first_if_needed(current, options)
            alt, inputs = self._choose(current, options, issues or [])
            trial = plan_trip(request, inputs, self.ctx.providers, self.options)
            new_problems = self._new_problems(current, trial)
            if new_problems:  # a cheaper plan that breaks another rule is not a fix
                rejected.add(alt.id)
                self.ctx.emit(
                    AGENT,
                    TraceKind.REVISION,
                    f"Skipped: {alt.description} "
                    f"(it would cause {', '.join(sorted(new_problems))})",
                )
                continue
            if is_downgrade(alt, current):
                notes.append(
                    f"Changed to a {inputs.hotel.tier.value} hotel area "
                    f"({inputs.hotel.neighborhood_id}) because no "
                    f"{current.inputs.hotel.tier.value} option fits the budget"
                )
            current = trial
            applied.append(alt.model_copy(update={"applied": True}))
            self.ctx.emit(
                AGENT,
                TraceKind.REVISION,
                f"Applied: {alt.description} (saves AED {alt.saving_aed:,.0f}); "
                f"now AED {current.budget.total:,.0f}",
            )
        return BudgetRevision(current, applied, notes)

    @staticmethod
    def _downgrade_first_if_needed(
        plan: Plan, options: list[tuple[BudgetAlternative, PlanInputs]]
    ) -> list[tuple[BudgetAlternative, PlanInputs]]:
        """If every same-tier saving together can't close the gap, don't spoil the trip with
        small swaps first: go straight to the cheaper tier (still the biggest saving first)."""
        gap = -plan.budget.remaining_aed
        same = [o for o in options if not is_downgrade(o[0], plan)]
        down = [o for o in options if is_downgrade(o[0], plan)]
        if down and sum(a.saving_aed for a, _ in same) < gap:
            return down + same
        return options

    def _new_problems(self, before: Plan, after: Plan) -> set[str]:
        """Blocking rule issues the change would introduce (budget issues don't count)."""

        def codes(p: Plan) -> set[str]:
            return {
                i.code
                for i in check_itinerary(
                    p.itinerary, self.ctx.providers, month=p.inputs.options.month
                )
                if i.blocks_pass and i.code not in BUDGET_CODES
            }

        return codes(after) - codes(before)

    def _choose(
        self,
        plan: Plan,
        options: list[tuple[BudgetAlternative, PlanInputs]],
        issues: list[ReviewIssue],
    ) -> tuple[BudgetAlternative, PlanInputs]:
        """The LLM may pick another alternative, but no tier downgrade while same-tier ones lead."""
        b = plan.budget
        requested = plan.schedule.itinerary.request.constraints.hotel_tier.value
        choice = self.ctx.ask(
            "budget",
            BudgetChoice,
            prompt="budget",
            user=to_json(
                {
                    "total_aed": b.total,
                    "budget_aed": b.budget_aed,
                    "over_by_aed": round(-b.remaining_aed, 2),
                    "hotel_tier_requested": requested,
                    "alternatives": [
                        {
                            "id": a.id,
                            "kind": a.kind.value,
                            "description": a.description,
                            "saving_aed": a.saving_aed,
                            "changes_hotel_tier": is_downgrade(a, plan),
                        }
                        for a, _ in options
                    ],
                }
            ),
            revision_feedback=feedback(issues),
        )
        default = options[0]
        if choice is None:
            return default
        by_id = {a.id: (a, i) for a, i in options}
        # A tier change is allowed only when the engine already put it first (nothing else fits).
        same_tier_left = not is_downgrade(options[0][0], plan)
        for alt_id in choice.apply_ids:
            if alt_id not in by_id:
                continue
            alt, _ = by_id[alt_id]
            if is_downgrade(alt, plan) and same_tier_left:
                continue  # keep the requested tier while we can
            if choice.explanation:
                self.ctx.emit(AGENT, TraceKind.OUTPUT, choice.explanation)
            return by_id[alt_id]
        return default
