"""Review agent: deterministic rule checks first, then the LLM's qualitative comments.

The rules are authoritative (architecture.md §5.4). LLM comments are always ``minor`` with
``origin="llm"``: they are shown and lower the score, but they never fail the review or trigger
a revision, so the loop only runs on objective problems (decision of 2026-11-15).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pydantic import Field

from agents.base import AgentContext, to_json
from agents.skills.rule_checks import check_itinerary, score
from core.tracing import TraceKind
from schemas import (
    IssueOrigin,
    Itinerary,
    ReviewIssue,
    ReviewReport,
    RouteTarget,
    Severity,
    SlotKind,
)
from schemas.base import StrictModel

AGENT = "review"
MAX_LLM_COMMENTS = 5


class LLMComment(StrictModel):
    message: str = Field(min_length=3, max_length=300)
    day: int | None = Field(default=None, ge=1, le=7)
    place_id: str | None = None
    route_to: RouteTarget = RouteTarget.ASSEMBLE


class LLMReview(StrictModel):
    comments: list[LLMComment] = Field(default_factory=list)
    summary: str = Field(default="", max_length=400)


@dataclass
class ReviewAgent:
    ctx: AgentContext

    def review(self, itinerary: Itinerary, *, month: int | None = None) -> ReviewReport:
        self.ctx.emit(AGENT, TraceKind.START, "Checking the plan")
        issues = check_itinerary(itinerary, self.ctx.providers, month=month)
        answer = self.ctx.ask(
            "review",
            LLMReview,
            prompt="review",
            user=to_json(
                {
                    "request": itinerary.request.constraints.model_dump(mode="json"),
                    "rule_issues": [f"{i.severity.value}: {i.message}" for i in issues],
                    "days": [
                        {
                            "day": d.day,
                            "theme": d.theme,
                            "slots": [
                                f"{s.start:%H:%M}-{s.end:%H:%M} {s.kind.value}: {s.title}"
                                for s in d.slots
                                if s.kind is not SlotKind.TRANSIT
                            ],
                        }
                        for d in itinerary.days
                    ],
                    "budget": None
                    if itinerary.budget is None
                    else {
                        "total_aed": itinerary.budget.total,
                        "budget_aed": itinerary.budget.budget_aed,
                    },
                }
            ),
        )
        llm_summary = ""
        if answer is not None:
            known_days = {d.day for d in itinerary.days}
            for comment in answer.comments[:MAX_LLM_COMMENTS]:
                issues.append(
                    ReviewIssue(
                        code="llm_suggestion",
                        severity=Severity.MINOR,  # never blocks or triggers a revision
                        message=comment.message,
                        route_to=comment.route_to,
                        origin=IssueOrigin.LLM,
                        day=comment.day if comment.day in known_days else None,
                        place_id=comment.place_id,
                    )
                )
            llm_summary = answer.summary
        counts = Counter(i.severity.value for i in issues)
        rule_summary = (
            ", ".join(f"{counts[s.value]} {s.value}" for s in Severity if counts[s.value])
            or "no issues"
        )
        report = ReviewReport.from_issues(
            issues,
            score=score(issues),
            summary=f"Rule checks: {rule_summary}." + (f" {llm_summary}" if llm_summary else ""),
        )
        self.ctx.emit(
            AGENT,
            TraceKind.REVIEW,
            f"{'Passed' if report.passed else 'Needs revision'} (score {report.score:.0f}): "
            + (
                "; ".join(
                    f"{i.severity.value} {i.code} -> {i.route_to.value}"
                    for i in issues
                    if i.blocks_pass
                )
                or rule_summary
            ),
            {"issues": [i.model_dump(mode="json") for i in issues]},
        )
        return report
