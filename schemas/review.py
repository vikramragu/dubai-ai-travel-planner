"""Review issues and the review report (architecture.md §5.4)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, PositiveInt, model_validator

from schemas.base import StrictModel


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"


class RouteTarget(StrEnum):
    """Graph node that must act on an issue."""

    DESTINATION = "destination"
    LOGISTICS = "logistics"
    ASSEMBLE = "assemble"
    BUDGET = "budget"


class IssueOrigin(StrEnum):
    RULE = "rule"  # deterministic rule check — authoritative
    LLM = "llm"  # qualitative reviewer comment
    USER = "user"  # a change the traveller asked for ("make it cheaper", "remove this stop")


class ReviewIssue(StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")  # e.g. "over_budget", "crowded_place"
    severity: Severity
    message: str = Field(min_length=1)
    route_to: RouteTarget
    origin: IssueOrigin = IssueOrigin.RULE
    day: PositiveInt | None = None
    place_id: str | None = None

    @property
    def blocks_pass(self) -> bool:
        return self.severity is not Severity.MINOR


class ReviewReport(StrictModel):
    """``passed`` is true only when there are no blocker or major issues."""

    issues: list[ReviewIssue] = Field(default_factory=list)
    score: float = Field(ge=0, le=100)
    passed: bool
    summary: str = ""

    @classmethod
    def from_issues(
        cls, issues: list[ReviewIssue], score: float, summary: str = ""
    ) -> ReviewReport:
        return cls(
            issues=issues,
            score=score,
            passed=not any(i.blocks_pass for i in issues),
            summary=summary,
        )

    @model_validator(mode="after")
    def _passed_matches_issues(self) -> Self:
        if self.passed == any(i.blocks_pass for i in self.issues):
            raise ValueError("passed must be true exactly when no blocker/major issues remain")
        return self

    @property
    def revision_targets(self) -> set[RouteTarget]:
        return {i.route_to for i in self.issues if i.blocks_pass}

    def by_severity(self, severity: Severity) -> list[ReviewIssue]:
        return [i for i in self.issues if i.severity is severity]
