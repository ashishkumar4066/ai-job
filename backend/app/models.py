"""SQLAlchemy 2.0 ORM models.

Deliberately dialect-neutral: `JSON` (not JSONB) and no `ARRAY`, so the same
schema runs on SQLite today and Postgres later with only a URL change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on both SQLite and Postgres.

    SQLite has no native tz storage and silently drops tzinfo, which makes
    naive/aware comparisons blow up later. This normalizes both directions so
    application code only ever sees aware UTC values.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class JobPosting(Base):
    __tablename__ = "job_postings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity: "{ats}:{company}:{job_id}" — the ATS-native stable id.
    source_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)
    # "{ats}:{company}" — the board this job belongs to; scopes the closure sweep.
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    ats: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    company: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    locations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    remote: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)

    description_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    posted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)

    # sha256 over the meaningful normalized fields; lets ingest skip no-op
    # writes and seeds the Phase 2 validation cache.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # --- Eligibility --------------------------------------------------------
    # Where a candidate may be based: ISO-3166-1 alpha-2 codes and/or the
    # `worldwide` sentinel. Empty means the source gave us nothing to go on.
    location_eligibility: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    timezone_restrictions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)

    # Tri-state: None = unknown. The pay rule treats unknown differently from a
    # known-false, so this must stay nullable.
    is_us_employer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Verdict of the filter stage. False never means "dropped" — the row is
    # always stored; `eligibility_reasons` records what decided it.
    eligibility_pass: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    eligibility_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_job_postings_source_status", "source_id", "status"),
        Index("ix_job_postings_status_posted", "status", "posted_at"),
        Index("ix_job_postings_eligible_status", "eligibility_pass", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobPosting {self.source_key} status={self.status}>"


class IngestRun(Base):
    """One execution of the ingest pipeline — the audit trail for a run."""

    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # How many fetched postings cleared the eligibility filter.
    eligible: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sources: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IngestRun {self.id} new={self.new} closed={self.closed}>"
