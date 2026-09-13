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

    # --- Structured commitment / work-mode ----------------------------------
    # Normalized vocabularies from `normalize.employment_type` and
    # `normalize.workplace_type`; None means the board never said, which
    # PASSES a preference rather than failing it. An adapter sets these only
    # from a field its board actually publishes — never inferred from prose,
    # because `workplace_type` exists precisely to be the trustworthy
    # alternative to the `detect_remote` keyword scan.
    employment_type: str | None = None
    workplace_type: str | None = None

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

    # Structured commitment / work mode. None means the board never said —
    # rendered as "not stated", never as a negative.
    employment_type: str | None = None
    workplace_type: str | None = None

    # Validity. `None` is "not yet checked", distinct from a low score; the
    # dashboard shows no badge at all rather than a zero.
    validity_score: int | None = None
    validity_reasons: list[str] = Field(default_factory=list)
    # When the verifier (the deterministic validity pass) last ran on this row.
    validity_checked_at: datetime | None = None
    # Whether the LLM has read this exact JD text. Every row shows both this
    # and the verifier state, so "was this checked?" is never a guess.
    llm_read: bool = False


class JobDetailOut(JobOut):
    description_html: str | None
    description_text: str | None
    raw_json: dict[str, Any] | None
    # The validity half of the LLM screen, cached on the posting under
    # `content_hash` alone, so it is present here even for jobs whose fit
    # verdict was invalidated by a profile edit.
    llm_validity: dict[str, Any] | None = None


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
    # A CURATED board that fetched nothing and has never stored a row. Its
    # slug is almost certainly wrong or the company has left that ATS — the
    # `skipped_closure_sweep` guard cannot see this case, because that guard
    # keys off having had open rows to lose. Surfaced so a dead config entry
    # is visible instead of silently contributing zero for months.
    never_produced_rows: bool = False
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


# --------------------------------------------------------------------------
# Matching (Phase 2B/2C — fit against `profile.yaml`)
# --------------------------------------------------------------------------
class MatchOut(BaseModel):
    """A job plus its fit verdict. Embeds the job so the list is one request."""

    model_config = ConfigDict(from_attributes=True)

    job: JobOut
    score: int
    band: str
    subscores: dict[str, int]
    match_reasons: list[str]
    confident: bool
    matched_skills: list[str]
    missing_stacks: list[str]
    years_required: int | None
    # On the LLM shortlist (every free rule passed). `shortlist_reasons` says
    # why not, when it is not — e.g. ["low_confidence", "blocked"].
    shortlisted: bool = False
    shortlist_reasons: list[str] = Field(default_factory=list)
    # Hard blockers read from the JD for free: `key:evidence`.
    blockers: list[str] = Field(default_factory=list)
    llm_used: bool
    # A current, successful deep read of this job against this profile.
    llm_read: bool = False
    llm_verdict: dict[str, Any] | None = None
    # Preference verdict — an overlay refreshed on every pass, never part of
    # the match's identity. Reasons cover passes too, so the drawer can say
    # "admitted because it stated no salary".
    prefs_pass: bool = True
    prefs_reasons: list[str] = Field(default_factory=list)
    prefs_version: str = ""
    # USD pay, surfaced as a finding on the match row. `usd_pay_source` says
    # where it was read: the board's structured salary, the deep read's
    # verbatim quote, or a pattern match over the JD prose — the last is the
    # common case, since Greenhouse states pay only in the description.
    usd_pay: str | None = None
    usd_pay_source: Literal["board", "deep_read", "jd"] | None = None
    profile_version: str
    scored_at: datetime


class MatchListOut(BaseModel):
    total: int
    limit: int
    offset: int
    profile_version: str
    items: list[MatchOut]
    # Band histogram over the whole filtered set, not just this page — the
    # panel header shows the shape of the result, which pagination would hide.
    bands: dict[str, int]
    shortlisted: int
    # Rows before the band selection is applied — the "All" chip. `total` is
    # the selected band's size.
    total_all: int = 0
    # Blocked jobs the "Hide blocked" toggle removes (counted before it applies).
    blocked: int = 0


class MatchRunOut(BaseModel):
    """Result of a scoring pass."""

    profile_version: str
    considered: int
    in_transfer: int = 0
    scored: int
    updated: int
    skipped: int
    # Every count from here down covers only the jobs in scope (`in_transfer`).
    shortlisted: int
    blocked: int = 0
    low_validity: int = 0
    low_confidence: int
    bands: dict[str, int]
    duration_ms: int
    error: str | None = None
    # Which preferences gated this pass, and how many rows they admitted.
    # `matching_prefs` is the size of the Matches list; `considered` is the
    # whole scored surface, so the difference is what preferences filtered.
    prefs_version: str = ""
    matching_prefs: int = 0


class LLMRunOut(BaseModel):
    """Result of a deep-read pass."""

    profile_version: str
    routed: int
    cached: int
    screened: int
    failed: int
    skipped_budget: int
    requests: int
    tokens: int
    bands: dict[str, int]
    statuses: dict[str, int]
    blocked: int
    duration_ms: int
    error: str | None = None


class LLMStatusOut(BaseModel):
    """Poll target for a running deep read.

    The pass is paced by an 8,000 token/minute ceiling, so ~400 rows takes
    ~90 minutes. It cannot be a synchronous request, and a spinner over that
    long reads as broken — hence per-row progress, the same answer
    `/ingest/status` gives for a sweep.
    """

    state: Literal["idle", "running", "done"]
    total: int = 0
    done: int = 0
    cached: int = 0
    failed: int = 0
    tokens: int = 0
    current: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: LLMRunOut | None = None


class ProfileOut(BaseModel):
    """The profile, as the dashboard needs to show it."""

    version: str
    full_name: str
    location: str
    total_years: int
    ai_years: int
    current_title: str
    target_titles: list[str]
    skills: dict[str, dict[str, int]]
    gaps: dict[str, int]
    min_annual_inr: float
    needs_sponsorship: bool
    resume_files: dict[str, str]


# --------------------------------------------------------------------------
# Preferences and the Matches "Run" pipeline
# --------------------------------------------------------------------------
class PrefsOut(BaseModel):
    """`prefs.yaml` over the wire, plus what it currently selects.

    `version` is what makes a stale Matches list detectable: the list reports
    the `prefs_version` its rows were gated against, and the UI can compare.
    """

    version: str
    work: dict[str, Any]
    experience: dict[str, Any]
    compensation: dict[str, Any]
    validity: dict[str, Any]
    freshness: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any]
    # The Jobs filters sent with "Send to Matches"; None = all eligible jobs.
    transfer: dict[str, Any] | None = None
    include_unstated: bool
    updated_at: datetime | None = None
    # Vocabularies the editor renders as checkboxes, served rather than
    # duplicated in TypeScript — a client-side copy is how a valid option
    # silently disappears from the UI after a backend change.
    workplace_options: list[str] = Field(default_factory=list)
    employment_options: list[str] = Field(default_factory=list)


class PrefsIn(BaseModel):
    """A preferences write. Every field optional — a partial save merges.

    Validation happens in `Prefs` itself (unknown employment type, inverted
    year band), so a bad write is a 422 with the offending value named rather
    than a silently-ignored field.
    """

    work: dict[str, Any] | None = None
    experience: dict[str, Any] | None = None
    compensation: dict[str, Any] | None = None
    validity: dict[str, Any] | None = None
    freshness: dict[str, Any] | None = None
    scope: dict[str, Any] | None = None
    include_unstated: bool | None = None


class ValidityRunOut(BaseModel):
    considered: int
    scored: int
    unchanged: int
    suspect: int
    duplicates: int
    llm_applied: int
    bands: dict[str, int] = Field(default_factory=dict)
    reasons: dict[str, int] = Field(default_factory=dict)
    duration_ms: int = 0
    error: str | None = None


class LLMEstimateOut(BaseModel):
    """What the deep read would cost. Shown before anything is spent."""

    routed: int
    cached: int
    pending: int
    cap: int = 0
    over_cap: int = 0
    est_tokens: int
    est_minutes: float
    requests: int
    # The provider's long token cap (per day on Groq, per month on Mistral),
    # how much of it is already gone, and this pass's share of it.
    token_cap: int = 0
    token_cap_period: str = ""
    tokens_used: int = 0
    cap_budget_pct: float = 0.0
    fits_in_cap: int = 0
    provider: str = ""
    model: str
    configured: bool
    note: str = ""


class PipelineStatusOut(BaseModel):
    """`POST /matches/run` and `GET /matches/run/status`.

    The pipeline runs the two free passes and stops. `estimate` is the price
    of the expensive one; starting it is a separate, explicit call.
    """

    stage: Literal["idle", "validity", "ranking", "estimating", "done", "failed"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    profile_version: str = ""
    prefs_version: str = ""
    validity: ValidityRunOut | None = None
    ranking: MatchRunOut | None = None
    estimate: LLMEstimateOut | None = None
