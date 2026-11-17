"""What every agent shares: the run context, trace events, prompts and a safe LLM call.

Each agent is a small class with one typed entry point. LLM output is always validated, and
any failure (no API key, provider error, invalid output after the retry) is logged as an
``error`` trace event and replaced by the agent's deterministic fallback, so a run never fails
because of the LLM (architecture.md §13).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, TypeVar

from pydantic import BaseModel, BeforeValidator

from core.config import AgentRole, Settings, get_settings
from core.llm import LLMClient, LLMError
from core.tracing import TraceEvent, TraceKind
from data.providers import DataProviders, get_providers
from schemas import ReviewIssue

M = TypeVar("M", bound=BaseModel)
PROMPTS = Path(__file__).resolve().parent / "prompts"


def ClipTo(limit: int) -> BeforeValidator:  # noqa: N802 - reads like a type marker
    """For LLM-written text, ``Annotated[str, ClipTo(300)]``: longer text is cut at a word
    boundary instead of failing validation (models don't reliably respect length limits, and a
    long sentence is no reason to throw away an otherwise valid answer)."""

    def clip(value: Any) -> Any:
        if not isinstance(value, str) or len(value) <= limit:
            return value
        return value[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"

    return BeforeValidator(clip)


def load_prompt(name: str, **values: Any) -> str:
    """``agents/prompts/<name>.md`` with ``$placeholders`` filled in."""
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    return Template(text).substitute({k: str(v) for k, v in values.items()})


def to_json(data: Any) -> str:
    """Compact JSON for prompts (no indentation: whitespace costs tokens, and Groq's free tier
    allows 8,000 tokens per minute)."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def feedback(issues: list[ReviewIssue]) -> str:
    """The ``revision_feedback`` section of a prompt."""
    if not issues:
        return "None: this is the first draft."
    return "\n".join(
        f"- [{i.severity.value}] {i.code}: {i.message}"
        + (f" (place: {i.place_id})" if i.place_id else "")
        for i in issues
    )


@dataclass
class AgentContext:
    providers: DataProviders = field(default_factory=get_providers)
    llm: LLMClient | None = None
    settings: Settings = field(default_factory=get_settings)
    run_id: str = "run"
    revision: int = 0
    events: list[TraceEvent] = field(default_factory=list)
    on_event: Callable[[TraceEvent], None] | None = None
    node: str | None = None  # graph node running the agent; added to every event's payload

    def emit(
        self,
        agent: str,
        kind: TraceKind,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> TraceEvent:
        if self.node is not None:
            payload = {**(payload or {}), "node": self.node}
        event = TraceEvent(
            run_id=self.run_id,
            agent=agent,
            kind=kind,
            summary=summary[:500],
            payload=payload,
            revision=self.revision,
        )
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)
        return event

    def ask(
        self,
        role: AgentRole,
        schema: type[M],
        *,
        prompt: str,
        user: str,
        temperature: float | None = None,
        **values: Any,
    ) -> M | None:
        """Structured LLM answer, or ``None`` (and an ``error`` event) when unavailable."""
        if self.llm is None:
            return None
        try:
            return self.llm.structured(
                role,
                schema,
                system=load_prompt(prompt, **values),
                user=user,
                temperature=temperature,
            )
        except LLMError as exc:
            self.emit(
                role, TraceKind.ERROR, f"LLM unavailable, using deterministic fallback: {exc}"
            )
            return None
