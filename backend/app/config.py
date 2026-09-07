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
    # How often the scheduler *checks* whether a sweep is due. A check is one
    # SQL query; it only sweeps when `refresh_window_hours` has elapsed. Keeping
    # this well under the window is deliberate: a failed daily sweep retries on
    # the next check instead of waiting another full day.
    ingest_interval_minutes: int = Field(default=60, ge=1)
    # The rolling freshness window. Boards are contacted at most once per this
    # many hours; every other page load renders from storage. Rolling rather
    # than a calendar-day boundary, which would call a 23:00 sweep stale at
    # 00:05. The manual refresh button bypasses it via `force=true`.
    refresh_window_hours: int = Field(default=24, ge=1)
    run_scheduler: bool = Field(default=True)
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    http_max_retries: int = Field(default=2, ge=0)
    # HTTP 429 gets its own budget: a rate limit means "later", not "broken",
    # so it must not burn the retry allowance reserved for real errors.
    #
    # Rate limits here reset on a per-MINUTE window, so the budget has to be
    # able to outlast one — but only just. Unbounded doubling turns a throttled
    # sweep into an hour of sleeping, so each individual wait is capped below.
    # This is a backstop: the real defence is pacing requests under the limit
    # in the first place (see `firecrawl_requests_per_minute`).
    rate_limit_max_retries: int = Field(default=6, ge=0)
    rate_limit_base_delay_seconds: float = Field(default=1.0, gt=0)
    # Ceiling on any single backoff sleep. 1+2+4+8+16+32 covers a minute window
    # without a runaway 64s/128s tail.
    rate_limit_max_delay_seconds: float = Field(default=32.0, gt=0)
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

    # --- Wellfound / Firecrawl ----------------------------------------------
    # Wellfound has no public API and blocks plain HTTP clients, so its adapter
    # renders pages through Firecrawl. Without a key the source is skipped, not
    # fatal — every other adapter keeps working.
    firecrawl_api_key: str | None = None
    # Firecrawl may serve a recently indexed copy instead of refetching. Job
    # freshness is the whole point here, so default to a live fetch.
    firecrawl_max_age_ms: int = Field(default=0, ge=0)
    # Measured 2026-09-06 against the configured plan: Firecrawl answers 429
    # with "Consumed (req/min): 16, Remaining (req/min): 0". Pacing below that
    # ceiling is strictly better than retrying after it trips — a 429 costs the
    # whole backoff ladder, a deliberate gap costs ~4s. Set to 0 to disable.
    firecrawl_requests_per_minute: int = Field(default=15, ge=0)
    # Pages per query. Every page is one Firecrawl browser render, and the
    # configured queries all spend their full allowance, so this multiplies:
    # 8 queries x 10 pages was 80 renders fired back-to-back, which tripped
    # the rate limit before stage 2 even started. 3 pages is ~110 jobs per
    # query, and the queries overlap heavily anyway (jobs dedupe by id).
    wellfound_max_pages: int = Field(default=3, ge=1)
    # Stage 2 confirms eligibility on the detail page. Turning it off makes a
    # run cheaper but lets Wellfound's "Everywhere" claim through unchecked.
    wellfound_verify_details: bool = Field(default=True)
    # Ceiling on stage-2 detail fetches per run — one Firecrawl credit each.
    wellfound_detail_budget: int = Field(default=60, ge=0)

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
