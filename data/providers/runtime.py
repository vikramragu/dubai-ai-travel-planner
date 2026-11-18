"""Per-run context for the data layer: where data events go and how many live calls are left.

Providers are shared across runs (and threads, in the UI), so per-run state can't live on them.
It lives in a :class:`contextvars.ContextVar` instead:

- the runner opens a run with :func:`data_run` (a fresh call budget);
- each graph node narrows it with :func:`node_events`, so the data events raised while that
  node runs (network calls, fallbacks) land in that node's trace, even when two nodes run in
  parallel threads;
- without a run (scripts, tests) data events are dropped and there is no budget.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from core.tracing import TraceKind

EventSink = Callable[[TraceKind, str, dict[str, Any] | None], None]


@dataclass
class CallBudget:
    """Network calls allowed in one run (``None`` = unlimited). Thread-safe."""

    limit: int | None = None
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def take(self) -> bool:
        with self._lock:
            if self.limit is not None and self.used >= self.limit:
                return False
            self.used += 1
            return True


@dataclass(frozen=True)
class DataRun:
    budget: CallBudget
    sink: EventSink | None = None


_RUN: ContextVar[DataRun | None] = ContextVar("data_run", default=None)


def current_run() -> DataRun | None:
    return _RUN.get()


def data_event(kind: TraceKind, summary: str, payload: dict[str, Any] | None = None) -> None:
    """Record a data-layer event in the current node's trace (no-op outside a run)."""
    run = _RUN.get()
    if run is not None and run.sink is not None:
        run.sink(kind, summary, payload)


@contextmanager
def data_run(budget: int | None = None) -> Iterator[DataRun]:
    """A run's data context: a fresh call budget shared by every node of the run."""
    token = _RUN.set(DataRun(CallBudget(budget)))
    try:
        yield _RUN.get()  # type: ignore[misc]
    finally:
        _RUN.reset(token)


@contextmanager
def node_events(sink: EventSink) -> Iterator[None]:
    """Send data events raised inside this block to ``sink`` (keeps the run's budget)."""
    run = _RUN.get()
    token = _RUN.set(DataRun(run.budget if run else CallBudget(None), sink))
    try:
        yield
    finally:
        _RUN.reset(token)


def set_node_sink(sink: EventSink) -> None:
    """Like :func:`node_events` for code that can't use a ``with`` block (a graph node sets it
    at its start). The runner's :func:`data_run` resets everything when the run ends."""
    run = _RUN.get()
    _RUN.set(DataRun(run.budget if run else CallBudget(None), sink))
