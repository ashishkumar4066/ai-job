"""Application settings, loaded from environment / .env.

Secrets never live in code. See `.env.example` for the full list.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _alias(name: str) -> AliasChoices:
    """`LLM_<NAME>`, falling back to the `GROQ_<NAME>` it was called before
    the provider switch existed — so an existing .env keeps working."""
    return AliasChoices(f"llm_{name}", f"groq_{name}")


@dataclass(frozen=True)
class LLMProvider:
    """One LLM provider, resolved from settings. What the client needs, flat.

    `limits` names only the windows this provider enforces — Groq limits per
    minute and per day, Mistral per second, per minute and per month, Cerebras
    per minute, per hour and per day — and `app/ratelimit.py` builds exactly
    those.
    """

    name: str
    key_env: str
    api_key: str | None
    base_url: str
    model: str
    reasoning_effort: str | None
    limits: dict[str, int]
    # Which `ratelimit.HEADER_SCHEMES` entry describes this provider's
    # rate-limit headers. Headers mean different windows per provider, so they
    # are only read where that meaning is documented or verified live.
    header_scheme: str | None
    retries_bad_request: bool
    # JSON Schema keywords the provider's strict mode does not support. They
    # are stripped from the request schema; `JobScreen` still parses the reply.
    unsupported_schema_keywords: frozenset[str] = frozenset()
    # Sent as `max_completion_tokens` when set. Cerebras books a call against
    # its token limits using this figure, or an upper-bound guess without it.
    max_completion_tokens: int | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Shared LLM knobs carry a validation alias (LLM_* or GROQ_*); this
        # keeps the plain field name usable when constructing Settings in code.
        populate_by_name=True,
    )

    # --- Database -----------------------------------------------------------
    # SQLite for local/personal scale. Swapping to Postgres later is a URL
    # change only: the models deliberately avoid dialect-specific column types.
    database_url: str = Field(default=f"sqlite+aiosqlite:///{BACKEND_ROOT / 'jobs.db'}")

    # --- Ingestion ----------------------------------------------------------
    companies_file: Path = Field(default=BACKEND_ROOT / "companies.yaml")
    # Eligibility + currency rules applied between normalize and persist.
    filters_file: Path = Field(default=BACKEND_ROOT / "config" / "filters.yaml")
    # The candidate profile every job is scored against. Source of truth on
    # disk: the dashboard edits it, but hand-editing must stay first-class.
    profile_file: Path = Field(default=BACKEND_ROOT / "profile.yaml")
    # What I want out of the board right now — the Matches gate. Distinct
    # from `filters_file` (can I hold this job at all?) and `profile_file`
    # (who am I?). Edited freely from the dashboard; a missing file is fine
    # and means "no view expressed", so `load_prefs` returns defaults.
    prefs_file: Path = Field(default=BACKEND_ROOT / "prefs.yaml")
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

    # --- The Muse -------------------------------------------------------------
    # Required by the terms "for any use beyond testing", though the API still
    # answers without one: https://www.themuse.com/developers/api/v2. Unkeyed is
    # 500 calls/hour; the registered key measured 12,000. A sweep spends <=100.
    themuse_api_key: str | None = None

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

    # --- YC jobs / Arc.dev ----------------------------------------------------
    # Both are read off public HTML pages with plain HTTP (no Firecrawl). The
    # gaps below are self-imposed politeness, not documented limits: YC's
    # robots.txt sets no crawl delay; Arc's sets none for `*` and 10s for
    # named crawlers. Detail pages are fetched only for India/worldwide
    # candidates, and each budget caps how many per run.
    yc_request_interval_seconds: float = Field(default=1.0, ge=0)
    yc_detail_budget: int = Field(default=40, ge=0)
    arc_request_interval_seconds: float = Field(default=3.0, ge=0)
    arc_detail_budget: int = Field(default=40, ge=0)
    # YC `companies` queries read one page per hiring company (251 matched
    # India/Fully Remote on 2026-09-19), so they are capped per run.
    yc_company_budget: int = Field(default=80, ge=0)

    # --- Cutshort / Hirist / Built In ---------------------------------------
    # Cutshort and Hirist are JSON endpoints behind their own sites; Built In
    # is HTML. All three list newest first, so a sweep stops at `*_max_age_days`
    # instead of walking thousands of old rows. Hirist's 10s gap is the
    # `Crawl-delay: 10` its site's robots.txt asks for, applied to its API too.
    cutshort_request_interval_seconds: float = Field(default=1.0, ge=0)
    cutshort_max_pages: int = Field(default=10, ge=1)
    cutshort_max_age_days: int = Field(default=30, ge=1)
    hirist_request_interval_seconds: float = Field(default=10.0, ge=0)
    hirist_max_pages: int = Field(default=3, ge=1)
    hirist_max_age_days: int = Field(default=30, ge=1)
    hirist_detail_budget: int = Field(default=20, ge=0)
    builtin_request_interval_seconds: float = Field(default=2.0, ge=0)
    builtin_max_pages: int = Field(default=15, ge=1)
    builtin_detail_budget: int = Field(default=40, ge=0)

    # --- RemoteYeah -----------------------------------------------------------
    # HTML listing pages, newest first; a sweep stops at `max_age_days`. Its
    # robots.txt sets no delay, so the gap is self-imposed politeness. Detail
    # pages are read only for India/worldwide rows with a wanted title; 60 ran
    # out on the first live sweep (203 rows, 132 eligible), hence 120.
    remoteyeah_request_interval_seconds: float = Field(default=2.0, ge=0)
    remoteyeah_max_pages: int = Field(default=10, ge=1)
    remoteyeah_max_age_days: int = Field(default=30, ge=1)
    remoteyeah_detail_budget: int = Field(default=120, ge=0)

    # --- LLM deep read -------------------------------------------------------
    # The deep-read pass. Without a key for the selected provider the pass is
    # skipped, not fatal — deterministic scores stand on their own and the
    # dashboard still ranks.
    #
    # `LLM_PROVIDER` picks which block below is live. Everything after the two
    # provider blocks is shared. Switching providers does not invalidate cached
    # verdicts: a verdict is keyed by (JD, profile), not by who wrote it, and
    # each one records `provider` and `model` so the two stay distinguishable.
    llm_provider: Literal["groq", "mistral", "cerebras"] = Field(default="groq")

    # --- Groq --------------------------------------------------------------
    groq_api_key: str | None = None
    # Default measured against the live board 2026-09-08, not chosen on specs.
    # `qwen/qwen3.8-27b` returned schema-valid JSON on 4 of 4 probe rows at
    # ~1,700 tokens each. `openai/gpt-oss-120b` cost ~3,100 tokens with
    # `reasoning_effort=low` and violated its own strict schema on 3 of those 4
    # — intermittently, so it cannot be retried away cheaply — and read
    # "Software Engineer II" as an excellent senior match with no gaps where
    # qwen read it as mid-level. `openai/gpt-oss-20b` fails strict schema
    # outright (HTTP 400). Override here if that changes.
    groq_model: str = Field(default="qwen/qwen3.8-27b")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1")
    # Only sent for models that accept it (the gpt-oss family). Measured: `low`
    # produced an identical verdict to `medium` for 22% fewer tokens, because
    # completion on a reasoning model is mostly reasoning.
    groq_reasoning_effort: str = Field(default="low")
    # The four limits, per model, from https://console.groq.com/docs/rate-limits
    # (free plan, qwen/qwen3.8-27b and the gpt-oss family alike). "You can hit
    # any limit type depending on which threshold you reach first."
    #
    # Only TPM and RPD are reported in headers (`x-ratelimit-*-tokens` is
    # always TPM, `x-ratelimit-*-requests` is always RPD). RPM and TPD are not
    # visible on the wire, so `app/ratelimit.py` counts them itself — TPD from
    # the persisted `llm_usage` ledger.
    #
    # TPD is the binding one: at ~1,800 tokens a row, 200k is ~110 rows a day,
    # well before 1,000 requests. The 2026-09-13 pass hit it at 207k.
    groq_requests_per_minute: int = Field(default=30, ge=1)
    groq_requests_per_day: int = Field(default=1_000, ge=1)
    groq_tokens_per_minute: int = Field(default=8000, ge=1000)
    groq_tokens_per_day: int = Field(default=200_000, ge=1000)

    # --- Mistral -----------------------------------------------------------
    # https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key
    # Free mode needs no card. Same OpenAI-style `/chat/completions` endpoint,
    # Bearer auth, and `response_format: json_schema` with `strict`.
    mistral_api_key: str | None = None
    # The quickstart's model. NOT yet measured against this board the way qwen
    # was — probe a handful of already-read rows before trusting a full pass.
    mistral_model: str = Field(default="mistral-small-latest")
    mistral_base_url: str = Field(default="https://api.mistral.ai/v1")
    # Mistral enforces requests per SECOND, tokens per minute and tokens per
    # MONTH, per organization — no daily cap. It does not publish free-mode
    # numbers (they are on Admin → Limits, admin.mistral.ai/plateforme/limits),
    # so these defaults are the commonly reported free figures: 1 req/s, 500k
    # tokens/min, 1B tokens/month. Set them to what your Limits page shows.
    mistral_requests_per_second: int = Field(default=1, ge=1)
    mistral_tokens_per_minute: int = Field(default=500_000, ge=1000)
    mistral_tokens_per_month: int = Field(default=1_000_000_000, ge=1000)

    # --- Cerebras ----------------------------------------------------------
    # https://inference-docs.cerebras.ai — cloud.cerebras.ai → API Keys.
    # Same OpenAI-style `/chat/completions`, Bearer auth, and
    # `response_format: json_schema` with `strict` (constrained decoding).
    cerebras_api_key: str | None = None
    # The same qwen weights measured on Groq, under Cerebras' own id. Public
    # structured-output models are `qwen-3.8-27b` and `gpt-oss-120b`. Not yet
    # probed against this board on Cerebras — check a handful of rows first.
    cerebras_model: str = Field(default="qwen-3.8-27b")
    cerebras_base_url: str = Field(default="https://api.cerebras.ai/v1")
    # Both public models accept it. qwen takes none|low|medium|high and
    # defaults to HIGH, which would bill a long reasoning trace on every row;
    # gpt-oss-120b takes low|medium|high. `low` is valid for both. Empty = omit.
    cerebras_reasoning_effort: str = Field(default="low")
    # Cerebras books a call against its token limits as prompt + this figure
    # (or its own upper-bound guess when unset), before generating anything.
    # Reasoning counts toward it. qwen's measured completion is 339-600 tokens
    # on Groq; 4,096 leaves room for a `low` reasoning trace. A reply cut off
    # here fails as `finish_reason=length` — raise it if that shows up.
    cerebras_max_completion_tokens: int = Field(default=4096, ge=256)
    # Limits are per MODEL; these defaults are qwen-3.8-27b's. Set them again
    # if CEREBRAS_MODEL changes.
    #
    # Minute figures: the Developer tier on
    # https://inference-docs.cerebras.ai/models/qwen-3.8-27b — 300 requests,
    # 150K uncached tokens (450K total). Hour and day are not on that page;
    # they are what this account's key reported live on 2026-09-13 (which also
    # read 450 requests/min, above the documented 300 — the lower one is kept).
    # Free Trial is far lower: 5 requests/min, 30K uncached tokens/min, 1M
    # tokens/day — set these to that if the key is on it.
    #
    # Token limits are the UNCACHED bucket; every token is counted against it,
    # so the 3x-larger total bucket can never bind first. Cerebras also reports
    # all six windows in response headers and those are read on every call, so
    # a key on a lower tier is paced by what the server says, not these.
    cerebras_requests_per_minute: int = Field(default=300, ge=1)
    cerebras_requests_per_hour: int = Field(default=27_000, ge=1)
    cerebras_requests_per_day: int = Field(default=648_000, ge=1)
    cerebras_tokens_per_minute: int = Field(default=150_000, ge=1000)
    cerebras_tokens_per_hour: int = Field(default=9_000_000, ge=1000)
    cerebras_tokens_per_day: int = Field(default=216_000_000, ge=1000)

    # --- Shared by every provider ------------------------------------------
    # Each accepts its old GROQ_* name too, so an existing .env keeps working.
    #
    # Exponential backoff with jitter, for the failures that do not name their
    # own wait: a 429 without `retry-after`, 5xx, and network errors. The n-th
    # retry waits uniformly within [d/2, d] where d = min(max, base * 2**n) —
    # the jitter keeps retries from re-colliding with the window that refused
    # them, the half-floor keeps a retry from firing immediately.
    llm_backoff_base_seconds: float = Field(
        default=2.0, gt=0, validation_alias=_alias("backoff_base_seconds")
    )
    llm_backoff_max_seconds: float = Field(
        default=60.0, gt=0, validation_alias=_alias("backoff_max_seconds")
    )
    # 429s on one row before it is recorded as failed. Separate from
    # `llm_max_retries`, because a 429 is the window's fault, not the row's.
    llm_max_rate_limit_retries: int = Field(
        default=8, ge=0, validation_alias=_alias("max_rate_limit_retries")
    )
    # A named wait longer than this is a daily or monthly cap, not a minute
    # window: the pass stops rather than sleep through it one row at a time.
    llm_max_wait_seconds: float = Field(
        default=300.0, gt=0, validation_alias=_alias("max_wait_seconds")
    )
    # Spend only this fraction of a per-second/per-minute ceiling. A token
    # estimate that comes in low still has room to land, and pacing under the
    # limit is strictly cheaper than tripping it: a 429 costs the whole retry,
    # a gap costs a few seconds.
    llm_budget_headroom: float = Field(
        default=0.9, gt=0.1, le=1.0, validation_alias=_alias("budget_headroom")
    )
    # Reserved for the answer before it exists, reconciled against actual usage
    # once the response lands. Measured completion on qwen is 339-600 tokens;
    # 700 covers it without over-reserving, which matters because the estimate
    # is what the pacer compares against the ceiling — reserving 1,100 fits two
    # calls per minute where the real cost fits three.
    llm_completion_reserve: int = Field(
        default=700, ge=100, validation_alias=_alias("completion_reserve")
    )
    # Default reads per pass. The shortlist is read best-first and this many
    # at most; `shortlist.HARD_MAX_READS` (50) caps it whatever is configured.
    # Was 500, which let one confirm read 427 jobs for 1.26M tokens. Not
    # validated `le=50`: an old `.env` carrying 500 would then refuse to boot,
    # so the ceiling is applied by `shortlist.clamp_reads` instead.
    llm_max_requests_per_run: int = Field(
        default=15, ge=1, validation_alias=_alias("max_requests_per_run")
    )
    # JD characters sent. p90 of the routed rows is 7,070 chars, so this keeps
    # ~95% of them whole while bounding the 13k-char outliers.
    llm_max_jd_chars: int = Field(default=8000, ge=500, validation_alias=_alias("max_jd_chars"))
    # Completion ceiling for Stage 3 résumé tailoring, which answers with up to
    # 16 rewritten lines plus its notes. Measured: the screen's 4,096 default
    # truncates it mid-answer ("completion truncated at max_completion_tokens").
    # Cerebras books this figure against its limits up front, so it is a
    # separate knob rather than a raised global.
    llm_tailor_completion_tokens: int = Field(
        default=10_000, ge=2000, validation_alias=_alias("tailor_completion_tokens")
    )
    # The cover letter answers with ~350 words plus notes — far less than a
    # tailoring, and Cerebras books the ceiling up front, so it has its own.
    llm_cover_completion_tokens: int = Field(
        default=6_000, ge=2000, validation_alias=_alias("cover_completion_tokens")
    )
    # Stage 1 profile intake reads a whole résumé and answers with the largest
    # structured object in the app: up to 80 skills, 30 gaps, 10 evidence lines
    # and every bullet verbatim. Runs once at setup, so the ceiling is generous
    # — a truncated answer here would cost a re-parse of the whole résumé.
    llm_intake_completion_tokens: int = Field(
        default=16_000, ge=4000, validation_alias=_alias("intake_completion_tokens")
    )
    llm_timeout_seconds: float = Field(
        default=120.0, gt=0, validation_alias=_alias("timeout_seconds")
    )
    # A strict-schema violation, 5xx or network error is retried this many
    # times before the row is recorded as failed. Failures are recorded, never
    # silently skipped.
    llm_max_retries: int = Field(default=3, ge=0, validation_alias=_alias("max_retries"))

    @property
    def llm(self) -> LLMProvider:
        """The selected provider, resolved to one flat description."""
        if self.llm_provider == "cerebras":
            return LLMProvider(
                name="cerebras",
                key_env="CEREBRAS_API_KEY",
                api_key=self.cerebras_api_key,
                base_url=self.cerebras_base_url.rstrip("/"),
                model=self.cerebras_model,
                reasoning_effort=self.cerebras_reasoning_effort or None,
                limits={
                    "requests_per_minute": self.cerebras_requests_per_minute,
                    "requests_per_hour": self.cerebras_requests_per_hour,
                    "requests_per_day": self.cerebras_requests_per_day,
                    "tokens_per_minute": self.cerebras_tokens_per_minute,
                    "tokens_per_hour": self.cerebras_tokens_per_hour,
                    "tokens_per_day": self.cerebras_tokens_per_day,
                },
                header_scheme="cerebras",
                # Constrained decoding guarantees the schema, so a 400 is a
                # malformed request, not an intermittent model failure.
                retries_bad_request=False,
                # Documented as unsupported in strict mode.
                unsupported_schema_keywords=frozenset({"minItems", "maxItems"}),
                max_completion_tokens=self.cerebras_max_completion_tokens,
            )
        if self.llm_provider == "mistral":
            return LLMProvider(
                name="mistral",
                key_env="MISTRAL_API_KEY",
                api_key=self.mistral_api_key,
                base_url=self.mistral_base_url.rstrip("/"),
                model=self.mistral_model,
                reasoning_effort=None,
                limits={
                    "requests_per_second": self.mistral_requests_per_second,
                    "tokens_per_minute": self.mistral_tokens_per_minute,
                    "tokens_per_month": self.mistral_tokens_per_month,
                },
                header_scheme=None,
                retries_bad_request=False,
            )
        return LLMProvider(
            name="groq",
            key_env="GROQ_API_KEY",
            api_key=self.groq_api_key,
            base_url=self.groq_base_url.rstrip("/"),
            model=self.groq_model,
            # Only the gpt-oss family accepts it; sending it to qwen is a 400.
            reasoning_effort=(
                self.groq_reasoning_effort or None if "gpt-oss" in self.groq_model else None
            ),
            limits={
                "requests_per_minute": self.groq_requests_per_minute,
                "requests_per_day": self.groq_requests_per_day,
                "tokens_per_minute": self.groq_tokens_per_minute,
                "tokens_per_day": self.groq_tokens_per_day,
            },
            header_scheme="groq",
            # Measured on Groq: a 400 here is the model violating its own
            # strict schema, intermittently, so a retry can succeed.
            retries_bad_request=True,
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm.api_key)

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
