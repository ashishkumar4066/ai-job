"""Pydantic models: config, the adapter contract, and API payloads.

`JobPosting` here is the *normalized* in-memory model produced by adapters.
`models.JobPosting` is the ORM row it is persisted into. They intentionally
share a name — the adapter interface in the spec is
`normalize(raw, company) -> JobPosting`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JobStatus = Literal["open", "closed"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SLUG_RE.sub("-", value.strip().lower()).strip("-")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class SourceConfig(BaseModel):
    """One entry from `companies.yaml` — either family of adapter.

    A **curated ATS** entry is `{company, ats, token_or_slug}`; adding a company
    whose ATS already has an adapter is a config change only.

    An **aggregator board** entry (Himalayas, Remotive) needs no board token —
    it carries `queries` instead, the set of API parameter dicts to sweep. Both
    shapes travel through the same adapter interface, so nothing downstream
    branches on which family a job came from.
    """

    model_config = ConfigDict(frozen=True)

    company: str
    ats: str
    # Aggregator boards have no per-company token, so this is optional.
    token_or_slug: str = ""
    enabled: bool = True
    # Optional override for the public careers page (used only in log context).
    careers_url: str | None = None

    # --- Eligibility inputs -------------------------------------------------
    # Curated entries are pre-vetted, so the config can assert what the feed
    # cannot tell us. `us_employer` is the `is_us_employer` input for the pay
    # rule; aggregator feeds leave it None (unknown), which is deliberately
    # distinct from False.
    us_employer: bool | None = None
    # Fallback candidate-eligibility for a pre-vetted company, used only when a
    # posting's own location strings resolve to nothing.
    location_eligibility: list[str] = Field(default_factory=list)

    # --- Aggregator-board options -------------------------------------------
    # Query parameter sets to sweep. Each dict is one API call (paged out by
    # the adapter); results are merged and de-duplicated across the set.
    queries: list[dict[str, Any]] = Field(default_factory=list)
    # Minimum gap between fetches, for boards whose terms cap request volume
    # (Remotive asks for at most ~4 calls/day). None = fetch every run.
    min_fetch_interval_minutes: int | None = None

    @field_validator("ats")
    @classmethod
    def _normalize_ats(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def key(self) -> str:
        """Stable, filesystem/URL-safe company identifier used in `source_key`."""
        return slugify(self.company)

    @property
    def source_id(self) -> str:
        """Identifies one board — the unit of the closure sweep.

        For an aggregator this is the whole board (`himalayas:himalayas`), not
        one query: a job found by two queries in the same sweep is one job, and
        `source_key` is globally unique.
        """
        return f"{self.ats}:{self.key}"


# The historical name, kept so existing call sites and tests keep working.
CompanyConfig = SourceConfig


# --------------------------------------------------------------------------
# Adapter contract
# --------------------------------------------------------------------------
class RawJob(BaseModel):
    """An untouched posting as returned by an ATS, plus its native id.

    `payload` is persisted verbatim to `raw_json` so a mapping bug is always
    recoverable without re-fetching.
    """

    model_config = ConfigDict(frozen=True)

    native_id: str
    payload: dict[str, Any]


class JobPosting(BaseModel):
    """Normalized posting — the single schema every source maps into."""

    source_key: str
    ats: str
    company: str
    title: str
    locations: list[str] = Field(default_factory=list)
    remote: bool = False
    department: str | None = None
    apply_url: str
    description_html: str | None = None
    description_text: str | None = None
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    raw_json: dict[str, Any] = Field(default_factory=dict)

    # --- Eligibility ---------------------------------------------------------
    # ISO-3166-1 alpha-2 codes and/or the `worldwide` sentinel: where a
    # candidate may be based. Empty means the source told us nothing.
    location_eligibility: list[str] = Field(default_factory=list)
    # Human-readable timezone constraints ("UTC+05:30", "European timezones").
    timezone_restrictions: list[str] = Field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    # Tri-state on purpose: None = unknown, which the pay rule treats
    # differently from a known-false.
    is_us_employer: bool | None = None
    # Set by the filter stage in `ingest`, never by an adapter.
    eligibility_pass: bool = False
    eligibility_reasons: list[str] = Field(default_factory=list)

    @property
    def primary_location(self) -> str:
        if self.locations:
            return ", ".join(self.locations[:3])
        return "Remote" if self.remote else "—"


# --------------------------------------------------------------------------
# API payloads
# --------------------------------------------------------------------------
class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_key: str
    ats: str
    company: str
    title: str
    locations: list[str]
    remote: bool
    department: str | None
    apply_url: str
    posted_at: datetime | None
    updated_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    status: JobStatus

    location_eligibility: list[str]
    timezone_restrictions: list[str]
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    is_us_employer: bool | None
    eligibility_pass: bool
    eligibility_reasons: list[str]


class JobDetailOut(JobOut):
    description_html: str | None
    description_text: str | None
    raw_json: dict[str, Any] | None


class JobListOut(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobOut]


class SourceResultOut(BaseModel):
    """Per-source outcome of one ingest run: the spec's required counts."""

    source_id: str
    company: str
    ats: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    closed: int = 0
    errored: int = 0
    # How many of `fetched` cleared the eligibility filter this run.
    eligible: int = 0
    ok: bool = True
    skipped_closure_sweep: bool = False
    # True when the source was not contacted at all because its configured
    # minimum fetch interval had not elapsed (see `min_fetch_interval_minutes`).
    throttled: bool = False
    error: str | None = None
    duration_ms: int = 0


class IngestRunOut(BaseModel):
    run_id: int
    started_at: datetime
    finished_at: datetime | None
    fetched: int
    eligible: int = 0
    new: int
    updated: int
    closed: int
    errored: int
    notified: int
    sources: list[SourceResultOut] = Field(default_factory=list)


class SourceProgressOut(BaseModel):
    """One board's live state inside a run that has not finished yet."""

    source_id: str
    company: str
    ats: str
    state: Literal["pending", "fetching", "done", "failed", "throttled"]
    fetched: int = 0
    new: int = 0
    eligible: int = 0
    error: str | None = None


class IngestStatusOut(BaseModel):
    """What `POST /ingest/refresh` and `GET /ingest/status` both answer with.

    `state` is the only field the dashboard needs to decide whether to keep
    waiting:

      * `fresh`   — the sweep was skipped, stored data already covers the
                    requested window; render immediately.
      * `running` — a run is in flight (this request may or may not have
                    started it); keep polling.
      * `idle`    — nothing is running; `result` / `error` describe the last
                    run this process drove.
    """

    state: Literal["fresh", "running", "idle"]
    # True when this call deliberately did not start a sweep.
    skipped: bool = False
    reason: str | None = None
    run_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Newest `finished_at` across all runs ever recorded, from the database —
    # survives a restart, unlike the in-memory fields above.
    last_finished_at: datetime | None = None
    sources_total: int = 0
    sources_done: int = 0
    sources: list[SourceProgressOut] = Field(default_factory=list)
    result: IngestRunOut | None = None
    error: str | None = None
