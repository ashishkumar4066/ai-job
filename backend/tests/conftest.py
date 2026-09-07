"""Shared fixtures.

Every test runs against a throwaway SQLite file and never touches the network:
adapters are exercised through recorded JSON fixtures served by respx.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.db import dispose_engine, get_engine, get_session_factory
from app.eligibility import get_filters
from app.models import Base
from app.notify import NullNotifier
from app.schemas import CompanyConfig

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Fresh SQLite database + isolated settings for one test."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("RUN_SCHEDULER", "false")
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("COMPANIES_FILE", str(FIXTURE_DIR / "companies_test.yaml"))
    monkeypatch.setenv("FILTERS_FILE", str(FIXTURE_DIR / "filters_test.yaml"))
    monkeypatch.setenv("HTTP_MAX_RETRIES", "0")
    monkeypatch.setenv("RATE_LIMIT_MAX_RETRIES", "0")

    get_settings.cache_clear()
    get_filters.cache_clear()
    await dispose_engine()

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await dispose_engine()
    get_settings.cache_clear()
    get_filters.cache_clear()


@pytest.fixture
def session_factory(db: None):  # noqa: ARG001 - ordering dependency
    return get_session_factory()


@pytest.fixture
def recording_notifier() -> NullNotifier:
    """Captures the jobs a run would have alerted on, without sending."""
    return NullNotifier("test")


@pytest.fixture
def greenhouse_company() -> CompanyConfig:
    return CompanyConfig(company="Stripe", ats="greenhouse", token_or_slug="stripe")


@pytest.fixture
def lever_company() -> CompanyConfig:
    return CompanyConfig(company="Palantir", ats="lever", token_or_slug="palantir")


@pytest.fixture
def ashby_company() -> CompanyConfig:
    return CompanyConfig(company="Linear", ats="ashby", token_or_slug="linear")


@pytest.fixture(autouse=True)
async def _reset_ingest_tracker() -> AsyncIterator[None]:
    """The tracker is a process-wide singleton; stop one test's run leaking
    'a sweep is already in progress' into the next."""
    from app.ingest_state import tracker

    yield
    await tracker.cancel()
    tracker._task = None
    tracker._progress = None
    tracker._result = None
    tracker._error = None


@pytest.fixture(autouse=True)
def _reset_adapter_registry() -> Iterator[None]:
    """Undo any test-local adapter registrations."""
    from app import adapters

    original = dict(adapters._REGISTRY)
    yield
    adapters._REGISTRY.clear()
    adapters._REGISTRY.update(original)
