"""APScheduler wiring: run the ingest every N minutes."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings, get_settings
from app.ingest import run_ingest

log = logging.getLogger(__name__)

INGEST_JOB_ID = "ingest"

_scheduler: AsyncIOScheduler | None = None


async def _scheduled_ingest() -> None:
    # Reuse the API's lock so a manual POST /ingest/run and the timer can never
    # write concurrently.
    from app.api import _ingest_lock

    if _ingest_lock.locked():
        log.info("scheduler.skipped", extra={"reason": "ingest already running"})
        return
    async with _ingest_lock:
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
        name="ingest all boards",
        max_instances=1,
        coalesce=True,          # a backlog collapses to one run
        misfire_grace_time=300,
        replace_existing=True,
    )
    _scheduler.start()
    log.info(
        "scheduler.started",
        extra={"interval_minutes": settings.ingest_interval_minutes},
    )
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler.stopped")
