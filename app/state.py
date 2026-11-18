"""Session-state helpers: run the graph in a worker thread and collect its live events.

Streamlit reruns the script on every interaction, so a run lives in ``st.session_state`` as a
:class:`RunHandle`. The worker thread only calls :func:`core.runner.run`; it never touches
Streamlit. The script polls the handle, which drains the :class:`TraceBus`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from core.config import DataMode, Settings
from core.llm import LLMClient
from core.runner import AUTO, RunResult, run
from core.tracing import TraceBus, TraceEvent
from data.providers import DataProviders


@dataclass
class RunOptions:
    text: str
    use_llm: bool = True
    max_revisions: int = 3
    data_mode: DataMode = DataMode.LOCAL


@dataclass
class RunHandle:
    options: RunOptions
    bus: TraceBus = field(default_factory=TraceBus)
    events: list[TraceEvent] = field(default_factory=list)
    result: RunResult | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None

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

    def work() -> None:
        try:
            handle.result = run(
                options.text,
                providers=providers,
                llm=client,
                settings=settings,
                bus=handle.bus,
                max_revisions=options.max_revisions,
            )
        except BaseException as exc:  # shown in the UI; the thread must not die silently
            handle.error = exc

    handle.thread = threading.Thread(target=work, name="planner-run", daemon=True)
    handle.thread.start()
    return handle
