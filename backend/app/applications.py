"""Application tracking, and the numbers the Dashboard shows.

Two jobs in one module, because the second is almost entirely a read of the
first plus things other modules already count.

What "applied" means here
-------------------------
Clicking Apply in the dashboard records an application. That is a *click on the
Apply button*, not proof a form was submitted, and `source="apply_click"` keeps
the difference visible rather than laundering it into an assertion. It is the
right default anyway: the JD is already in the drawer, so reaching for Apply
means intent, and a tracker that needs discipline to stay accurate ends up
empty and therefore useless. Every status is editable, and `DELETE` undoes a
misclick outright.

Why silence is computed, never stored
-------------------------------------
"No reply in 30 days" is derived from `status_changed_at` on every read. That
keeps it correct with no background job, and — more importantly — keeps every
*stored* status something the user asserted. The Dashboard offers to mark those
rows `ghosted`; it does not do it for them. Counts you can argue with are worth
more than counts that drift on a timer.

It measures from `status_changed_at` rather than `applied_at` because a reply
followed by silence restarts the clock. Measuring from the application date
would call an active interview process stale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.models import Application, GeneratedDocument, IngestRun, JobMatch, JobPosting, LlmUsage

log = logging.getLogger(__name__)

STATUSES: Final[tuple[str, ...]] = Application.STATUSES
CLOSED: Final[frozenset[str]] = Application.CLOSED
# The one status that means "sent, nothing back yet" — what the Dashboard calls
# awaiting.
AWAITING: Final[str] = "applied"

# How long an application sits at one status before the Dashboard suggests it
# has gone quiet. 30 days matches the board's own posting-age limit: past that
# the posting itself is stale, so silence has almost certainly become a no.
SILENT_AFTER_DAYS: Final[int] = 30

# Weeks of history in the activity chart. A quarter is long enough to show a
# trend and short enough to read at a glance.
ACTIVITY_WEEKS: Final[int] = 12

SOURCES: Final[tuple[str, ...]] = ("apply_click", "manual")


class ApplicationError(RuntimeError):
    """The request cannot be satisfied. The message is shown to the user."""


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


async def get_application(session: AsyncSession, job_id: int) -> Application | None:
    return await session.scalar(select(Application).where(Application.job_id == job_id))


async def mark_applied(
    session: AsyncSession, job_id: int, *, source: str = "manual"
) -> tuple[Application, bool]:
    """Record an application for this job. Idempotent.

    Returns `(application, created)`. A second call does **not** reset the
    status or the date: clicking Apply again to re-read the form must not undo
    "interviewing" or restamp a three-week-old application as today's.
    """
    if source not in SOURCES:
        raise ApplicationError(f"unknown source {source!r}")
    job = await session.get(JobPosting, job_id)
    if job is None:
        raise ApplicationError(f"job {job_id} not found")

    existing = await get_application(session, job_id)
    if existing is not None:
        return existing, False

    now = _now()
    application = Application(
        job_id=job_id,
        status=AWAITING,
        applied_at=now,
        status_changed_at=now,
        source=source,
        history=[{"status": AWAITING, "at": now.isoformat()}],
        created_at=now,
        updated_at=now,
    )
    session.add(application)
    await session.commit()
    await session.refresh(application)
    log.info(
        "applications.marked",
        extra={"job_id": job_id, "source": source, "company": job.company},
    )
    return application, True


async def set_status(
    session: AsyncSession,
    job_id: int,
    *,
    status: str | None = None,
    notes: str | None = None,
) -> Application:
    """Move an application along, and/or edit its notes."""
    if status is not None and status not in STATUSES:
        raise ApplicationError(
            f"unknown status {status!r} — expected one of {', '.join(STATUSES)}"
        )
    application = await get_application(session, job_id)
    if application is None:
        raise ApplicationError("no application recorded for this job")

    now = _now()
    if status is not None and status != application.status:
        application.status = status
        application.status_changed_at = now
        # Reassign, don't append: SQLAlchemy's JSON type only notices a new value.
        application.history = [
            *(application.history or []),
            {"status": status, "at": now.isoformat()},
        ]
    if notes is not None:
        application.notes = notes.strip() or None
    application.updated_at = now
    await session.commit()
    await session.refresh(application)
    return application


async def unmark(session: AsyncSession, job_id: int) -> bool:
    """Undo — delete the application entirely. True if there was one.

    A hard delete, not a status: an accidental Apply click should leave no trace
    in the counts, and "I did not apply" is the absence of an application rather
    than a stage of one.
    """
    application = await get_application(session, job_id)
    if application is None:
        return False
    await session.delete(application)
    await session.commit()
    log.info("applications.unmarked", extra={"job_id": job_id})
    return True


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

# The JD bodies and the board payload are never part of a list response.
_LIST_DEFERRED = (
    defer(JobPosting.raw_json),
    defer(JobPosting.description_html),
    defer(JobPosting.description_text),
)


def _rows_stmt() -> Select[Any]:
    return (
        select(Application, JobPosting)
        .join(JobPosting, JobPosting.id == Application.job_id)
        .options(*_LIST_DEFERRED)
    )


def days_silent(application: Application, *, now: datetime | None = None) -> int:
    return max(0, ((now or _now()) - application.status_changed_at).days)


def is_silent(application: Application, *, now: datetime | None = None) -> bool:
    """Open, and nothing has moved for `SILENT_AFTER_DAYS`."""
    if application.status in CLOSED:
        return False
    return days_silent(application, now=now) >= SILENT_AFTER_DAYS


async def list_applications(
    session: AsyncSession,
    *,
    statuses: list[str] | None = None,
    silent_only: bool = False,
    limit: int = 200,
) -> list[tuple[Application, JobPosting]]:
    """Applications newest-moved first, with their postings."""
    stmt = _rows_stmt().order_by(Application.status_changed_at.desc())
    if statuses:
        unknown = [s for s in statuses if s not in STATUSES]
        if unknown:
            raise ApplicationError(f"unknown status: {', '.join(unknown)}")
        stmt = stmt.where(Application.status.in_(statuses))
    rows = [(a, j) for a, j in (await session.execute(stmt.limit(limit))).all()]
    if silent_only:
        now = _now()
        rows = [(a, j) for a, j in rows if is_silent(a, now=now)]
    return rows


# --------------------------------------------------------------------------
# The Dashboard
# --------------------------------------------------------------------------


@dataclass(slots=True)
class WeekPoint:
    """One week of the activity chart. `week` is the Monday, ISO date."""

    week: str
    applications: int
    jobs_found: int


@dataclass(slots=True)
class DashboardData:
    # Applications
    by_status: dict[str, int] = field(default_factory=dict)
    total_applications: int = 0
    awaiting: int = 0
    active: int = 0
    silent: int = 0
    applied_last_7d: int = 0
    applied_last_30d: int = 0
    response_rate: float | None = None
    # Pipeline health
    jobs_open: int = 0
    jobs_eligible: int = 0
    jobs_fresh: int = 0
    matches_scored: int = 0
    matches_shortlisted: int = 0
    documents: int = 0
    last_sweep_at: datetime | None = None
    last_sweep_fetched: int = 0
    # LLM budget
    provider: str = ""
    model: str = ""
    tokens_today: int = 0
    tokens_per_day: int | None = None
    requests_today: int = 0
    requests_per_day: int | None = None
    deep_reads: int = 0
    # Activity
    activity: list[WeekPoint] = field(default_factory=list)


async def _count(session: AsyncSession, stmt: Select[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


async def dashboard(session: AsyncSession, *, settings: Any | None = None) -> DashboardData:
    """Everything the Dashboard shows, in one round of queries.

    One call rather than six endpoints: the panel is useless half-populated, and
    a single payload means the numbers on screen are all as of the same instant.
    """
    from app.config import get_settings
    from app.job_filters import MAX_POSTING_AGE_DAYS

    settings = settings or get_settings()
    now = _now()
    data = DashboardData()

    # --- Applications ----------------------------------------------------
    rows = (
        await session.execute(
            select(Application.status, func.count()).group_by(Application.status)
        )
    ).all()
    counts = {status: int(n) for status, n in rows}
    data.by_status = {status: counts.get(status, 0) for status in STATUSES}
    data.total_applications = sum(data.by_status.values())
    data.awaiting = data.by_status.get(AWAITING, 0)
    data.active = sum(n for s, n in data.by_status.items() if s not in CLOSED)

    open_applications = (
        await session.scalars(select(Application).where(Application.status.notin_(list(CLOSED))))
    ).all()
    data.silent = sum(1 for a in open_applications if is_silent(a, now=now))

    for days, attr in ((7, "applied_last_7d"), (30, "applied_last_30d")):
        setattr(
            data,
            attr,
            await _count(
                session,
                select(Application.id).where(Application.applied_at >= now - timedelta(days=days)),
            ),
        )

    # Anything that ever left `applied` got a human response. Computed off
    # `history` rather than the current status, so a rejection after an
    # interview still counts as a response — which is what the number is for.
    if data.total_applications:
        responded = 0
        for application in (await session.scalars(select(Application))).all():
            trail = [entry.get("status") for entry in (application.history or [])]
            if any(status and status != AWAITING for status in trail):
                responded += 1
        data.response_rate = round(responded / data.total_applications, 3)

    # --- Pipeline health -------------------------------------------------
    open_jobs = select(JobPosting.id).where(JobPosting.status == "open")
    data.jobs_open = await _count(session, open_jobs)
    data.jobs_eligible = await _count(session, open_jobs.where(JobPosting.eligibility_pass.is_(True)))
    data.jobs_fresh = await _count(
        session,
        open_jobs.where(
            JobPosting.eligibility_pass.is_(True),
            JobPosting.posted_at >= now - timedelta(days=MAX_POSTING_AGE_DAYS),
        ),
    )
    data.matches_scored = await _count(session, select(JobMatch.id))
    data.matches_shortlisted = await _count(
        session, select(JobMatch.id).where(JobMatch.prefs_pass.is_(True))
    )
    data.documents = await _count(session, select(GeneratedDocument.id))

    last_run = await session.scalar(
        select(IngestRun)
        .where(IngestRun.finished_at.isnot(None))
        .order_by(IngestRun.finished_at.desc())
        .limit(1)
    )
    if last_run is not None:
        data.last_sweep_at = last_run.finished_at
        data.last_sweep_fetched = last_run.fetched or 0

    # --- LLM budget ------------------------------------------------------
    provider = settings.llm
    data.provider = provider.name
    data.model = provider.model
    data.tokens_per_day = provider.limits.get("tokens_per_day")
    data.requests_per_day = provider.limits.get("requests_per_day")

    # The ledger is per provider *model*, which is what the cap applies to.
    # A day here is the provider's rolling 24h, matching `ratelimit.py`.
    since = now - timedelta(days=1)
    billed = LlmUsage.status_code < 400
    data.tokens_today = int(
        await session.scalar(
            select(func.coalesce(func.sum(LlmUsage.total_tokens), 0)).where(
                LlmUsage.created_at >= since, LlmUsage.model == provider.model, billed
            )
        )
        or 0
    )
    data.requests_today = await _count(
        session,
        select(LlmUsage.id).where(
            LlmUsage.created_at >= since, LlmUsage.model == provider.model, billed
        ),
    )
    data.deep_reads = await _count(
        session, select(JobMatch.id).where(JobMatch.llm_used.is_(True))
    )

    # --- Activity --------------------------------------------------------
    data.activity = await _activity(session, now=now)
    return data


def _monday(moment: datetime) -> datetime:
    start = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=start.weekday())


async def _activity(session: AsyncSession, *, now: datetime) -> list[WeekPoint]:
    """Applications sent and jobs found, per week, oldest first.

    Bucketed in Python rather than SQL: SQLite has no `date_trunc`, and doing it
    with `strftime` would make this the one query that breaks on the Postgres
    move the stack rules exist to keep cheap.
    """
    first = _monday(now) - timedelta(weeks=ACTIVITY_WEEKS - 1)
    buckets: dict[str, dict[str, int]] = {
        (first + timedelta(weeks=i)).date().isoformat(): {"applications": 0, "jobs_found": 0}
        for i in range(ACTIVITY_WEEKS)
    }

    applied = (
        await session.scalars(select(Application.applied_at).where(Application.applied_at >= first))
    ).all()
    found = (
        await session.scalars(
            select(JobPosting.first_seen_at).where(JobPosting.first_seen_at >= first)
        )
    ).all()

    for stamps, key in ((applied, "applications"), (found, "jobs_found")):
        for stamp in stamps:
            if stamp is None:
                continue
            week = _monday(stamp).date().isoformat()
            if week in buckets:
                buckets[week][key] += 1

    return [
        WeekPoint(week=week, applications=v["applications"], jobs_found=v["jobs_found"])
        for week, v in sorted(buckets.items())
    ]
