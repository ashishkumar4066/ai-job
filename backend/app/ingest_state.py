"""Live state for the in-flight ingest run.

The dashboard blocks its first paint on a fresh sweep (see `POST /ingest/refresh`),
which the synchronous `POST /ingest/run` cannot serve: a 90-second HTTP request
dies to the first proxy timeout, gives the UI nothing to render while it waits,
and strands a reloaded tab with a 409. So a refresh starts the run as a
background task and the UI polls this tracker for per-source progress instead.

One tracker per process. The lock it owns is the same one the scheduler and the
manual trigger take, so a background run, a timer tick and a manual POST can
never write concurrently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.models import utcnow
from app.schemas import CompanyConfig, IngestRunOut, SourceResultOut

log = logging.getLogger(__name__)

# `pending` -> queued behind the concurrency semaphore.
# `fetching` -> its board is being contacted right now.
# Terminal: `done`, `failed`, `throttled` (skipped, min interval not elapsed).
SourceState = Literal["pending", "fetching", "done", "failed", "throttled"]

_TERMINAL: frozenset[str] = frozenset({"done", "failed", "throttled"})


@dataclass
class SourceProgress:
    """How one board is doing in the current run."""

    source_id: str
    company: str
    ats: str
    state: SourceState = "pending"
    fetched: int = 0
    new: int = 0
    eligible: int = 0
    error: str | None = None

    @property
    def settled(self) -> bool:
        return self.state in _TERMINAL


class RunProgress:
    """Mutable, in-memory progress for one run.

    Deliberately not persisted: it exists only to answer "what is happening
    right now" while a run is open. `ingest_runs` remains the durable record.
    """

    def __init__(self) -> None:
        self.run_id: int | None = None
        self.started_at: datetime = utcnow()
        self.finished_at: datetime | None = None
        self.error: str | None = None
        # Insertion-ordered, so the UI lists boards in config order.
        self._sources: dict[str, SourceProgress] = {}

    def begin(self, companies: Iterable[CompanyConfig]) -> None:
        self._sources = {
            company.source_id: SourceProgress(
                source_id=company.source_id, company=company.company, ats=company.ats
            )
            for company in companies
        }

    def mark(self, source_id: str, state: SourceState) -> None:
        entry = self._sources.get(source_id)
        if entry is not None:
            entry.state = state

    def apply(self, result: SourceResultOut) -> None:
        """Fold a finished source's counts in, deriving its terminal state."""
        entry = self._sources.get(result.source_id)
        if entry is None:
            entry = SourceProgress(
                source_id=result.source_id, company=result.company, ats=result.ats
            )
            self._sources[result.source_id] = entry
        entry.fetched = result.fetched
        entry.new = result.new
        entry.eligible = result.eligible
        entry.error = result.error
        if result.throttled:
            entry.state = "throttled"
        else:
            entry.state = "done" if result.ok else "failed"

    @property
    def sources(self) -> list[SourceProgress]:
        return list(self._sources.values())

    @property
    def total(self) -> int:
        return len(self._sources)

    @property
    def done(self) -> int:
        return sum(1 for entry in self._sources.values() if entry.settled)


class IngestTracker:
    """Owns the ingest lock and whatever run is currently using it."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._progress: RunProgress | None = None
        self._result: IngestRunOut | None = None
        self._error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def busy(self) -> bool:
        """True when *anything* holds the lock — background run, timer or manual."""
        return self.running or self.lock.locked()

    @property
    def progress(self) -> RunProgress | None:
        return self._progress

    @property
    def result(self) -> IngestRunOut | None:
        return self._result

    @property
    def error(self) -> str | None:
        return self._error

    def start(
        self, runner: Callable[[RunProgress], Awaitable[IngestRunOut]]
    ) -> RunProgress:
        """Launch a background run. The caller must have checked `running` first.

        Safe against a double-start from two near-simultaneous requests: this
        method never awaits, so the `running` check and the task creation happen
        in one uninterrupted event-loop turn.
        """
        progress = RunProgress()
        self._progress = progress
        self._result = None
        self._error = None
        self._task = asyncio.create_task(self._drive(runner, progress))
        return progress

    async def _drive(
        self, runner: Callable[[RunProgress], Awaitable[IngestRunOut]], progress: RunProgress
    ) -> None:
        try:
            # Queue behind the scheduler or a manual run rather than racing it.
            async with self.lock:
                self._result = await runner(progress)
        except asyncio.CancelledError:
            self._error = "cancelled"
            raise
        except Exception as exc:
            # A background failure has no request to surface on, so it is parked
            # here for the next /ingest/status poll to report.
            self._error = f"{type(exc).__name__}: {exc}"
            progress.error = self._error
            log.exception("ingest.background_failed")
        finally:
            progress.finished_at = utcnow()

    async def cancel(self) -> None:
        """Stop an in-flight run (shutdown only) and wait for it to unwind."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown path
            pass


tracker = IngestTracker()

__all__ = ["IngestTracker", "RunProgress", "SourceProgress", "SourceState", "tracker"]
