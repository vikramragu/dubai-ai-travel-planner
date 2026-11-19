"""Session-state helpers: runs in worker threads, the must-visit list, saved trips, AI plan count.

Streamlit reruns the script on every interaction, so a run lives in ``st.session_state`` as a
:class:`RunHandle`. The worker thread only calls :func:`core.runner.run` (or
:func:`core.runner.revise` for a change request); it never touches Streamlit. The script polls
the handle, which drains the :class:`TraceBus`.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.config import DataMode, Settings
from core.llm import LLMClient
from core.runner import AUTO, ChangeRequest, RunResult, revise, run
from core.tracing import TraceBus, TraceEvent
from data.providers import DataProviders
from schemas import Itinerary, TripRequest


@dataclass
class RunOptions:
    text: str
    use_llm: bool = True
    max_revisions: int = 3
    data_mode: DataMode = DataMode.LOCAL
    request: TripRequest | None = None  # set by the trip form: no parsing


@dataclass
class RunHandle:
    options: RunOptions
    bus: TraceBus = field(default_factory=TraceBus)
    events: list[TraceEvent] = field(default_factory=list)
    result: RunResult | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None
    change: ChangeRequest | None = None  # set when this run is a change to an earlier plan

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def finished(self) -> bool:
        return self.thread is not None and not self.thread.is_alive()

    def pump(self) -> list[TraceEvent]:
        """Move the events waiting on the bus into ``events``; returns the new ones."""
        new = self.bus.drain()
        self.events += new
        return new

    def wait(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)
        self.pump()


def _start(handle: RunHandle, work: Callable[[], RunResult]) -> RunHandle:
    def target() -> None:
        try:
            handle.result = work()
        except BaseException as exc:  # shown in the UI; the thread must not die silently
            handle.error = exc

    handle.thread = threading.Thread(target=target, name="planner-run", daemon=True)
    handle.thread.start()
    return handle


def start_run(
    options: RunOptions,
    *,
    providers: DataProviders,
    settings: Settings,
    llm: LLMClient | str | None = AUTO,
) -> RunHandle:
    """Start planning in a background thread and return immediately."""
    handle = RunHandle(options)
    client = llm if options.use_llm else None
    return _start(
        handle,
        lambda: run(
            options.text,
            providers=providers,
            llm=client,
            settings=settings,
            bus=handle.bus,
            max_revisions=options.max_revisions,
            request=options.request,
        ),
    )


def start_change(
    previous: RunHandle,
    change: ChangeRequest,
    *,
    providers: DataProviders,
    settings: Settings,
    use_llm: bool | None = None,
    llm: LLMClient | str | None = AUTO,
) -> RunHandle:
    """Apply a traveller's change to a finished plan, in a background thread. ``use_llm``
    defaults to the original run's choice (the caller passes the visitor's current allowance)."""
    assert previous.result is not None
    use = previous.options.use_llm if use_llm is None else use_llm
    options = RunOptions(**{**previous.options.__dict__, "use_llm": use})
    handle = RunHandle(options, change=change)
    client = llm if use else None
    result = previous.result
    return _start(
        handle,
        lambda: revise(
            result, change, providers=providers, llm=client, settings=settings, bus=handle.bus
        ),
    )


# ------------------------------------------------------------------ saved trips
@dataclass
class SavedTrip:
    """A plan kept for this browser session (and downloadable as JSON)."""

    id: str
    name: str
    itinerary: Itinerary
    saved_at: datetime
    result: RunResult | None = None  # None for an uploaded file (no graph to change it with)


def make_saved(itinerary: Itinerary, result: RunResult | None = None) -> SavedTrip:
    c = itinerary.request.constraints
    when = f" from {itinerary.days[0].date:%d %b}" if itinerary.days[0].date else ""
    total = f", AED {itinerary.budget.total:,.0f}" if itinerary.budget else ""
    name = f"{c.days} days{when} · {itinerary.hotel.neighborhood_id}{total}"
    return SavedTrip(uuid.uuid4().hex[:8], name, itinerary, datetime.now(UTC), result)


def itinerary_json(itinerary: Itinerary) -> str:
    return itinerary.model_dump_json(indent=1)


def load_itinerary(raw: bytes | str) -> Itinerary:
    """Parse a downloaded trip file (raises ``ValueError`` with a readable message)."""
    try:
        return Itinerary.model_validate_json(raw)
    except Exception as exc:  # pydantic's ValidationError is a ValueError subclass
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ValueError(f"This isn't a trip file from this planner ({first})") from exc


# ------------------------------------------------------------------ session keys
def session(st_state: Any) -> dict[str, Any]:
    """Create the session keys this app uses (idempotent)."""
    st_state.setdefault("must_visit", [])
    st_state.setdefault("saved", [])
    st_state.setdefault("ai_plans", 0)
    st_state.setdefault("runs", {})  # page key -> RunHandle
    return st_state  # type: ignore[no-any-return]
