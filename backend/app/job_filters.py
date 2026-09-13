"""The Jobs tile's filters — one definition, used by every surface that counts.

Why this module exists
----------------------
The dashboard showed four numbers that could not be reconciled: 5,481 on the
header, 740 with Remote on, "430 of 910 eligible" after matching, and a deep
read quoting 198 while reading 427. Each came from a different query. The
cure is that every count on screen — the Jobs list, its facets, the funnel,
and the scope a user transfers into Matches — is built by `apply_job_filters`
over one `JobFilterSet`, so two numbers that should agree cannot drift.

"Remote" means one thing
------------------------
The Jobs toggle used to read `job_postings.remote` (a keyword scan) while the
Matches preference read `workplace_type` (the board's own field). They agreed
to within two rows on the live board, but only by coincidence. Both now use
`IS_REMOTE`: the board's `workplace_type` when it published one, else the
keyword flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Select, case, func, not_, or_

from app.models import JobPosting, utcnow

# Postings older than this are no use to me — a hard limit, not a preference.
# The Jobs tile defaults to it and Matches can never be widened past it.
MAX_POSTING_AGE_DAYS = 30

IS_REMOTE = case(
    (JobPosting.workplace_type.is_not(None), JobPosting.workplace_type == "remote"),
    else_=JobPosting.remote,
)


class JobFilterSet(BaseModel):
    """What the Jobs tile is filtering by — also the scope sent to Matches.

    Field names mirror the `/jobs` query parameters, so a transferred scope
    reads the same as the URL that produced it.
    """

    model_config = ConfigDict(frozen=True)

    company: list[str] = Field(default_factory=list)
    ats: list[str] = Field(default_factory=list)
    department: list[str] = Field(default_factory=list)
    remote: bool | None = None
    q: str | None = None
    q_scope: Literal["all", "title"] = "all"
    posted_within_days: int | None = Field(default=None, ge=1, le=365)
    first_seen_after: datetime | None = None
    eligibility_pass: bool | None = None
    min_validity: int | None = Field(default=None, ge=0, le=100)

    @field_validator("company", "ats", "department")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v and v.strip()]

    @field_validator("q")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    def describe(self) -> list[str]:
        """Human labels for the active filters, for the scope banner."""
        out: list[str] = []
        if self.eligibility_pass:
            out.append("My roles")
        if self.posted_within_days:
            out.append(f"Posted ≤ {self.posted_within_days}d")
        if self.remote is not None:
            out.append("Remote" if self.remote else "Not remote")
        if self.min_validity:
            out.append(f"Validity ≥ {self.min_validity}")
        out.extend(self.company)
        out.extend(a.title() for a in self.ats)
        out.extend(self.department)
        if self.q:
            out.append(f"“{self.q}”" + (" in title" if self.q_scope == "title" else ""))
        if self.first_seen_after:
            out.append("New since last visit")
        return out


def apply_job_filters(
    stmt: Select,
    filters: JobFilterSet | None = None,
    *,
    status: str = "open",
) -> Select:
    """Narrow `stmt` (anything selecting from `job_postings`) by `filters`.

    If this and the counts built on it ever diverge, facet counts stop
    matching the result list — the quickest way to make a dashboard feel
    broken.
    """
    f = filters or JobFilterSet()
    if status != "any":
        stmt = stmt.where(JobPosting.status == status)
    if f.company:
        stmt = stmt.where(
            or_(*(func.lower(JobPosting.company) == c.lower() for c in f.company))
        )
    if f.ats:
        stmt = stmt.where(JobPosting.ats.in_([a.lower() for a in f.ats]))
    if f.remote is not None:
        stmt = stmt.where(IS_REMOTE if f.remote else not_(IS_REMOTE))
    if f.eligibility_pass is not None:
        stmt = stmt.where(JobPosting.eligibility_pass.is_(f.eligibility_pass))
    if f.min_validity:
        # A row the validity pass has never scored (NULL) passes. Treating
        # NULL as 0 would empty the table on a fresh DB the moment anyone
        # touched this filter — "not yet checked" is not "worthless".
        stmt = stmt.where(
            or_(
                JobPosting.validity_score >= f.min_validity,
                JobPosting.validity_score.is_(None),
            )
        )
    if f.department:
        stmt = stmt.where(
            or_(*(func.lower(JobPosting.department) == d.lower() for d in f.department))
        )
    if f.posted_within_days is not None:
        stmt = stmt.where(posted_within(f.posted_within_days))
    if f.first_seen_after is not None:
        moment = f.first_seen_after
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        stmt = stmt.where(JobPosting.first_seen_at > moment)
    if f.q:
        pattern = f"%{f.q}%"
        if f.q_scope == "title":
            stmt = stmt.where(JobPosting.title.ilike(pattern))
        else:
            stmt = stmt.where(
                or_(
                    JobPosting.title.ilike(pattern),
                    JobPosting.company.ilike(pattern),
                    JobPosting.description_text.ilike(pattern),
                )
            )
    return stmt


def posted_within(days: int, now: datetime | None = None):
    """Posted in the last `days`, falling back to first-seen when undated.

    Every board dates its postings today, so the fallback is a guard rather
    than a live path — but a row with no date must not become immortal.
    """
    cutoff = (now or utcnow()) - timedelta(days=days)
    return or_(
        JobPosting.posted_at >= cutoff,
        (JobPosting.posted_at.is_(None)) & (JobPosting.first_seen_at >= cutoff),
    )


def _stamp(job: JobPosting) -> datetime | None:
    stamp = job.posted_at or job.first_seen_at
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def posting_age_days(job: JobPosting, now: datetime | None = None) -> int:
    """Whole days since the posting was published (or first seen, if undated)."""
    stamp = _stamp(job)
    if stamp is None:
        return 0
    return max(0, int(((now or utcnow()) - stamp).total_seconds() // 86400))


def is_older_than(job: JobPosting, days: int, now: datetime | None = None) -> bool:
    """The Python twin of `not posted_within(days)` — the same exact cutoff."""
    stamp = _stamp(job)
    return stamp is not None and stamp < (now or utcnow()) - timedelta(days=days)


FUNNEL_ORDER: tuple[tuple[str, str], ...] = (
    ("eligibility_pass", "Eligible (My roles)"),
    ("posted_within_days", "Posted recently"),
    ("remote", "Work mode"),
    ("min_validity", "Validity"),
    ("company", "Company"),
    ("ats", "Platform"),
    ("department", "Department"),
    ("q", "Keyword"),
    ("first_seen_after", "New since last visit"),
)


def funnel_prefixes(filters: JobFilterSet) -> list[tuple[str, str, JobFilterSet]]:
    """The active filters, applied one at a time in `FUNNEL_ORDER`.

    Each entry is (key, label, the filter set with that step and every step
    before it applied). Counting each prefix gives a funnel whose last number
    is exactly the Jobs list's total, because it IS the same filter set.
    """
    steps: list[tuple[str, str, JobFilterSet]] = []
    applied: dict[str, object] = {"q_scope": filters.q_scope}
    for key, label in FUNNEL_ORDER:
        value = getattr(filters, key)
        if value in (None, [], "", False) and not (key == "remote" and value is False):
            continue
        applied[key] = value
        if key == "posted_within_days":
            label = f"Posted ≤ {value}d"
        elif key == "remote":
            label = "Remote" if value else "Not remote"
        elif key == "min_validity":
            label = f"Validity ≥ {value}"
        steps.append((key, label, JobFilterSet(**applied)))
    return steps
