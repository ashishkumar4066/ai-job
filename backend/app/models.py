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
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
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

    # --- Structured fields the preferences layer filters on -----------------
    # Normalized vocabularies, not the boards' raw spellings, because five
    # sources write the same fact five ways ("FullTime", "Full Time",
    # "full-time", "Full-time", "full_time"). `normalize.employment_type`
    # owns the mapping.
    #
    # NULL means the board never said. That has to PASS a preference rather
    # than fail it: Greenhouse publishes neither field, and failing NULL would
    # silently drop all 2,224 of its rows — the same trap the unstated-pay and
    # unstated-years rules already avoid.
    employment_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    workplace_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    # --- Validity (Phase 2B) ------------------------------------------------
    # Nullable on purpose: NULL is "not yet checked", 0 is "checked, and it is
    # worthless". Defaulting to 0 would make an unrun pass look like a board
    # full of ghosts.
    validity_score: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    validity_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    validity_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # The validity half of the LLM screen, keyed on `content_hash` ALONE.
    #
    # This is the second of CLAUDE.md's two cache keys, and it is a column on
    # the posting rather than on `job_matches` for a measured reason: one Groq
    # call returns validity and fit together, but only fit depends on the
    # profile. While both halves lived in `job_matches.llm_verdict` — a row
    # keyed by `(job_id, profile_version)` — editing `profile.yaml` orphaned
    # the validity answer too, so the next pass re-read ~440 rows over ~2
    # hours to recompute a judgement about JD text that had not changed.
    llm_validity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    llm_validity_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_job_postings_source_status", "source_id", "status"),
        Index("ix_job_postings_status_posted", "status", "posted_at"),
        Index("ix_job_postings_eligible_status", "eligibility_pass", "status"),
    )

    @property
    def llm_read(self) -> bool:
        """The LLM has read this exact JD text (the validity half is current).

        Keyed on `content_hash` alone, so it answers "has the LLM ever read
        this posting as it stands", independent of which profile was used.
        """
        return bool(
            self.llm_validity
            and "error" not in self.llm_validity
            and self.llm_validity_hash == self.content_hash
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobPosting {self.source_key} status={self.status}>"


class JobMatch(Base):
    """One job scored against one profile version.

    Its own table rather than columns on `job_postings`, because a fit score is
    a fact about a *pairing*, not about the posting. The same job scored
    against a rewritten résumé is a different row, and the old one stays
    readable — so "why did this drop from 78 to 61 after I edited my CV?" is
    answerable instead of overwritten.

    Contrast `eligibility_pass`, which *is* a column on the posting: that
    verdict depends only on the job and the filter config, never on me.
    """

    __tablename__ = "job_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Hash of the scoring-relevant profile fields; see `app/profile.py`.
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    subscores: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    match_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # False when the JD named too few technologies for the overlap score to
    # mean anything. Drives LLM routing independently of `score`.
    confident: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    matched_skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    missing_stacks: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    years_required: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- LLM half (populated by the deep-read pass, not the free ranker) ----
    llm_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # Cache key for the FIT half: the JD text it was judged against. Paired
    # with `profile_version` above, this is the first of CLAUDE.md's two keys —
    # an unchanged JD *and* an unchanged profile means zero API calls.
    #
    # The validity half of the same call is NOT stored here. It lives on
    # `JobPosting.llm_validity`, keyed on `content_hash` alone, so that a
    # profile edit re-bills fit without re-billing validity. See that column.
    llm_content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    llm_verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # --- Preference verdict: an overlay, deliberately NOT part of the key ---
    # Preferences decide which scored rows the Matches list shows; they never
    # change a score. Putting `prefs_version` in the unique key would fork 929
    # identically-scored rows on every tweak of "full-time only", which is
    # exactly the cost that gating *scoring* instead of *eligibility* exists
    # to avoid. So these are refreshed in place on each pass.
    #
    # `prefs_pass` defaults True: a row that has never been gated is
    # unfiltered, not rejected, so an older row does not vanish from Matches.
    prefs_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    prefs_pass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    prefs_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # --- Hard blockers read from the JD for free (`app/blockers.py`) --------
    # `key:evidence` strings. NULL = not checked yet. `blocked` mirrors
    # "non-empty" as an indexed boolean so the LLM shortlist is plain SQL.
    blockers: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    scored_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    def llm_read_for(self, job: JobPosting) -> bool:
        """A current, successful deep read exists for this pairing."""
        return bool(
            self.llm_used
            and self.llm_verdict
            and "error" not in self.llm_verdict
            and self.llm_content_hash == job.content_hash
        )

    __table_args__ = (
        UniqueConstraint("job_id", "profile_version", name="uq_job_matches_job_profile"),
        Index("ix_job_matches_profile_score", "profile_version", "score"),
        Index("ix_job_matches_prefs", "profile_version", "prefs_pass", "score"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobMatch job={self.job_id} v={self.profile_version} score={self.score}>"


class GeneratedDocument(Base):
    """A tailored résumé for one job, as LaTeX, plus how it got that way.

    Keyed on (`job_id`, `profile_version`, `kind`) exactly as CLAUDE.md's
    Stage 3 schema specifies. `profile_version` is in the key for the same
    reason it is in `job_matches`: a résumé tailored against last week's
    profile is a different document, and showing it as current is the failure
    the version hash exists to prevent.

    Why the LaTeX and not the PDF
    -----------------------------
    `tex` is the source of truth and the thing the editor round-trips. The PDF
    is a pure function of it (`app/latex.py`), reproducible in ~3.5s, and
    storing megabytes of binary per job to save that is a bad trade. The one
    PDF that matters is the one the user downloads, and it is compiled from
    exactly the `tex` on screen.

    `edits` and `tailoring` are kept apart on purpose. `tailoring` is what the
    model said, verbatim, including rewrites the fact check then threw away.
    `edits` is what was actually applied. Keeping only the second would make
    "why is this bullet unchanged?" unanswerable.

    `is_draft` never flips in this phase. CLAUDE.md: *generated documents are
    always drafts for review; no send or submit path exists in this phase.*
    """

    __tablename__ = "generated_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="resume")

    tex: Mapped[str] = mapped_column(Text, nullable=False)
    # The JD this was tailored against, so a re-posted description shows as
    # stale — the same cache key the deep read uses for its fit half.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # Which revision of the prompt produced it; see `TAILOR_PROMPT_VERSION`.
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    edits: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tailoring: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # `factcheck.FactIssue` rows: rewrites rejected for an unsupported metric,
    # and terms flagged for a human to look at.
    issues: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # The refinement conversation (`app/resume_chat.py`): messages, and on each
    # assistant turn the proposal it made and whether it was applied. Reset on
    # regenerate — its region ids describe the résumé it was written against.
    chat: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    llm_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    llm_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # True once the user has saved their own edits over the generated text.
    hand_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "job_id", "profile_version", "kind", name="uq_generated_documents_job_profile_kind"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<GeneratedDocument job={self.job_id} kind={self.kind} v={self.profile_version}>"


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


class LlmUsage(Base):
    """One billed Groq call — the ledger the daily limits are enforced from.

    Groq's free tier caps a model at 200,000 tokens and 1,000 requests a DAY
    (https://console.groq.com/docs/rate-limits), but its response headers only
    ever describe requests-per-day and tokens-per-MINUTE. Nothing on the wire
    says how much of today's token allowance is left, so it has to be counted
    here — and persisted, because a pass that restarts with an in-memory count
    of zero walks straight into the cap. On 2026-09-13 a pass reached ~207k
    tokens and from then on every row failed on 429s.

    A rejected (429) call is not written. Observed on that run: after ~120
    billed calls and dozens of 429s, `x-ratelimit-remaining-requests` read
    890 of 1,000 — so rejections are not counted against the day.
    """

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_llm_usage_model_created", "model", "created_at"),)
