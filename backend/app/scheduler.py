"""APScheduler wiring: check every N minutes, sweep at most once a window.

The timer is a *check*, not a sweep. It fires often (hourly by default) but
runs the ingest only when `refresh_window_hours` has elapsed since the last
finished run — the same rolling window `POST /ingest/refresh` uses, and read
from the same place, so the scheduler and the dashboard can never double-sweep
each other. Checking more often than the window is what makes a failed daily
sweep retry within the hour instead of waiting another full day.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings, get_settings
from app.db import session_scope
from app.ingest import is_fresh, run_ingest
from app.ingest_state import tracker

log = logging.getLogger(__name__)

INGEST_JOB_ID = "ingest"

_scheduler: AsyncIOScheduler | None = None


async def _scheduled_ingest() -> None:
    # Share the tracker's lock so a manual POST /ingest/run, a dashboard
    # refresh and the timer can never write concurrently.
    if tracker.busy:
        log.info("scheduler.skipped", extra={"reason": "ingest already running"})
        return

    async with session_scope() as session:
        last_finished = await is_fresh(session)
    if last_finished is not None:
        # A dashboard visit or an earlier tick already swept inside the window.
        log.info(
            "scheduler.skipped",
            extra={"reason": "data is fresh", "last_finished": last_finished.isoformat()},
        )
        return

    async with tracker.lock:
        try:
            await run_ingest()
        except Exception:
            # A scheduled failure must not kill the scheduler thread.
            log.exception("scheduler.ingest_failed")


def start_scheduler(settings: Settings | None = None) -> AsyncIOScheduler | None:
    global _scheduler
    settings = settings or get_settings()

    if not settings.run_scheduler:
        log.info("scheduler.disabled")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _scheduled_ingest,
        trigger=IntervalTrigger(minutes=settings.ingest_interval_minutes),
        id=INGEST_JOB_ID,
        name="ingest all boards if the freshness window has lapsed",
        max_instances=1,
        coalesce=True,          # a backlog collapses to one run
        misfire_grace_time=300,
        replace_existing=True,
    )
    _scheduler.start()
    log.info(
        "scheduler.started",
        extra={
            "check_interval_minutes": settings.ingest_interval_minutes,
            "refresh_window_hours": settings.refresh_window_hours,
        },
    )
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler.stopped")
