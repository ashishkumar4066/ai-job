"""Application settings, loaded from environment / .env.

Secrets never live in code. See `.env.example` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database -----------------------------------------------------------
    # SQLite for local/personal scale. Swapping to Postgres later is a URL
    # change only: the models deliberately avoid dialect-specific column types.
    database_url: str = Field(default=f"sqlite+aiosqlite:///{BACKEND_ROOT / 'jobs.db'}")

    # --- Ingestion ----------------------------------------------------------
    companies_file: Path = Field(default=BACKEND_ROOT / "companies.yaml")
    # Eligibility + currency rules applied between normalize and persist.
    filters_file: Path = Field(default=BACKEND_ROOT / "config" / "filters.yaml")
    ingest_interval_minutes: int = Field(default=60, ge=1)
    run_scheduler: bool = Field(default=True)
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    http_max_retries: int = Field(default=2, ge=0)
    # HTTP 429 gets its own budget: a rate limit means "later", not "broken",
    # so it must not burn the retry allowance reserved for real errors.
    rate_limit_max_retries: int = Field(default=4, ge=0)
    rate_limit_base_delay_seconds: float = Field(default=1.0, gt=0)
    # Hard cap on pages an aggregator sweep will walk, per query. Himalayas
    # reports a `totalCount` that does not match what it will actually page
    # out, so paging stops on an empty page — this bounds a runaway sweep.
    aggregator_max_pages: int = Field(default=25, ge=1)
    # Per-source concurrency cap so a 50-company run does not open 50 sockets.
    ingest_concurrency: int = Field(default=5, ge=1)

    # Safety rail for "closure by disappearance": if a source that previously
    # had open jobs suddenly returns zero, treat it as a suspect fetch and skip
    # the closure sweep rather than closing the entire board. Ashby in
    # particular answers HTTP 200 with `{"jobs": []}` for an unknown slug.
    guard_empty_fetches: bool = Field(default=True)

    # --- Notifications ------------------------------------------------------
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    notifications_enabled: bool = Field(default=True)
    # Cap alerts per run so a first-ever ingest does not fire 1000 messages.
    max_notifications_per_run: int = Field(default=25, ge=0)

    # --- Misc ---------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=False)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
