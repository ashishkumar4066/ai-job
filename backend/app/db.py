"""Async engine / session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _connect_args(url: str) -> dict[str, object]:
    if url.startswith("sqlite"):
        # The scheduler thread and the request handlers share one connection pool.
        return {"check_same_thread": False}
    return {}


def _tune_sqlite(dbapi_connection: object, _record: object) -> None:
    """Per-connection pragmas that keep the dashboard responsive.

    WAL lets reads proceed while a sweep, validity pass or match run is
    writing; in the default rollback journal every dashboard request queued
    behind the writer, which is why `/meta/facets` swung between 0.4s and
    3.7s. `busy_timeout` turns the rare remaining lock into a short wait
    instead of an error. The page cache matters because `job_postings` rows
    carry ~20 KB of JD text each, and the list columns sit behind it.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA cache_size=-65536")  # 64 MB
        cursor.execute("PRAGMA temp_store=MEMORY")
    finally:
        cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            future=True,
            connect_args=_connect_args(settings.database_url),
        )
        if settings.database_url.startswith("sqlite"):
            event.listen(_engine.sync_engine, "connect", _tune_sqlite)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session: commit on success, roll back on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency (read paths; commits are explicit in write paths)."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
