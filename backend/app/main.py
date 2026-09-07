"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import router
from app.config import get_settings
from app.db import dispose_engine, get_engine
from app.ingest_state import tracker
from app.logging_config import configure_logging
from app.models import Base
from app.scheduler import shutdown_scheduler, start_scheduler

log = logging.getLogger(__name__)


async def init_db() -> None:
    """Create tables if absent.

    Alembic owns the schema (`alembic upgrade head`); this is a convenience so
    a fresh checkout boots without a migration step.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    await init_db()
    start_scheduler(settings)
    log.info(
        "app.started",
        extra={
            "db": settings.database_url.split("///")[-1],
            "scheduler": settings.run_scheduler,
            "telegram": settings.telegram_configured,
        },
    )
    try:
        yield
    finally:
        shutdown_scheduler()
        # A dashboard-triggered sweep runs detached from any request, so stop it
        # before the engine goes away or it writes into a disposed pool.
        await tracker.cancel()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Personal Job Aggregator",
        description="Phase 1 — aggregates live postings from Greenhouse, Lever and Ashby.",
        version="0.1.0",
        lifespan=lifespan,
    )
    # The Phase 2 dashboard is served from a Vite dev server on another port.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
