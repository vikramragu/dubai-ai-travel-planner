"""Trace events, the in-process ``TraceBus`` and run metrics (architecture.md §8, §11.1).

The runner forwards every event from the graph's ``custom`` stream to the bus; a UI running the
graph in a worker thread reads the bus while the run is in progress.
"""

from __future__ import annotations

import queue
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, NonNegativeFloat, NonNegativeInt, model_validator

from schemas.base import StrictModel


class TraceKind(StrEnum):
    START = "start"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FALLBACK = "fallback"
    OUTPUT = "output"
    REVIEW = "review"
    ROUTE = "route"
    REVISION = "revision"
    END = "end"
    ERROR = "error"


class TraceEvent(StrictModel):
    run_id: str = Field(min_length=1)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agent: str = Field(min_length=1)  # orchestrator | destination | logistics | budget | review
    kind: TraceKind
    summary: str = Field(min_length=1)  # one human-readable line
    payload: dict[str, Any] | None = None  # compact typed output
    revision: NonNegativeInt = 0


class RunMetrics(StrictModel):
    wall_time_s: NonNegativeFloat = 0.0
    llm_calls: NonNegativeInt = 0
    tokens_in: NonNegativeInt = 0
    tokens_out: NonNegativeInt = 0
    est_llm_cost_usd: NonNegativeFloat = 0.0
    revisions: NonNegativeInt = 0
    final_score: float | None = Field(default=None, ge=0, le=100)
    issues_found: NonNegativeInt = 0
    issues_resolved: NonNegativeInt = 0

    @model_validator(mode="after")
    def _resolved_le_found(self) -> Self:
        if self.issues_resolved > self.issues_found:
            raise ValueError("issues_resolved cannot exceed issues_found")
        return self


class TraceBus:
    """A thread-safe queue of trace events. ``close()`` ends iteration for readers."""

    _CLOSED = object()

    def __init__(self) -> None:
        self._queue: queue.Queue[object] = queue.Queue()
        self.closed = False

    def publish(self, event: TraceEvent) -> None:
        self._queue.put(event)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put(self._CLOSED)

    def drain(self) -> list[TraceEvent]:
        """Every event waiting now, without blocking (for polling UIs)."""
        out: list[TraceEvent] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return out
            if item is self._CLOSED:
                self._queue.put(item)  # keep the end marker for blocking readers
                return out
            assert isinstance(item, TraceEvent)
            out.append(item)

    def __iter__(self) -> Iterator[TraceEvent]:
        """Block for events until the bus is closed."""
        while True:
            item = self._queue.get()
            if item is self._CLOSED:
                self._queue.put(item)
                return
            assert isinstance(item, TraceEvent)
            yield item
