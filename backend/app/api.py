"""FastAPI routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import supported_ats
from app.companies import CompanyConfigError, load_companies
from app.config import get_settings
from app.db import get_session
from app.ingest import is_fresh, run_ingest
from app.ingest_state import RunProgress, tracker
from app.models import IngestRun, JobPosting, utcnow
from app.schemas import (
    IngestRunOut,
    IngestStatusOut,
    JobDetailOut,
    JobListOut,
    JobOut,
    SourceProgressOut,
)

log = logging.getLogger(__name__)

router = APIRouter()

# Serializes manual, scheduled and page-load runs so two ingests never
# interleave writes. The lock lives on the tracker because the background
# refresh path needs to observe it as well as hold it.
_ingest_lock = tracker.lock

SortField = Literal["posted_at", "first_seen_at", "last_seen_at", "title", "company"]


def _apply_filters(
    stmt: Select,
    *,
    company: list[str] | None = None,
    ats: list[str] | None = None,
    remote: bool | None = None,
    status: str = "open",
    q: str | None = None,
    q_scope: str = "all",
    department: list[str] | None = None,
    posted_within_days: int | None = None,
    first_seen_after: datetime | None = None,
    eligibility_pass: bool | None = None,
) -> Select:
    """Shared filter builder so /jobs and /meta/facets stay consistent.

    If these ever diverge, facet counts stop matching the result list — the
    quickest way to make a dashboard feel broken.
    """
    if status != "any":
        stmt = stmt.where(JobPosting.status == status)
    if company:
        stmt = stmt.where(
            or_(*(func.lower(JobPosting.company) == c.strip().lower() for c in company))
        )
    if ats:
        stmt = stmt.where(JobPosting.ats.in_([a.strip().lower() for a in ats]))
    if remote is not None:
        stmt = stmt.where(JobPosting.remote.is_(remote))
    if eligibility_pass is not None:
        stmt = stmt.where(JobPosting.eligibility_pass.is_(eligibility_pass))
    if department:
        stmt = stmt.where(
            or_(*(func.lower(JobPosting.department) == d.strip().lower() for d in department))
        )
    if posted_within_days is not None:
        cutoff = utcnow() - timedelta(days=posted_within_days)
        stmt = stmt.where(JobPosting.posted_at.is_not(None), JobPosting.posted_at >= cutoff)
    if first_seen_after is not None:
        moment = (
            first_seen_after
            if first_seen_after.tzinfo
            else first_seen_after.replace(tzinfo=UTC)
        )
        stmt = stmt.where(JobPosting.first_seen_at > moment)
    if q:
        pattern = f"%{q.strip()}%"
        if q_scope == "title":
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


@router.get("/health", tags=["meta"])
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, object]:
    total = await session.scalar(select(func.count()).select_from(JobPosting))
    open_jobs = await session.scalar(
        select(func.count()).select_from(JobPosting).where(JobPosting.status == "open")
    )
    last_run = await session.scalar(select(func.max(IngestRun.finished_at)))
    return {
        "status": "ok",
        "jobs_total": total or 0,
        "jobs_open": open_jobs or 0,
        "last_ingest_finished_at": last_run,
        "supported_ats": supported_ats(),
    }


@router.get("/companies", tags=["meta"])
async def list_companies() -> list[dict[str, object]]:
    try:
        configs = load_companies(include_disabled=True)
    except CompanyConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [
        {
            "company": c.company,
            "ats": c.ats,
            "token_or_slug": c.token_or_slug,
            "enabled": c.enabled,
            "source_id": c.source_id,
        }
        for c in configs
    ]


@router.get("/jobs", response_model=JobListOut, tags=["jobs"])
async def list_jobs(
    session: Annotated[AsyncSession, Depends(get_session)],
    company: Annotated[list[str] | None, Query(description="Repeatable company filter")] = None,
    ats: Annotated[list[str] | None, Query(description="Repeatable ATS filter")] = None,
    remote: bool | None = None,
    status: Annotated[Literal["open", "closed", "any"], Query()] = "open",
    q: Annotated[str | None, Query(description="Keyword search")] = None,
    q_scope: Annotated[
        Literal["all", "title"],
        Query(description="'all' searches title+company+description; 'title' is precise"),
    ] = "all",
    department: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    posted_within_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    first_seen_after: Annotated[
        datetime | None, Query(description="Only jobs discovered after this instant")
    ] = None,
    eligibility_pass: Annotated[
        bool | None,
        Query(description="true = only jobs that cleared the eligibility filter"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: SortField = "first_seen_at",
    order: Literal["asc", "desc"] = "desc",
) -> JobListOut:
    stmt = _apply_filters(
        select(JobPosting),
        company=company,
        ats=ats,
        remote=remote,
        status=status,
        q=q,
        q_scope=q_scope,
        department=department,
        posted_within_days=posted_within_days,
        first_seen_after=first_seen_after,
        eligibility_pass=eligibility_pass,
    )

    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    )

    sort_column = getattr(JobPosting, sort)
    stmt = stmt.order_by(sort_column.desc() if order == "desc" else sort_column.asc())

    # A bulk first ingest stamps every row with an identical `first_seen_at`,
    # so an id-only tie-break would show one company's jobs and nothing else.
    # Falling back to `posted_at` keeps the default view a real mixed feed.
    if sort != "posted_at":
        stmt = stmt.order_by(JobPosting.posted_at.desc())
    # Final tie-break on id so pagination can never repeat or skip a row.
    stmt = stmt.order_by(JobPosting.id.desc()).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).scalars().all()
    return JobListOut(
        total=total or 0,
        limit=limit,
        offset=offset,
        items=[JobOut.model_validate(row) for row in rows],
    )


@router.get("/meta/facets", tags=["meta"])
async def facets(
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[Literal["open", "closed", "any"], Query()] = "open",
    since: Annotated[
        datetime | None, Query(description="Instant used for the 'new since' count")
    ] = None,
    eligibility_pass: Annotated[
        bool | None,
        Query(description="true = count only jobs that cleared the eligibility filter"),
    ] = None,
) -> dict[str, object]:
    """Filter options with counts, plus headline stats.

    One request backs every dropdown in the dashboard, so the UI never has to
    guess which companies or departments actually have jobs.

    `eligibility_pass` must be threaded through here and not only into /jobs:
    the dashboard hides ineligible rows by default, and a facet list built
    without the same gate offers companies whose every posting is filtered out.
    Selecting one then yields an empty table, which reads as a broken filter.
    """

    async def grouped(column) -> list[dict[str, object]]:
        stmt = _apply_filters(
            select(column, func.count().label("n")),
            status=status,
            eligibility_pass=eligibility_pass,
        ).where(column.is_not(None))
        rows = await session.execute(
            stmt.group_by(column).order_by(func.count().desc(), column.asc())
        )
        return [{"value": value, "count": count} for value, count in rows if value]

    base = _apply_filters(
        select(func.count()).select_from(JobPosting),
        status=status,
        eligibility_pass=eligibility_pass,
    )
    total = await session.scalar(base) or 0
    remote_count = await session.scalar(base.where(JobPosting.remote.is_(True))) or 0

    # Deliberately NOT derived from `base`. With the gate on, `base` already
    # excludes every ineligible row, so counting within it would report
    # ineligible = 0 and make the split describe the query rather than the
    # board. This pair always answers "of the jobs at this status, how many
    # clear the filter?", which is the only reading that stays useful.
    split_base = _apply_filters(select(func.count()).select_from(JobPosting), status=status)
    split_total = await session.scalar(split_base) or 0
    eligible_count = (
        await session.scalar(split_base.where(JobPosting.eligibility_pass.is_(True))) or 0
    )

    week_ago = utcnow() - timedelta(days=7)
    posted_week = (
        await session.scalar(
            base.where(JobPosting.posted_at.is_not(None), JobPosting.posted_at >= week_ago)
        )
        or 0
    )

    new_since = 0
    if since is not None:
        moment = since if since.tzinfo else since.replace(tzinfo=UTC)
        new_since = await session.scalar(base.where(JobPosting.first_seen_at > moment)) or 0

    open_total = (
        await session.scalar(
            select(func.count()).select_from(JobPosting).where(JobPosting.status == "open")
        )
        or 0
    )
    closed_total = (
        await session.scalar(
            select(func.count()).select_from(JobPosting).where(JobPosting.status == "closed")
        )
        or 0
    )
    last_ingest = await session.scalar(select(func.max(IngestRun.finished_at)))

    return {
        "companies": await grouped(JobPosting.company),
        "ats": await grouped(JobPosting.ats),
        "departments": await grouped(JobPosting.department),
        "currencies": await grouped(JobPosting.salary_currency),
        "totals": {
            "matching": total,
            "open": open_total,
            "closed": closed_total,
            "remote": remote_count,
            "eligible": eligible_count,
            "ineligible": split_total - eligible_count,
            "posted_last_7d": posted_week,
            "new_since": new_since,
        },
        "last_ingest_finished_at": last_ingest,
    }


@router.get("/jobs/{job_id}", response_model=JobDetailOut, tags=["jobs"])
async def get_job(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    include_raw: bool = False,
) -> JobDetailOut:
    job = await session.get(JobPosting, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    payload = JobDetailOut.model_validate(job)
    if not include_raw:
        payload.raw_json = None
    return payload


@router.post("/ingest/run", response_model=IngestRunOut, tags=["ingest"])
async def trigger_ingest(
    notify: Annotated[bool, Query(description="Send Telegram alerts for new jobs")] = True,
) -> IngestRunOut:
    if tracker.busy:
        raise HTTPException(status_code=409, detail="an ingest run is already in progress")
    async with _ingest_lock:
        try:
            return await run_ingest(notify=notify, settings=get_settings())
        except CompanyConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _build_status(
    session: AsyncSession,
    *,
    state: Literal["fresh", "running", "idle"] | None = None,
    skipped: bool = False,
    reason: str | None = None,
) -> IngestStatusOut:
    """Snapshot the tracker, plus the durable 'last run ever' from the DB.

    `last_finished_at` comes from `ingest_runs` rather than the tracker so the
    same-day check still works after a restart, when nothing is in memory.
    """
    last_finished = await session.scalar(select(func.max(IngestRun.finished_at)))
    resolved = state or ("running" if tracker.busy else "idle")
    progress: RunProgress | None = None if resolved == "fresh" else tracker.progress

    return IngestStatusOut(
        state=resolved,
        skipped=skipped,
        reason=reason,
        run_id=progress.run_id if progress else None,
        started_at=progress.started_at if progress else None,
        finished_at=progress.finished_at if progress else None,
        last_finished_at=last_finished,
        sources_total=progress.total if progress else 0,
        sources_done=progress.done if progress else 0,
        sources=[
            SourceProgressOut(
                source_id=entry.source_id,
                company=entry.company,
                ats=entry.ats,
                state=entry.state,
                fetched=entry.fetched,
                new=entry.new,
                eligible=entry.eligible,
                error=entry.error,
            )
            for entry in (progress.sources if progress else [])
        ],
        result=tracker.result,
        error=tracker.error,
    )


@router.post("/ingest/refresh", response_model=IngestStatusOut, tags=["ingest"])
async def refresh_ingest(
    session: Annotated[AsyncSession, Depends(get_session)],
    fresh_since: Annotated[
        datetime | None,
        Query(
            description=(
                "Skip the sweep when the last run finished at or after this instant. "
                "Defaults to now minus `refresh_window_hours` (24), so the boards are "
                "contacted at most once a day and every later visit renders straight "
                "from storage. The dashboard sends nothing and takes that default."
            )
        ),
    ] = None,
    notify: Annotated[
        bool, Query(description="Send Telegram alerts for jobs this run discovers")
    ] = False,
    force: Annotated[bool, Query(description="Sweep even if the data is already fresh")] = False,
) -> IngestStatusOut:
    """Start a sweep of every configured board, unless one is not needed.

    Returns immediately in all three cases — the run itself continues in the
    background and is followed with `GET /ingest/status`. Holding the request
    open for the length of a real sweep (Wellfound alone can take a minute)
    would give the caller a timeout instead of an answer.
    """
    if not force:
        # Freshness is checked *before* the busy check on purpose. A page load
        # that lands while the scheduler happens to be sweeping still has fresh
        # stored data, and blocking its first paint behind an hour-long tick is
        # exactly the "it fetches every time I refresh" symptom.
        cutoff = fresh_since
        if cutoff is not None and cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        last_finished = await is_fresh(session, cutoff)
        if last_finished is not None:
            return await _build_status(
                session,
                state="fresh",
                skipped=True,
                reason=f"last sweep finished {last_finished.isoformat()}",
            )

    if tracker.busy:
        # Someone got here first — the scheduler, another tab, or a reload
        # mid-run. Attach to that run rather than queueing a second sweep.
        return await _build_status(
            session, state="running", reason="a sweep was already in progress"
        )

    settings = get_settings()
    tracker.start(
        lambda progress: run_ingest(notify=notify, settings=settings, progress=progress)
    )
    log.info("ingest.refresh_started", extra={"notify": notify, "forced": force})
    return await _build_status(session, state="running", reason="sweep started")


@router.get("/ingest/status", response_model=IngestStatusOut, tags=["ingest"])
async def ingest_status(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IngestStatusOut:
    """Poll target for a refresh in flight; `state` leaves 'running' when done."""
    return await _build_status(session)


@router.get("/ingest/runs", tags=["ingest"])
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, object]]:
    rows = (
        (await session.execute(select(IngestRun).order_by(IngestRun.id.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return [
        {
            "run_id": row.id,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "fetched": row.fetched,
            "new": row.new,
            "updated": row.updated,
            "closed": row.closed,
            "errored": row.errored,
            "notified": row.notified,
            "sources": row.sources or [],
        }
        for row in rows
    ]
