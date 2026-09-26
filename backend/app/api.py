"""FastAPI routes."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import defer
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import supported_ats
from app.companies import CompanyConfigError, load_companies
from app.config import get_settings
from app.db import get_session, session_scope
from app.documents import get_job as require_job  # `get_job` is taken by the /jobs/{id} route
from app.documents import (
    COVER_LETTER,
    DocumentError,
    compile_document,
    diff_against_base,
    generate,
    get_document,
    hand_edit_warnings,
    is_stale,
    revert,
    save_tex,
    tex_hash,
)
from app.funnel import jobs_funnel, matches_funnel
from app.latex import LatexUnavailable
from app.llm import LLMError, LLMNotConfigured, LLMRateLimited
from app.ingest import is_fresh, run_ingest
from app.ingest_state import RunProgress, tracker
from app.job_filters import IS_REMOTE, JobFilterSet, apply_job_filters
from app.llm_runner import LLMProgress, LLMRunResult, run_llm_screen
from app.llm_runner import tracker as llm_tracker
from app.match_runner import MatchRunResult, run_matching
from app.matching import get_weights
from app.models import GeneratedDocument, IngestRun, JobMatch, JobPosting, utcnow
from app.normalize import detect_currency, find_usd_pay
from app.pipeline import LLMEstimate, PipelineProgress, estimate_llm_pass, run_pipeline
from app.pipeline import tracker as pipeline_tracker
from app.prefs import Prefs, PrefsError, TransferPrefs, get_prefs, load_prefs, save_prefs
from app.profile import ProfileError, get_profile
from app import profile_intake
from app.resume_tex import ResumeDocument, ResumeTemplateError
from app import cover_chat, resume_chat
from app.shortlist import HARD_MAX_READS, reasons_excluded, shortlist_clause
from app.validation_runner import ValidityRunResult, run_validation
from app.validation_runner import _band as validity_band
from app.schemas import (
    BaseResumeOut,
    CompileOut,
    ChatApplyIn,
    ChatApplyOut,
    ChatIn,
    ChatOut,
    DiffRowOut,
    DocumentDiffOut,
    DocumentOut,
    DocumentSaveIn,
    FactIssueOut,
    IngestRunOut,
    IngestStatusOut,
    JobDetailOut,
    JobListOut,
    JobOut,
    LLMEstimateOut,
    LLMRunOut,
    LLMStatusOut,
    MatchListOut,
    MatchOut,
    MatchRunOut,
    PipelineStatusOut,
    PrefsIn,
    PrefsOut,
    ProfileDocumentOut,
    ProfileDraftOut,
    ProfileOut,
    ProfileSaveIn,
    SourceProgressOut,
    ValidityRunOut,
)
from app.prefs import EMPLOYMENT_TYPES, WORKPLACE_TYPES

log = logging.getLogger(__name__)

router = APIRouter()

# Serializes manual, scheduled and page-load runs so two ingests never
# interleave writes. The lock lives on the tracker because the background
# refresh path needs to observe it as well as hold it.
_ingest_lock = tracker.lock

# Upload ceiling for the profile's own files. A résumé .tex is ~7KB and its
# PDF ~200KB; this is slack, not a real bound, and exists so a mis-picked
# 40MB file is refused before it is read into memory.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

SortField = Literal["posted_at", "first_seen_at", "last_seen_at", "title", "company"]

# The JD bodies and the raw board payload are ~185 MB across the table and are
# never part of a list response (`JobOut` omits them). Loading them anyway made
# a 100-row page read ~2 MB of text and parse 100 JSON documents to discard.
_JD_BODY_DEFERRED = (defer(JobPosting.raw_json), defer(JobPosting.description_html))
_LIST_DEFERRED = (*_JD_BODY_DEFERRED, defer(JobPosting.description_text))


def _apply_filters(stmt: Select, *, status: str = "open", **filters: object) -> Select:
    """`job_filters.apply_job_filters` with keyword arguments.

    Kept as a thin shim so the facet queries below read as they did; the one
    implementation lives in `app/job_filters.py`, shared with the funnel and
    with the scope sent to Matches.
    """
    # Callers pass query parameters straight through, so an absent list filter
    # arrives as None; the model's defaults are the empty list.
    given = {key: value for key, value in filters.items() if value is not None}
    return apply_job_filters(stmt, JobFilterSet(**given), status=status)


def job_filter_params(
    company: Annotated[list[str] | None, Query(description="Repeatable company filter")] = None,
    ats: Annotated[list[str] | None, Query(description="Repeatable ATS filter")] = None,
    remote: bool | None = None,
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
    min_validity: Annotated[
        int | None,
        Query(ge=0, le=100, description="Minimum validity score; unscored rows always pass"),
    ] = None,
) -> JobFilterSet:
    """The Jobs tile's filters from query parameters — one parser for every route."""
    return JobFilterSet(
        company=company or [],
        ats=ats or [],
        remote=remote,
        q=q,
        q_scope=q_scope,
        department=department or [],
        posted_within_days=posted_within_days,
        first_seen_after=first_seen_after,
        eligibility_pass=eligibility_pass,
        min_validity=min_validity,
    )


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
    filters: Annotated[JobFilterSet, Depends(job_filter_params)],
    status: Annotated[Literal["open", "closed", "any"], Query()] = "open",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: SortField = "first_seen_at",
    order: Literal["asc", "desc"] = "desc",
) -> JobListOut:
    stmt = apply_job_filters(select(JobPosting), filters, status=status)

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
    stmt = stmt.options(*_LIST_DEFERRED)

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

    # Every count below is folded into as few scans as possible. The filter
    # columns (`posted_at`, `validity_score`, `workplace_type`, ...) sit behind
    # ~20 KB of JD text in each row, so a scan costs ~40 ms and the old one-
    # query-per-number shape made this route ~17 scans long.
    def flag(condition) -> object:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    week_ago = utcnow() - timedelta(days=7)
    moment = None
    if since is not None:
        moment = since if since.tzinfo else since.replace(tzinfo=UTC)

    # Validity bands, so the `min validity` slider can show what each step
    # would cost before you move it. `unscored` is reported separately because
    # those rows pass every threshold and would otherwise look like
    # high-validity rows.
    band_edges = (("solid", 85, 101), ("ok", 70, 85), ("questionable", 50, 70), ("suspect", 0, 50))
    score = JobPosting.validity_score
    gated = _apply_filters(
        select(
            func.count(),
            flag(IS_REMOTE),
            flag(JobPosting.posted_at.is_not(None) & (JobPosting.posted_at >= week_ago)),
            flag(JobPosting.first_seen_at > moment) if moment is not None else func.count() * 0,
            flag(score.is_(None)),
            *(flag(score.is_not(None) & (score >= lo) & (score < hi)) for _, lo, hi in band_edges),
        ).select_from(JobPosting),
        status=status,
        eligibility_pass=eligibility_pass,
    )
    total, remote_count, posted_week, new_since, validity_unscored, *band_counts = (
        (await session.execute(gated)).one()
    )
    validity_bands = {label: n for (label, _, _), n in zip(band_edges, band_counts, strict=True)}

    # Deliberately NOT gated. With the gate on, the query above already
    # excludes every ineligible row, so counting within it would report
    # ineligible = 0 and make the split describe the query rather than the
    # board. This pair always answers "of the jobs at this status, how many
    # clear the filter?", which is the only reading that stays useful.
    split_total, eligible_count = (
        await session.execute(
            _apply_filters(
                select(func.count(), flag(JobPosting.eligibility_pass.is_(True))).select_from(
                    JobPosting
                ),
                status=status,
            )
        )
    ).one()

    open_total, closed_total = (
        await session.execute(
            select(flag(JobPosting.status == "open"), flag(JobPosting.status == "closed"))
        )
    ).one()
    last_ingest = await session.scalar(select(func.max(IngestRun.finished_at)))

    # Every dropdown's options in one pass over the gated rows, counted here
    # rather than with one GROUP BY scan per column.
    facet_columns = {
        "companies": JobPosting.company,
        "ats": JobPosting.ats,
        "departments": JobPosting.department,
        "currencies": JobPosting.salary_currency,
        "employment_types": JobPosting.employment_type,
        "workplace_types": JobPosting.workplace_type,
    }
    counters: dict[str, Counter[str]] = {key: Counter() for key in facet_columns}
    rows = await session.execute(
        _apply_filters(
            select(*facet_columns.values()), status=status, eligibility_pass=eligibility_pass
        )
    )
    for row in rows:
        for key, value in zip(facet_columns, row, strict=True):
            if value:
                counters[key][value] += 1

    def ranked(counter: Counter[str]) -> list[dict[str, object]]:
        ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        return [{"value": value, "count": count} for value, count in ordered]

    return {
        **{key: ranked(counter) for key, counter in counters.items()},
        "validity": {"bands": validity_bands, "unscored": validity_unscored},
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


@router.get("/meta/funnel", tags=["meta"])
async def jobs_funnel_route(
    session: Annotated[AsyncSession, Depends(get_session)],
    filters: Annotated[JobFilterSet, Depends(job_filter_params)],
    status: Annotated[Literal["open", "closed", "any"], Query()] = "open",
) -> dict[str, object]:
    """How the board narrows to the Jobs list, one active filter at a time.

    Takes exactly the `/jobs` filter parameters, and the final step's count is
    the `/jobs` total for the same query — both are built by
    `job_filters.apply_job_filters`.
    """
    return await jobs_funnel(session, filters, status=status)


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


# ---------------------------------------------------------------------------
# Matching — fit against `profile.yaml`
#
# Ranking only. Nothing here drops a job: `eligibility_pass` is the binary
# gate, and this is an ordering on top of it. See `app/matching.py` for why
# that separation is load-bearing rather than stylistic.
# ---------------------------------------------------------------------------
_match_lock = asyncio.Lock()


def _band_of(score: int) -> str:
    if score >= 80:
        return "excellent"
    if score >= 65:
        return "strong"
    if score >= 50:
        return "moderate"
    if score >= 35:
        return "weak"
    return "poor"


# [lo, hi) score ranges — the SQL form of `_band_of` and of the Validator's
# `validation_runner._band`.
_FIT_BAND_RANGES = {
    "excellent": (80, 101),
    "strong": (65, 80),
    "moderate": (50, 65),
    "weak": (35, 50),
    "poor": (0, 35),
}
_VALIDITY_BAND_RANGES = {
    "solid": (85, 101),
    "ok": (70, 85),
    "questionable": (50, 70),
    "suspect": (0, 50),
}


def _validity_band_of(score: int | None) -> str:
    return "unchecked" if score is None else validity_band(score)


def _template_available() -> bool:
    try:
        ResumeDocument.load()
    except ResumeTemplateError:
        return False
    return True


@router.get("/profile", response_model=ProfileOut, tags=["matching"])
async def read_profile() -> ProfileOut:
    """The profile every job is scored against.

    Answers 200 with `configured: false` when there is no profile yet, rather
    than 500. A first run legitimately has none, and the dashboard needs to
    tell "not set up" apart from "the server is broken" in order to show the
    setup dialog instead of an error.
    """
    try:
        p = get_profile()
    except ProfileError as exc:
        return ProfileOut(
            configured=False, error=str(exc), has_template=_template_available()
        )
    return ProfileOut(
        version=p.version,
        configured=True,
        full_name=p.identity.full_name,
        location=p.identity.location,
        total_years=p.seniority.total_years,
        ai_years=p.seniority.ai_years,
        current_title=p.seniority.current_title,
        target_titles=p.seniority.target_titles,
        skills=p.skills,
        gaps=p.gaps,
        min_annual_inr=p.compensation.min_annual_inr,
        needs_sponsorship=p.work_authorization.needs_sponsorship,
        resume_files=p.resume_files,
        has_template=_template_available(),
    )


@router.get("/profile/document", response_model=ProfileDocumentOut, tags=["matching"])
async def read_profile_document() -> ProfileDocumentOut:
    """The raw `profile.yaml` mapping, for the editor.

    The editor round-trips this rather than `ProfileOut` so that hand-added
    keys survive a save — `profile.yaml` stays the source of truth, and an
    editor that silently drops what it does not understand would make
    hand-editing second-class.
    """
    settings = get_settings()
    try:
        data = profile_intake.read_profile_data()
    except ProfileError as exc:
        return ProfileDocumentOut(
            configured=False, error=str(exc), path=str(settings.profile_file)
        )
    error: str | None = None
    version = ""
    try:
        version = get_profile().version
    except ProfileError as exc:
        error = str(exc)
    return ProfileDocumentOut(
        data=data,
        version=version,
        configured=bool(data) and error is None,
        error=error,
        path=str(settings.profile_file),
        has_template=_template_available(),
    )


@router.put("/profile", response_model=ProfileOut, tags=["matching"])
async def write_profile(payload: ProfileSaveIn) -> ProfileOut:
    """Write `profile.yaml` and return the new version.

    Validated through the same gate `load_profile` applies, so the editor
    cannot save a file the loader would then refuse — which would leave
    scoring broken with no visible cause. A rejected save changes nothing on
    disk.

    Scores keyed to the old `profile_version` are not deleted: they simply stop
    matching the current version, which is what makes a stale score render as
    stale instead of as current.
    """
    try:
        profile_intake.save_profile(payload.data)
    except ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await read_profile()


@router.post("/profile/upload", response_model=ProfileDraftOut, tags=["matching"])
async def upload_resume(
    tex: Annotated[UploadFile, File(description="The résumé's LaTeX source (.tex)")],
    resume: Annotated[
        UploadFile | None, File(description="The compiled résumé (.pdf), optional")
    ] = None,
    parse: Annotated[
        bool, Query(description="Read the .tex with the LLM and propose a profile")
    ] = True,
) -> ProfileDraftOut:
    """Store an uploaded résumé and propose a profile from it. Saves no profile.

    Two files, two jobs. The **.tex** becomes the tailoring template — Stage 3
    only ever re-words regions of it, so without it there is nothing to tailor.
    The **.pdf** is what Phase 3 uploads to application forms, and is recorded
    as `resume_files.base`.

    `parse=false` stores the files without spending an LLM call, for replacing
    a template without re-reading the whole profile.

    The returned draft is *not* written to disk: `gaps:` is the fact-checker's
    denylist and `evidence.depth` decides whether a capability reads as
    production experience. Both need a human's eye before anything is generated
    against them, so the caller reviews the draft and PUTs it back.
    """
    warnings: list[str] = []
    try:
        raw = await tex.read()
    finally:
        await tex.close()
    if not raw:
        raise HTTPException(status_code=422, detail="the .tex file is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"the .tex is larger than {MAX_UPLOAD_BYTES // 1024}KB"
        )
    try:
        tex_source = raw.decode("utf-8")
    except UnicodeDecodeError:
        # A .tex is text; a PDF uploaded into this slot is the likely cause, and
        # saying so beats a decode traceback.
        raise HTTPException(
            status_code=422,
            detail="that file is not UTF-8 text — is it the .tex, rather than the PDF?",
        ) from None
    if "\\begin{document}" not in tex_source and "\\documentclass" not in tex_source:
        warnings.append(
            "that .tex has no \\documentclass or \\begin{document} — it may not compile"
        )

    existing = profile_intake.read_profile_data()
    stored = profile_intake.store_resume_files(
        tex_source, resume_bytes=None, resume_name=None
    )
    if resume is not None and resume.filename:
        try:
            pdf = await resume.read()
        finally:
            await resume.close()
        if len(pdf) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"the résumé file is larger than {MAX_UPLOAD_BYTES // 1024}KB",
            )
        if pdf:
            stored.update(
                profile_intake.store_resume_files(
                    None, resume_bytes=pdf, resume_name=resume.filename
                )
            )

    # Point the profile at what was just stored, so a save wires up Phase 3's
    # résumé upload and Stage 3's template without the user editing paths.
    files = dict(existing.get("resume_files") or {})
    files.update(stored)
    existing["resume_files"] = files

    if not parse:
        return ProfileDraftOut(
            data=existing,
            resume_text=profile_intake.tex_to_text(tex_source),
            stored=stored,
            warnings=warnings,
        )

    try:
        data, tokens = await profile_intake.draft_from_tex(tex_source, existing=existing)
    except ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LLMNotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{exc} — the files are stored; fill the profile in by hand, or set a key.",
        ) from exc
    except LLMRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"reading the résumé failed: {exc}") from exc

    if not data.get("gaps"):
        warnings.append(
            "no gaps were identified — `gaps:` is the denylist that blocks a generated résumé "
            "from claiming a stack you do not have, so add the ones that matter before saving."
        )
    return ProfileDraftOut(
        data=data,
        tokens=tokens,
        resume_text=profile_intake.tex_to_text(tex_source),
        stored=stored,
        warnings=warnings,
    )


@router.post("/matches/score", response_model=MatchRunOut, tags=["matching"])
async def score_matches(
    session: Annotated[AsyncSession, Depends(get_session)],
    force: Annotated[bool, Query(description="Re-score even unchanged postings")] = False,
) -> MatchRunOut:
    """Run the deterministic scoring pass. No LLM calls, no network."""
    if _match_lock.locked():
        raise HTTPException(status_code=409, detail="a scoring pass is already running")
    async with _match_lock:
        result = await run_matching(session, force=force)
    out = _match_run_out(result)
    assert out is not None
    return out


def _llm_run_out(result: LLMRunResult) -> LLMRunOut:
    return LLMRunOut(
        profile_version=result.profile_version,
        routed=result.routed,
        cached=result.cached,
        screened=result.screened,
        failed=result.failed,
        skipped_budget=result.skipped_budget,
        requests=result.requests,
        tokens=result.tokens,
        bands=result.bands,
        statuses=result.statuses,
        blocked=result.blocked,
        duration_ms=result.duration_ms,
        error=result.error,
    )


async def _llm_background(
    force: bool, limit: int | None, job_ids: list[int] | None = None
) -> None:
    """Own session, own lock — the pass outlives the request that started it."""
    async with llm_tracker.lock:
        # Stamped here, not only inside the runner: the runner sets it after
        # the shortlist queries, and the POST that started this pass answered
        # before then — "idle", so the dashboard never polled and never saw
        # the pass finish.
        progress = llm_tracker.progress = LLMProgress(started_at=utcnow())
        try:
            async with session_scope() as session:
                llm_tracker.result = await run_llm_screen(
                    session,
                    force=force,
                    limit=limit,
                    job_ids=job_ids,
                    progress=progress,
                )
            if llm_tracker.result.error and not progress.error:
                progress.error = llm_tracker.result.error
        except Exception as exc:  # pragma: no cover - defensive
            progress.error = f"{type(exc).__name__}: {exc}"
            log.exception("llm.run_failed")
        finally:
            # Early returns in the runner (no profile) never stamp it, which
            # would leave the pass reading as running forever.
            if progress.finished_at is None:
                progress.finished_at = utcnow()


def _start_llm(force: bool, limit: int | None, job_ids: list[int] | None = None) -> None:
    if not get_settings().llm_configured:
        raise HTTPException(status_code=503, detail="the LLM provider key is not configured")
    if llm_tracker.busy:
        raise HTTPException(status_code=409, detail="a deep read is already running")
    asyncio.create_task(_llm_background(force, limit, job_ids))


@router.post("/matches/llm", response_model=LLMStatusOut, tags=["matching"])
async def start_llm_screen(
    force: Annotated[bool, Query(description="Re-read rows already cached")] = False,
    limit: Annotated[
        int | None,
        Query(ge=1, le=HARD_MAX_READS, description="Read at most N shortlisted jobs (≤ 50)"),
    ] = None,
) -> LLMStatusOut:
    """Deep-read the shortlist in the background and return at once.

    Reads at most `limit` jobs (default `LLM_MAX_REQUESTS_PER_RUN`, 15; never
    more than 50), best score first. Poll `GET /matches/llm/status`.
    """
    _start_llm(force, limit)
    # Let the task claim the lock before the caller can poll and be told "idle".
    await asyncio.sleep(0)
    return await llm_status()


@router.post("/matches/{job_id}/llm", response_model=LLMStatusOut, tags=["matching"])
async def start_llm_one(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    force: Annotated[bool, Query(description="Re-read even if already read")] = False,
) -> LLMStatusOut:
    """Deep-read ONE job on request — one call, shortlist or not.

    This is where low-confidence jobs go: they no longer reach the automatic
    pass, but any ranked job can be read from its drawer when it is worth it.
    """
    try:
        version = get_profile().version
    except ProfileError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    exists = await session.scalar(
        select(func.count())
        .select_from(JobMatch)
        .where(JobMatch.job_id == job_id, JobMatch.profile_version == version)
    )
    if not exists:
        raise HTTPException(
            status_code=404,
            detail="this job is not ranked yet — only open, eligible jobs can be deep-read",
        )
    _start_llm(force, 1, [job_id])
    await asyncio.sleep(0)
    return await llm_status()


@router.get("/matches/llm/estimate", response_model=LLMEstimateOut, tags=["matching"])
async def llm_estimate(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int | None, Query(ge=1, le=HARD_MAX_READS)] = None,
) -> LLMEstimateOut:
    """Re-price the deep read for a different cap — the confirm dialog's picker."""
    out = _estimate_out(await estimate_llm_pass(session, limit=limit))
    assert out is not None
    return out


@router.get("/matches/llm/status", response_model=LLMStatusOut, tags=["matching"])
async def llm_status() -> LLMStatusOut:
    progress = llm_tracker.progress
    # The lock is the truth: a pass holds it from before its first query.
    if llm_tracker.busy or progress.running:
        state: Literal["idle", "running", "done"] = "running"
    elif progress.finished_at is not None:
        state = "done"
    else:
        state = "idle"
    return LLMStatusOut(
        state=state,
        total=progress.total,
        done=progress.done,
        cached=progress.cached,
        failed=progress.failed,
        tokens=progress.tokens,
        current=progress.current,
        started_at=progress.started_at,
        finished_at=progress.finished_at,
        error=progress.error,
        result=_llm_run_out(llm_tracker.result) if llm_tracker.result else None,
    )


def _format_usd(value: float) -> str:
    return f"${value / 1000:,.0f}K" if value >= 10_000 else f"${value:,.0f}"


def _usd_pay(job: JobPosting) -> dict[str, str]:
    """`usd_pay` / `usd_pay_source` for a match row, most trustworthy first.

    The board's structured salary wins, then the deep read's verbatim quote,
    then a pattern match over the JD prose. Non-USD pay is ignored on purpose:
    this finding answers "does this pay in dollars", not "is pay stated".
    """
    if (job.salary_currency or "").upper() == "USD" and (job.salary_min or job.salary_max):
        amounts = sorted({v for v in (job.salary_min, job.salary_max) if v})
        return {"usd_pay": " – ".join(_format_usd(v) for v in amounts), "usd_pay_source": "board"}

    quoted = (job.llm_validity or {}).get("compensation_text") or ""
    if quoted and detect_currency(quoted) == "USD":
        return {"usd_pay": " ".join(quoted.split())[:80], "usd_pay_source": "deep_read"}

    found = find_usd_pay(job.description_text)
    return {"usd_pay": found, "usd_pay_source": "jd"} if found else {}


@router.get("/matches", response_model=MatchListOut, tags=["matching"])
async def list_matches(
    session: Annotated[AsyncSession, Depends(get_session)],
    company: Annotated[list[str] | None, Query()] = None,
    ats: Annotated[list[str] | None, Query()] = None,
    department: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    q_scope: Annotated[Literal["all", "title"], Query()] = "all",
    posted_within_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    min_score: Annotated[int, Query(ge=0, le=100)] = 0,
    fit_band: Annotated[
        list[str] | None,
        Query(description="Ranker fit: excellent|strong|moderate|weak|poor (repeatable, OR)"),
    ] = None,
    llm_band: Annotated[
        list[str] | None,
        Query(
            description=(
                "The LLM's fit_band from a current deep read: excellent|strong|moderate|"
                "weak|poor|unread (repeatable, OR)"
            )
        ),
    ] = None,
    validity: Annotated[
        list[str] | None,
        Query(description="Validator band: solid|ok|questionable|suspect|unchecked (repeatable, OR)"),
    ] = None,
    shortlisted: Annotated[
        bool | None, Query(description="true = only jobs on the LLM shortlist")
    ] = None,
    hide_blocked: Annotated[
        bool, Query(description="true = drop jobs whose JD states a hard blocker")
    ] = False,
    match_prefs: Annotated[
        bool,
        Query(
            description=(
                "true (default) = only rows that cleared the preference gate. "
                "false shows every scored row, preference misses included."
            )
        ),
    ] = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Annotated[Literal["score", "posted_at", "first_seen_at"], Query()] = "score",
    order: Literal["asc", "desc"] = "desc",
) -> MatchListOut:
    """Scored jobs, best fit first, composing with the existing /jobs filters."""
    try:
        version = get_profile().version
    except ProfileError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    stmt = (
        select(JobMatch, JobPosting)
        .join(JobPosting, JobMatch.job_id == JobPosting.id)
        .where(JobMatch.profile_version == version)
    )
    # Matching only ever scores open+eligible rows, but re-applying the gate
    # keeps the list correct when a job closes between the pass and the read.
    stmt = _apply_filters(
        stmt,
        company=company,
        ats=ats,
        department=department,
        q=q,
        q_scope=q_scope,
        posted_within_days=posted_within_days,
        status="open",
        eligibility_pass=True,
    )
    # The scope sent from Jobs bounds this list even with preference misses
    # shown: "Include jobs that miss my preferences" means misses among the
    # jobs I sent, not the whole eligible board. Applied live, like the funnel.
    try:
        transfer = get_prefs().transfer
    except PrefsError:
        transfer = None
    if transfer is not None:
        stmt = apply_job_filters(stmt, transfer.filters)
    # The preference gate. On by default, which is the whole shape of this
    # surface: Jobs shows the entire board, Matches shows what I asked for.
    # It reads the STORED per-row verdict rather than re-deriving the filter
    # here, because the exact rules need the FX table and the JD prose — see
    # `prefs_gate.sql_clause` on why the SQL form is only a superset.
    if match_prefs:
        stmt = stmt.where(JobMatch.prefs_pass.is_(True))
    threshold = get_weights().llm_threshold
    if min_score:
        stmt = stmt.where(JobMatch.score >= min_score)

    # Blocked jobs among everything above, counted BEFORE "hide blocked" is
    # applied — it is the number that toggle would remove.
    blocked_count = (
        await session.scalar(
            select(func.count()).select_from(stmt.where(JobMatch.blocked.is_(True)).subquery())
        )
        or 0
    )
    if hide_blocked:
        stmt = stmt.where(JobMatch.blocked.is_(False))
    if shortlisted is not None:
        gate = shortlist_clause(threshold)
        stmt = stmt.where(gate if shortlisted else ~gate)

    # Three independent band filters — the ranker's fit, the LLM's fit and the
    # Validator's validity — ORed within one, ANDed across them. Each one's
    # counts are taken with the OTHER two applied but not itself, so a chip
    # says how many rows selecting it would add, and never collapses to the
    # current selection ("All 3" the moment Excellent was clicked).
    # The LLM's band, only where `llm_read_for` would say True: a read of this
    # exact JD that succeeded (failed reads carry `error` and no `fit_band`).
    llm_fit = JobMatch.llm_verdict["fit_band"].as_string()
    llm_read = and_(
        JobMatch.llm_used.is_(True),
        JobMatch.llm_content_hash == JobPosting.content_hash,
        llm_fit.is_not(None),
    )
    want_fit = {b.lower() for b in fit_band or []}
    want_llm = {b.lower() for b in llm_band or []}
    want_validity = {b.lower() for b in validity or []}

    band_rows = await session.execute(
        stmt.with_only_columns(
            JobMatch.score,
            shortlist_clause(threshold),
            case((llm_read, llm_fit), else_=None),
            JobPosting.validity_score,
        )
    )
    bands: dict[str, int] = {}
    llm_bands: dict[str, int] = {}
    validity_bands: dict[str, int] = {}
    on_shortlist = 0
    total_all = 0
    for score, is_short, read_band, validity_score in band_rows:
        total_all += 1
        if is_short:
            on_shortlist += 1
        keys = (_band_of(score), read_band or "unread", _validity_band_of(validity_score))
        hits = (
            not want_fit or keys[0] in want_fit,
            not want_llm or keys[1] in want_llm,
            not want_validity or keys[2] in want_validity,
        )
        for i, counts in enumerate((bands, llm_bands, validity_bands)):
            if all(hit for j, hit in enumerate(hits) if j != i):
                counts[keys[i]] = counts.get(keys[i], 0) + 1

    if want_fit:
        stmt = stmt.where(
            or_(
                *(
                    and_(JobMatch.score >= lo, JobMatch.score < hi)
                    for b, (lo, hi) in _FIT_BAND_RANGES.items()
                    if b in want_fit
                ),
                False,
            )
        )
    if want_llm:
        named = sorted(want_llm - {"unread"})
        clauses = [and_(llm_read, llm_fit.in_(named))] if named else []
        if "unread" in want_llm:
            clauses.append(~llm_read)
        stmt = stmt.where(or_(*clauses, False))
    if want_validity:
        clauses = [
            and_(JobPosting.validity_score >= lo, JobPosting.validity_score < hi)
            for b, (lo, hi) in _VALIDITY_BAND_RANGES.items()
            if b in want_validity
        ]
        if "unchecked" in want_validity:
            clauses.append(JobPosting.validity_score.is_(None))
        stmt = stmt.where(or_(*clauses, False))

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    col = JobMatch.score if sort == "score" else getattr(JobPosting, sort)
    stmt = stmt.order_by(col.desc() if order == "desc" else col.asc())
    if sort == "score":
        # Ties on score fall back to recency, so an arbitrary id order does not
        # bury a fresh posting under a months-old one with the same number.
        stmt = stmt.order_by(JobPosting.posted_at.desc())
    stmt = stmt.order_by(JobPosting.id.desc()).limit(limit).offset(offset)
    # `description_text` stays loaded: `_usd_pay` reads the JD prose.
    stmt = stmt.options(*_JD_BODY_DEFERRED)

    rows = (await session.execute(stmt)).all()
    items = [
        MatchOut(
            job=JobOut.model_validate(job),
            score=m.score,
            band=_band_of(m.score),
            subscores=m.subscores or {},
            match_reasons=m.match_reasons or [],
            confident=m.confident,
            matched_skills=m.matched_skills or [],
            missing_stacks=m.missing_stacks or [],
            years_required=m.years_required,
            shortlisted=not (excluded := reasons_excluded(m, job, threshold)),
            shortlist_reasons=excluded,
            blockers=m.blockers or [],
            llm_used=m.llm_used,
            llm_read=m.llm_read_for(job),
            llm_verdict=m.llm_verdict,
            profile_version=m.profile_version,
            prefs_pass=m.prefs_pass,
            prefs_reasons=m.prefs_reasons or [],
            prefs_version=m.prefs_version or "",
            **_usd_pay(job),
            scored_at=m.scored_at,
        )
        for m, job in rows
    ]
    return MatchListOut(
        total=total,
        limit=limit,
        offset=offset,
        profile_version=version,
        items=items,
        bands=bands,
        llm_bands=llm_bands,
        validity_bands=validity_bands,
        shortlisted=on_shortlist,
        total_all=total_all,
        blocked=blocked_count,
    )


@router.get("/matches/funnel", tags=["matching"])
async def matches_funnel_route(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    """How the eligible board narrows to the LLM shortlist, step by step.

    Read from the verdicts the last Run stored. `stale` is true when those
    verdicts were computed under different preferences — Run to refresh.
    """
    try:
        version = get_profile().version
        prefs = get_prefs()
    except (ProfileError, PrefsError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return await matches_funnel(
        session,
        profile_version=version,
        prefs=prefs,
        threshold=get_weights().llm_threshold,
        default_reads=get_settings().llm_max_requests_per_run,
    )


@router.put("/matches/transfer", response_model=PrefsOut, tags=["matching"])
async def transfer_to_matches(
    session: Annotated[AsyncSession, Depends(get_session)],
    filters: JobFilterSet,
) -> PrefsOut:
    """"Send to Matches": make these Jobs filters the scope Matches works on.

    Stored in `prefs.yaml` as the filters themselves, re-applied on every Run,
    so jobs a later sweep adds under the same filters flow in and closed ones
    drop out. Applied by the next `POST /matches/run` — free, no LLM calls.
    """
    try:
        current = load_prefs()
    except PrefsError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    count = await session.scalar(
        apply_job_filters(select(func.count()).select_from(JobPosting), filters)
    ) or 0
    stored = save_prefs(
        current.model_copy(
            update={
                "transfer": TransferPrefs(
                    filters=filters, count_at_transfer=count, transferred_at=utcnow()
                )
            }
        )
    )
    log.info("matches.transfer", extra={"count": count, "version": stored.version})
    return _prefs_out(stored)


@router.delete("/matches/transfer", response_model=PrefsOut, tags=["matching"])
async def clear_transfer() -> PrefsOut:
    """Forget the scope sent from Jobs — Matches works on every eligible job again."""
    try:
        current = load_prefs()
    except PrefsError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _prefs_out(save_prefs(current.model_copy(update={"transfer": None})))


# ---------------------------------------------------------------------------
# Preferences — "what do I want out of the board right now?"
#
# A third config layer, and the API keeps the three straight:
#   filters.yaml -> eligibility, at ingest. Deliberately NOT editable here:
#                   it gates Telegram alerts and is doctrine, not mood.
#   profile.yaml -> who I am. `GET /profile`.
#   prefs.yaml   -> this. Cheap, re-runnable, gates the Matches list only.
# ---------------------------------------------------------------------------
def _prefs_out(prefs: Prefs) -> PrefsOut:
    return PrefsOut(
        version=prefs.version,
        work=prefs.work.model_dump(),
        experience=prefs.experience.model_dump(),
        compensation=prefs.compensation.model_dump(),
        validity=prefs.validity.model_dump(),
        freshness=prefs.freshness.model_dump(),
        scope=prefs.scope.model_dump(),
        transfer=(
            {
                **prefs.transfer.model_dump(mode="json"),
                "labels": prefs.transfer.filters.describe(),
            }
            if prefs.transfer
            else None
        ),
        include_unstated=prefs.include_unstated,
        updated_at=prefs.updated_at,
        workplace_options=list(WORKPLACE_TYPES),
        employment_options=list(EMPLOYMENT_TYPES),
    )


@router.get("/prefs", response_model=PrefsOut, tags=["matching"])
async def read_prefs() -> PrefsOut:
    """Current match preferences, plus the vocabularies the editor renders."""
    try:
        return _prefs_out(load_prefs())
    except PrefsError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/prefs", response_model=PrefsOut, tags=["matching"])
async def write_prefs(payload: PrefsIn) -> PrefsOut:
    """Merge a partial preferences write into `prefs.yaml` and return it.

    A **merge**, not a replace: the editor saves one section at a time, and a
    replace would silently reset every section the client did not send.

    `prefs.yaml` on disk stays the source of truth — this writes the file
    (atomically) rather than keeping preferences in the database, so
    hand-editing stays a first-class path exactly as it is for `profile.yaml`.

    Saving does NOT re-score, and cannot need to: preferences select rows,
    they never change a score. The new verdict is applied by the next
    `POST /matches/run`, which is free and which the UI calls next anyway.
    """
    try:
        current = load_prefs()
    except PrefsError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    merged = current.model_dump(exclude={"updated_at"})
    for section in ("work", "experience", "compensation", "validity", "freshness", "scope"):
        incoming = getattr(payload, section)
        if incoming is not None:
            merged[section] = {**merged.get(section, {}), **incoming}
    if payload.include_unstated is not None:
        merged["include_unstated"] = payload.include_unstated

    try:
        candidate = Prefs(**merged)
    except Exception as exc:  # noqa: BLE001 - the message names the bad value
        raise HTTPException(status_code=422, detail=f"invalid preferences: {exc}") from exc

    stored = save_prefs(candidate)
    log.info("prefs.updated", extra={"version": stored.version})
    return _prefs_out(stored)


# ---------------------------------------------------------------------------
# The Matches "Run" pipeline: validity -> ranking -> a costed estimate.
# Never the deep read; see `app/pipeline.py`.
# ---------------------------------------------------------------------------
def _validity_out(result: ValidityRunResult | None) -> ValidityRunOut | None:
    if result is None:
        return None
    return ValidityRunOut(
        considered=result.considered,
        scored=result.scored,
        unchanged=result.unchanged,
        suspect=result.suspect,
        duplicates=result.duplicates,
        llm_applied=result.llm_applied,
        bands=result.bands,
        reasons=result.reasons,
        duration_ms=result.duration_ms,
        error=result.error,
    )


def _estimate_out(est: LLMEstimate | None) -> LLMEstimateOut | None:
    if est is None:
        return None
    return LLMEstimateOut(
        routed=est.routed,
        cached=est.cached,
        pending=est.pending,
        cap=est.cap,
        over_cap=est.over_cap,
        est_tokens=est.est_tokens,
        est_minutes=est.est_minutes,
        requests=est.requests,
        token_cap=est.token_cap,
        token_cap_period=est.token_cap_period,
        tokens_used=est.tokens_used,
        cap_budget_pct=est.cap_budget_pct,
        fits_in_cap=est.fits_in_cap,
        provider=est.provider,
        model=est.model,
        configured=est.configured,
        note=est.note,
    )


def _match_run_out(result: MatchRunResult | None) -> MatchRunOut | None:
    if result is None:
        return None
    return MatchRunOut(
        profile_version=result.profile_version,
        considered=result.considered,
        in_transfer=result.in_transfer,
        scored=result.scored,
        updated=result.updated,
        skipped=result.skipped,
        shortlisted=result.shortlisted,
        blocked=result.blocked,
        low_validity=result.low_validity,
        low_confidence=result.low_confidence,
        bands=result.bands,
        duration_ms=result.duration_ms,
        error=result.error,
        prefs_version=result.prefs_version,
        matching_prefs=result.matching_prefs,
    )


def _pipeline_out(p: PipelineProgress) -> PipelineStatusOut:
    return PipelineStatusOut(
        stage=p.stage,
        started_at=p.started_at,
        finished_at=p.finished_at,
        error=p.error,
        profile_version=p.profile_version,
        prefs_version=p.prefs_version,
        validity=_validity_out(p.validity),
        ranking=_match_run_out(p.ranking),
        estimate=_estimate_out(p.estimate),
    )


async def _pipeline_background(force: bool) -> None:
    """Own session, own lock — the run outlives the request that started it."""
    async with pipeline_tracker.lock:
        pipeline_tracker.progress = PipelineProgress()
        try:
            async with session_scope() as session:
                await run_pipeline(
                    session, force=force, progress=pipeline_tracker.progress
                )
        except Exception as exc:  # pragma: no cover - defensive
            pipeline_tracker.progress.stage = "failed"
            pipeline_tracker.progress.error = f"{type(exc).__name__}: {exc}"
            pipeline_tracker.progress.finished_at = utcnow()
            log.exception("pipeline.failed")


@router.post("/matches/run", response_model=PipelineStatusOut, tags=["matching"])
async def start_pipeline(
    force: Annotated[bool, Query(description="Re-score even unchanged postings")] = False,
) -> PipelineStatusOut:
    """Run validity + ranking in the background, then price the deep read.

    Returns immediately; poll `GET /matches/run/status`. The two passes are
    ~22s of CPU combined, which is past the point where holding an HTTP
    request open is honest — and the dashboard already knows how to poll a
    background job from the ingest sweep.

    This never starts the deep read. It hands back what the deep read would
    cost, and `POST /matches/llm` is the separate, explicit confirmation.
    """
    if pipeline_tracker.busy:
        raise HTTPException(status_code=409, detail="a run is already in progress")
    asyncio.create_task(_pipeline_background(force))
    # Let the task claim the lock before the caller can poll and see "idle".
    await asyncio.sleep(0)
    return _pipeline_out(pipeline_tracker.progress)


@router.get("/matches/run/status", response_model=PipelineStatusOut, tags=["matching"])
async def pipeline_status() -> PipelineStatusOut:
    return _pipeline_out(pipeline_tracker.progress)


@router.post("/validity/run", response_model=ValidityRunOut, tags=["validation"])
async def run_validity(
    session: Annotated[AsyncSession, Depends(get_session)],
    force: Annotated[bool, Query(description="Rewrite even unchanged verdicts")] = False,
) -> ValidityRunOut:
    """The validity pass on its own. Free, no network, ~12s for 6,317 rows.

    Synchronous because it is short and has no external dependency. The
    Matches pipeline runs the same function as its first stage; this exists
    for the Jobs tile, which wants validity badges without ranking anything.
    """
    result = await run_validation(session, force=force)
    out = _validity_out(result)
    assert out is not None  # run_validation always returns a result
    return out


# --------------------------------------------------------------------------
# Stage 3 — tailored documents
#
# The résumé is LaTeX end to end: the editor round-trips `tex`, and the PDF is
# compiled from it on demand rather than stored (see `app/documents.py`).
# Generation is synchronous — one LLM call for one job, ~10s, started by a
# person who is watching. That is the opposite of the deep read, which is a
# multi-hour pass and therefore a background task with a poll endpoint.
# --------------------------------------------------------------------------
def _document_out(document: GeneratedDocument, job: JobPosting) -> DocumentOut:
    tailoring = document.tailoring or {}
    edits = document.edits or {}
    bullets = edits.get("bullets") or []
    return DocumentOut(
        id=document.id,
        job_id=document.job_id,
        kind=document.kind,
        profile_version=document.profile_version,
        tex=document.tex,
        is_draft=document.is_draft,
        hand_edited=document.hand_edited,
        llm_used=document.llm_used,
        llm_tokens=document.llm_tokens,
        created_at=document.created_at,
        updated_at=document.updated_at,
        stale=is_stale(document, job),
        tailoring_notes=list(tailoring.get("tailoring_notes") or []),
        jd_keywords=list(tailoring.get("jd_keywords") or []),
        issues=[FactIssueOut(**issue) for issue in (document.issues or [])],
        reworded=[b["id"] for b in bullets if b.get("text")],
        dropped=[b["id"] for b in bullets if not b.get("include", True)],
    )


async def _load_document(session: AsyncSession, doc_id: int) -> GeneratedDocument:
    document = await session.get(GeneratedDocument, doc_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    return document


DocumentKind = Literal["resume", "cover_letter"]


def _document_filename(job: JobPosting, extension: str, kind: str = "resume") -> str:
    """`Ashish_Kumar_Canonical_Security_Software_Engineer.pdf`.

    Named for the employer and role because these land in a downloads folder
    among a dozen others, where `resume.pdf` is unrecoverable. A cover letter
    gets `_Cover_Letter` on the end, so the pair sorts together.
    """
    try:
        who = get_profile().identity.full_name
    except ProfileError:
        who = "resume"
    parts = [who, job.company, job.title]
    if kind == COVER_LETTER:
        parts.append("Cover Letter")
    stem = "_".join(re.sub(r"[^A-Za-z0-9]+", "_", part).strip("_") for part in parts if part)
    return f"{stem[:120]}.{extension}"


@router.get("/documents/base", response_model=BaseResumeOut, tags=["documents"])
async def base_resume() -> BaseResumeOut:
    """The untailored résumé and its editable regions.

    Backs the diff pane, and answers "is tailoring possible at all" — the .tex
    lives beside the PDF in `data/`, which is gitignored as PII, so a fresh
    checkout has the profile but not the template.
    """
    try:
        template = ResumeDocument.load()
    except ResumeTemplateError as exc:
        return BaseResumeOut(tex="", available=False, note=str(exc))
    return BaseResumeOut(
        tex=template.tex,
        regions=[
            {"id": r.id, "kind": r.kind, "group": r.group, "label": r.label, "text": r.text}
            for r in template.regions
        ],
    )


@router.get("/matches/{job_id}/document", response_model=DocumentOut, tags=["documents"])
async def read_document(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: Annotated[DocumentKind, Query(description="resume or cover_letter")] = "resume",
) -> DocumentOut:
    """The tailored résumé (or cover letter) for this job at the CURRENT profile version.

    404 when none exists — the modal then opens on an empty state with a
    Generate button, rather than billing an LLM call because a row was clicked.
    """
    try:
        version = get_profile().version
    except ProfileError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    job = await session.get(JobPosting, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    document = await get_document(session, job_id, profile_version=version, kind=kind)
    if document is None:
        what = "cover letter" if kind == COVER_LETTER else "tailored resume"
        raise HTTPException(status_code=404, detail=f"no {what} for this job yet")
    return _document_out(document, job)


@router.post("/matches/{job_id}/document", response_model=DocumentOut, tags=["documents"])
async def create_document(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    force: Annotated[
        bool, Query(description="Re-run the LLM even if a current document exists")
    ] = False,
    kind: Annotated[DocumentKind, Query(description="resume or cover_letter")] = "resume",
) -> DocumentOut:
    """Tailor the résumé, or draft the cover letter, for this job. One LLM call,
    billed to the daily cap.

    Cache-correct per CLAUDE.md: an unchanged JD and an unchanged profile
    return the stored document and spend nothing. `force` is also how hand
    edits are discarded, which is why that case refuses without it rather than
    quietly overwriting the user's own text.
    """
    try:
        result = await generate(session, job_id, kind=kind, force=force)
    except DocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResumeTemplateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMRateLimited as exc:
        raise HTTPException(status_code=429, detail=f"LLM limit reached: {exc}") from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"the model failed: {exc}") from exc
    job = await require_job(session, job_id)
    return _document_out(result.document, job)


@router.put("/documents/{doc_id}", response_model=DocumentOut, tags=["documents"])
async def update_document(
    doc_id: int,
    payload: DocumentSaveIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentOut:
    """Save hand-edited LaTeX. Marks the document hand-edited, costs nothing."""
    document = await _load_document(session, doc_id)
    try:
        document = await save_tex(session, document, payload.tex)
    except DocumentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job = await require_job(session, document.job_id)
    return _document_out(document, job)


@router.post("/documents/{doc_id}/revert", response_model=DocumentOut, tags=["documents"])
async def revert_document(
    doc_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentOut:
    """Undo hand edits by replaying the stored tailoring. Free — no LLM call."""
    document = await _load_document(session, doc_id)
    try:
        document = await revert(session, document)
    except DocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    job = await require_job(session, document.job_id)
    return _document_out(document, job)


@router.get("/documents/{doc_id}/diff", response_model=DocumentDiffOut, tags=["documents"])
async def document_diff(
    doc_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DocumentDiffOut:
    """This résumé against the untailored template, region by region. Free.

    Résumé-only: a cover letter is prose written from the profile, so there is
    no original to diff it against — its equivalent review surface is the fact
    check, which already reports per sentence.

    200 with `available: false` when the template is missing or the document's
    LaTeX no longer parses. Both are states the review pane should explain, not
    HTTP errors: a hand edit that breaks the structure is exactly when someone
    reaches for the diff.
    """
    document = await _load_document(session, doc_id)
    if document.kind == COVER_LETTER:
        return DocumentDiffOut(
            document_id=document.id,
            kind=document.kind,
            available=False,
            note=(
                "A cover letter is written from your profile rather than tailored from a "
                "template, so there is no base version to compare it against."
            ),
        )
    try:
        template = ResumeDocument.load()
    except ResumeTemplateError as exc:
        return DocumentDiffOut(
            document_id=document.id, kind=document.kind, available=False, note=str(exc)
        )
    try:
        rows, counts = diff_against_base(document.tex, template)
    except ResumeTemplateError as exc:
        return DocumentDiffOut(
            document_id=document.id,
            kind=document.kind,
            available=False,
            note=f"this résumé's LaTeX no longer parses, so it cannot be compared: {exc}",
        )
    return DocumentDiffOut(
        document_id=document.id,
        kind=document.kind,
        rows=[DiffRowOut(**row) for row in rows],
        reworded=counts["reworded"],
        dropped=counts["dropped"],
        moved=counts["moved"],
        unchanged=counts["unchanged"],
        added=counts["added"],
    )


@router.post("/documents/{doc_id}/compile", response_model=CompileOut, tags=["documents"])
async def compile_document_route(
    doc_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    force: Annotated[bool, Query(description="Ignore the cached PDF")] = False,
) -> CompileOut:
    """Build the PDF and report what happened. The bytes come from `/pdf`.

    A failed compile answers 200 with `ok=false`: the editor is expected to be
    mid-edit and broken half the time, so a broken document is a state to
    render, not an HTTP error to handle.
    """
    document = await _load_document(session, doc_id)
    try:
        result = await compile_document(document, use_cache=not force)
    except LatexUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    warnings: list[str] = []
    if document.hand_edited:
        try:
            job = await require_job(session, document.job_id)
            warnings = hand_edit_warnings(document, job, get_profile())
        except (DocumentError, ProfileError, ResumeTemplateError, ValueError):
            # The fact check is advisory. It never fails a compile.
            warnings = []

    return CompileOut(
        ok=result.ok,
        errors=result.errors,
        log=result.log[-4000:],
        duration_s=round(result.duration_s, 2),
        pdf_hash=tex_hash(document.tex) if result.ok else "",
        fact_warnings=warnings,
    )


@router.get("/documents/{doc_id}/pdf", tags=["documents"])
async def document_pdf(
    doc_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    download: Annotated[bool, Query(description="Send as an attachment")] = False,
) -> Response:
    """The compiled PDF — the preview pane's source, and the download.

    Both read the same cache entry, so the file that lands on disk is
    byte-for-byte the one that was on screen.
    """
    document = await _load_document(session, doc_id)
    try:
        result = await compile_document(document)
    except LatexUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not result.ok or result.pdf is None:
        raise HTTPException(
            status_code=422,
            detail={"message": "the document does not compile", "errors": result.errors},
        )

    job = await require_job(session, document.job_id)
    disposition = "attachment" if download else "inline"
    filename = _document_filename(job, "pdf", document.kind)
    return Response(
        content=result.pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/documents/{doc_id}/tex", tags=["documents"])
async def document_tex(
    doc_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """The LaTeX source, for Overleaf or a local build."""
    document = await _load_document(session, doc_id)
    job = await require_job(session, document.job_id)
    filename = _document_filename(job, "tex", document.kind)
    return Response(
        content=document.tex,
        media_type="application/x-tex",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Stage 3 — refining a document by chat (`app/resume_chat.py`, `app/cover_chat.py`)
#
# A message costs one LLM call; everything else here is free. A reply never
# edits the document — it carries a fact-checked proposal that the user applies
# (or dismisses) with a separate request.
#
# The two kinds address different things — the résumé's 16 template regions, the
# letter's body paragraphs — so they are separate modules. They expose the same
# five functions and build proposals in the same shape, so these handlers pick a
# module and otherwise do not branch, and one panel in the UI renders both.
# --------------------------------------------------------------------------
def _chat_module(document: GeneratedDocument) -> Any:
    return cover_chat if document.kind == COVER_LETTER else resume_chat


async def _chat_out(session: AsyncSession, document: GeneratedDocument) -> ChatOut:
    messages = list(document.chat or [])
    return ChatOut(
        document_id=document.id,
        messages=messages,
        suggestions=await _chat_module(document).suggestions(session, document),
        tokens=sum(int(m.get("tokens") or 0) for m in messages),
    )


@router.get("/documents/{doc_id}/chat", response_model=ChatOut, tags=["documents"])
async def read_chat(
    doc_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChatOut:
    document = await _load_document(session, doc_id)
    return await _chat_out(session, document)


@router.post("/documents/{doc_id}/chat", response_model=ChatOut, tags=["documents"])
async def send_chat(
    doc_id: int,
    payload: ChatIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChatOut:
    """Ask for a change or a question about this document. One LLM call."""
    document = await _load_document(session, doc_id)
    try:
        await _chat_module(document).send_message(session, document, payload.message)
    except DocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResumeTemplateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMRateLimited as exc:
        raise HTTPException(status_code=429, detail=f"LLM limit reached: {exc}") from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"the model failed: {exc}") from exc
    return await _chat_out(session, document)


@router.post(
    "/documents/{doc_id}/chat/{message_id}/apply",
    response_model=ChatApplyOut,
    tags=["documents"],
)
async def apply_chat(
    doc_id: int,
    message_id: str,
    payload: ChatApplyIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChatApplyOut:
    """Write a reply's accepted edits into the document. Free — no LLM call."""
    document = await _load_document(session, doc_id)
    try:
        document = await _chat_module(document).apply_proposal(
            session, document, message_id, payload.accept
        )
    except DocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    job = await require_job(session, document.job_id)
    return ChatApplyOut(
        document=_document_out(document, job), chat=await _chat_out(session, document)
    )


@router.post(
    "/documents/{doc_id}/chat/{message_id}/dismiss",
    response_model=ChatOut,
    tags=["documents"],
)
async def dismiss_chat(
    doc_id: int,
    message_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChatOut:
    document = await _load_document(session, doc_id)
    try:
        await _chat_module(document).dismiss_proposal(session, document, message_id)
    except DocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _chat_out(session, document)


@router.delete("/documents/{doc_id}/chat", response_model=ChatOut, tags=["documents"])
async def clear_chat(
    doc_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChatOut:
    """Start the conversation over. Leaves the document as it is."""
    document = await _load_document(session, doc_id)
    await _chat_module(document).clear_chat(session, document)
    return await _chat_out(session, document)
