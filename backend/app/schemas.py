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
class CompanyConfig(BaseModel):
    """One entry from `companies.yaml`.

    Adding a company whose ATS already has an adapter is a config change only.
    """

    model_config = ConfigDict(frozen=True)

    company: str
    ats: str
    token_or_slug: str
    enabled: bool = True
    # Optional override for the public careers page (used only in log context).
    careers_url: str | None = None

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
        """Identifies one (ats, company) board — the unit of the closure sweep."""
        return f"{self.ats}:{self.key}"


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
    ok: bool = True
    skipped_closure_sweep: bool = False
    error: str | None = None
    duration_ms: int = 0


class IngestRunOut(BaseModel):
    run_id: int
    started_at: datetime
    finished_at: datetime | None
    fetched: int
    new: int
    updated: int
    closed: int
    errored: int
    notified: int
    sources: list[SourceResultOut] = Field(default_factory=list)
