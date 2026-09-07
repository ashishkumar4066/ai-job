"""The ingest pipeline.

One run =
  1. fetch every configured board concurrently (failures isolated per source),
  2. upsert each board's postings by `source_key`,
  3. close any previously-open job the board no longer lists,
  4. alert on jobs whose `first_seen_at` equals this run's timestamp.

Idempotency guarantees:
  * `source_key` is unique, so a re-run updates instead of inserting.
  * `first_seen_at` is only ever set at insert time, so a re-run produces no
    new-job events.
  * A row whose content hash is unchanged has only `last_seen_at` touched.

Network I/O runs concurrently; database writes run sequentially, because
SQLite permits a single writer and per-source ordering makes runs reproducible.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import BaseAdapter, get_adapter
from app.companies import load_companies
from app.config import Settings, get_settings
from app.db import session_scope
from app.eligibility import FilterConfig, evaluate, load_filters
from app.ingest_state import RunProgress
from app.models import IngestRun, JobPosting, utcnow
from app.normalize import content_hash
from app.notify import Notifier, build_notifier
from app.schemas import CompanyConfig, IngestRunOut, SourceResultOut
from app.schemas import JobPosting as NormalizedJob

log = logging.getLogger(__name__)


class FetchOutcome:
    """What one board returned — either postings or the error that stopped it."""

    __slots__ = ("company", "adapter", "postings", "error", "duration_ms")

    def __init__(
        self,
        company: CompanyConfig,
        adapter: BaseAdapter | None,
        postings: list[NormalizedJob] | None,
        error: str | None,
        duration_ms: int,
    ) -> None:
        self.company = company
        self.adapter = adapter
        self.postings = postings
        self.error = error
        self.duration_ms = duration_ms

    @property
    def ok(self) -> bool:
        return self.error is None


def _apply_eligibility(postings: list[NormalizedJob], filters: FilterConfig) -> int:
    """The eligibility stage: after normalize, before persist.

    Mutates each posting in place with the verdict and its reasons. Nothing is
    dropped — a failing job is stored flagged, so the rules stay auditable and
    tunable against real data. Returns how many passed.
    """
    eligible = 0
    for posting in postings:
        result = evaluate(
            location_eligibility=posting.location_eligibility,
            remote=posting.remote,
            salary_min=posting.salary_min,
            salary_max=posting.salary_max,
            salary_currency=posting.salary_currency,
            title=posting.title,
            description_text=posting.description_text,
            filters=filters,
        )
        posting.eligibility_pass = result.passed
        posting.eligibility_reasons = result.reasons
        eligible += int(result.passed)
    return eligible


async def _fetch_one(
    company: CompanyConfig,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    filters: FilterConfig,
    progress: RunProgress | None = None,
) -> FetchOutcome:
    """Fetch + normalize + filter one board. Never raises — errors are returned."""
    started = time.perf_counter()
    adapter: BaseAdapter | None = None
    async with semaphore:
        # Marked here, not before the semaphore: a UI blocked on this run should
        # see which boards are genuinely in flight, not which are queued.
        if progress is not None:
            progress.mark(company.source_id, "fetching")
        try:
            adapter = get_adapter(company.ats, client=client)
            raws = await adapter.fetch(company)
            postings = adapter.normalize_all(raws, company)
            eligible = _apply_eligibility(postings, filters)
            elapsed = int((time.perf_counter() - started) * 1000)
            log.info(
                "ingest.fetched",
                extra={
                    "source_id": company.source_id,
                    "company": company.company,
                    "ats": company.ats,
                    "raw": len(raws),
                    "normalized": len(postings),
                    "eligible": eligible,
                    "duration_ms": elapsed,
                },
            )
            return FetchOutcome(company, adapter, postings, None, elapsed)
        except Exception as exc:  # isolate: never abort the run
            elapsed = int((time.perf_counter() - started) * 1000)
            log.error(
                "ingest.source_failed",
                extra={
                    "source_id": company.source_id,
                    "company": company.company,
                    "ats": company.ats,
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_ms": elapsed,
                },
            )
            return FetchOutcome(company, adapter, None, f"{type(exc).__name__}: {exc}", elapsed)


def _hash_of(posting: NormalizedJob) -> str:
    return content_hash(
        title=posting.title,
        locations=posting.locations,
        department=posting.department,
        remote=posting.remote,
        apply_url=posting.apply_url,
        description_text=posting.description_text,
        posted_at=posting.posted_at,
        updated_at=posting.updated_at,
    )


def _dedupe(postings: list[NormalizedJob], source_id: str) -> list[NormalizedJob]:
    """Guard against a board listing the same job id twice in one response."""
    out: list[NormalizedJob] = []
    seen: set[str] = set()
    for posting in postings:
        if posting.source_key in seen:
            log.warning(
                "ingest.duplicate_in_feed",
                extra={"source_id": source_id, "source_key": posting.source_key},
            )
            continue
        seen.add(posting.source_key)
        out.append(posting)
    return out


async def _persist_source(
    session: AsyncSession,
    outcome: FetchOutcome,
    run_ts: datetime,
    settings: Settings,
) -> SourceResultOut:
    """Upsert one board's postings and run its closure sweep."""
    company = outcome.company
    result = SourceResultOut(
        source_id=company.source_id,
        company=company.company,
        ats=company.ats,
        duration_ms=outcome.duration_ms,
    )

    existing_rows = (
        await session.execute(
            select(JobPosting).where(JobPosting.source_id == company.source_id)
        )
    ).scalars().all()
    existing = {row.source_key: row for row in existing_rows}
    open_before = sum(1 for row in existing_rows if row.status == "open")

    postings = _dedupe(outcome.postings or [], company.source_id)
    result.fetched = len(postings)
    result.eligible = sum(1 for p in postings if p.eligibility_pass)
    seen_keys: set[str] = set()

    for posting in postings:
        seen_keys.add(posting.source_key)
        digest = _hash_of(posting)
        row = existing.get(posting.source_key)

        if row is None:
            session.add(
                JobPosting(
                    source_key=posting.source_key,
                    source_id=company.source_id,
                    ats=posting.ats,
                    company=posting.company,
                    title=posting.title,
                    locations=posting.locations,
                    remote=posting.remote,
                    department=posting.department,
                    apply_url=posting.apply_url,
                    description_html=posting.description_html,
                    description_text=posting.description_text,
                    posted_at=posting.posted_at,
                    updated_at=posting.updated_at,
                    # New-job detection keys off this being exactly the run stamp.
                    first_seen_at=run_ts,
                    last_seen_at=run_ts,
                    status="open",
                    content_hash=digest,
                    location_eligibility=posting.location_eligibility,
                    timezone_restrictions=posting.timezone_restrictions,
                    salary_min=posting.salary_min,
                    salary_max=posting.salary_max,
                    salary_currency=posting.salary_currency,
                    is_us_employer=posting.is_us_employer,
                    eligibility_pass=posting.eligibility_pass,
                    eligibility_reasons=posting.eligibility_reasons,
                    raw_json=posting.raw_json,
                )
            )
            result.new += 1
            continue

        # Seen before: always refresh liveness, but only rewrite content when
        # it actually changed. `first_seen_at` is never touched.
        row.last_seen_at = run_ts
        changed = row.content_hash != digest

        if row.status != "open":
            # The board is listing it again — reopen rather than duplicate.
            row.status = "open"
            row.closed_at = None
            changed = True

        if changed:
            row.title = posting.title
            row.locations = posting.locations
            row.remote = posting.remote
            row.department = posting.department
            row.apply_url = posting.apply_url
            row.description_html = posting.description_html
            row.description_text = posting.description_text
            row.posted_at = posting.posted_at
            row.updated_at = posting.updated_at
            row.content_hash = digest
            row.raw_json = posting.raw_json
            result.updated += 1

        # The verdict is refreshed on every run regardless of `content_hash`,
        # because editing `filters.yaml` must re-decide existing rows even
        # though the posting itself never changed.
        row.location_eligibility = posting.location_eligibility
        row.timezone_restrictions = posting.timezone_restrictions
        row.salary_min = posting.salary_min
        row.salary_max = posting.salary_max
        row.salary_currency = posting.salary_currency
        row.is_us_employer = posting.is_us_employer
        row.eligibility_pass = posting.eligibility_pass
        row.eligibility_reasons = posting.eligibility_reasons

    # --- Closure by disappearance ------------------------------------------
    adapter_suspicious = outcome.adapter.empty_result_is_suspicious if outcome.adapter else True
    empty_but_had_jobs = not postings and open_before > 0
    if settings.guard_empty_fetches and empty_but_had_jobs and adapter_suspicious:
        # e.g. a typo'd Ashby slug answers 200 with `{"jobs": []}`; closing the
        # whole board on that would be data loss dressed up as a signal.
        result.skipped_closure_sweep = True
        log.warning(
            "ingest.closure_sweep_skipped",
            extra={
                "source_id": company.source_id,
                "reason": "empty fetch for a source that previously had open jobs",
                "open_before": open_before,
            },
        )
    else:
        for row in existing_rows:
            if row.status == "open" and row.source_key not in seen_keys:
                row.status = "closed"
                row.closed_at = run_ts
                result.closed += 1

    log.info(
        "ingest.source_done",
        extra={
            "source_id": company.source_id,
            "company": company.company,
            "ats": company.ats,
            "fetched": result.fetched,
            "new": result.new,
            "updated": result.updated,
            "closed": result.closed,
            "errored": result.errored,
        },
    )
    return result


async def _new_jobs_for_run(
    session: AsyncSession, run_ts: datetime, *, eligible_only: bool = False
) -> list[JobPosting]:
    """New jobs = rows whose `first_seen_at` is exactly this run's stamp.

    `eligible_only` is the alert gate: ineligible jobs are still stored and
    still counted as new, they just never reach Telegram.
    """
    query = select(JobPosting).where(
        JobPosting.first_seen_at == run_ts, JobPosting.status == "open"
    )
    if eligible_only:
        query = query.where(JobPosting.eligibility_pass.is_(True))

    return list(
        (await session.execute(query.order_by(JobPosting.company, JobPosting.title)))
        .scalars()
        .all()
    )


async def _last_fetch_times(session: AsyncSession) -> dict[str, datetime]:
    """Most recent successful fetch per source, from the rows it wrote.

    Used only by the `min_fetch_interval_minutes` throttle. Deriving it from
    `max(last_seen_at)` avoids a second bookkeeping table, and is exact: every
    successful fetch stamps `last_seen_at` on every posting it saw.
    """
    rows = await session.execute(
        select(JobPosting.source_id, func.max(JobPosting.last_seen_at)).group_by(
            JobPosting.source_id
        )
    )
    return {source_id: seen for source_id, seen in rows.all() if seen is not None}


def _is_throttled(
    company: CompanyConfig, last_fetch: dict[str, datetime], now: datetime
) -> bool:
    """True when a source's configured minimum fetch gap has not elapsed.

    Remotive's terms ask for at most ~4 calls a day while the scheduler runs
    hourly, so the cap has to live here rather than in the schedule.
    """
    interval = company.min_fetch_interval_minutes
    if not interval:
        return False
    previous = last_fetch.get(company.source_id)
    if previous is None:
        return False
    return (now - previous).total_seconds() < interval * 60


async def last_completed_run_at(session: AsyncSession) -> datetime | None:
    """When the newest *finished* sweep ended, or None if none ever has.

    Interrupted runs leave `finished_at` NULL and deliberately do not count —
    half a sweep is not fresh data, and a killed dev server should not make the
    boards look freshly fetched for the rest of the day.
    """
    return await session.scalar(select(func.max(IngestRun.finished_at)))


def refresh_cutoff(settings: Settings | None = None) -> datetime:
    """Stored data finishing before this instant is stale and due a sweep.

    A rolling window, not a calendar boundary: whoever asks, the answer is
    "was the last sweep less than N hours ago", so the gate behaves the same
    at 23:00 as at 00:05 and needs nothing from the caller's timezone.
    """
    settings = settings or get_settings()
    return utcnow() - timedelta(hours=settings.refresh_window_hours)


async def is_fresh(
    session: AsyncSession, cutoff: datetime | None = None, *, settings: Settings | None = None
) -> datetime | None:
    """The last finished run if it is inside the window, else None.

    Returning the timestamp rather than a bool lets callers report *why* they
    skipped without a second query.
    """
    last_finished = await last_completed_run_at(session)
    if last_finished is None:
        return None
    boundary = cutoff or refresh_cutoff(settings)
    return last_finished if last_finished >= boundary else None


async def run_ingest(
    *,
    companies: list[CompanyConfig] | None = None,
    notifier: Notifier | None = None,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
    notify: bool = True,
    progress: RunProgress | None = None,
) -> IngestRunOut:
    """Execute one full ingest run and return its per-source summary.

    `progress` is an optional live sink for a caller that is showing the run to
    a human while it happens; it never affects the outcome.
    """
    settings = settings or get_settings()
    run_ts = utcnow()

    if companies is None:
        companies = load_companies(settings.companies_file)

    if progress is not None:
        progress.begin(companies)

    log.info(
        "ingest.run_start",
        extra={"sources": len(companies), "run_ts": run_ts.isoformat()},
    )

    async with session_scope() as session:
        run = IngestRun(started_at=run_ts)
        session.add(run)
        await session.flush()
        run_id = run.id

    if progress is not None:
        progress.run_id = run_id

    if not companies:
        log.warning("ingest.no_sources")
        async with session_scope() as session:
            run = await session.get(IngestRun, run_id)
            if run is not None:
                run.finished_at = utcnow()
        return IngestRunOut(
            run_id=run_id,
            started_at=run_ts,
            finished_at=utcnow(),
            fetched=0,
            new=0,
            updated=0,
            closed=0,
            errored=0,
            notified=0,
            sources=[],
        )

    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": "personal-job-aggregator/1.0", "Accept": "application/json"},
    )

    try:
        filters = load_filters(settings.filters_file)
        log.info(
            "ingest.filters",
            extra={
                "allowed_locations": filters.allowed_locations,
                "require_remote": filters.require_remote,
                "min_annual_salary_inr": filters.min_annual_salary_inr,
            },
        )

        async with session_scope() as session:
            last_fetch = await _last_fetch_times(session)

        results: list[SourceResultOut] = []

        def record(result: SourceResultOut) -> SourceResultOut:
            results.append(result)
            if progress is not None:
                progress.apply(result)
            return result

        due: list[CompanyConfig] = []
        for company in companies:
            if _is_throttled(company, last_fetch, run_ts):
                log.info(
                    "ingest.source_throttled",
                    extra={
                        "source_id": company.source_id,
                        "min_interval_minutes": company.min_fetch_interval_minutes,
                    },
                )
                record(
                    SourceResultOut(
                        source_id=company.source_id,
                        company=company.company,
                        ats=company.ats,
                        throttled=True,
                    )
                )
                continue
            due.append(company)

        semaphore = asyncio.Semaphore(settings.ingest_concurrency)
        outcomes = await asyncio.gather(
            *(
                _fetch_one(company, http_client, semaphore, filters, progress)
                for company in due
            )
        )

        for outcome in outcomes:
            if not outcome.ok:
                record(
                    SourceResultOut(
                        source_id=outcome.company.source_id,
                        company=outcome.company.company,
                        ats=outcome.company.ats,
                        errored=1,
                        ok=False,
                        error=outcome.error,
                        duration_ms=outcome.duration_ms,
                    )
                )
                continue

            # One transaction per source: a DB failure on one board cannot roll
            # back the boards that already succeeded.
            try:
                async with session_scope() as session:
                    record(await _persist_source(session, outcome, run_ts, settings))
            except Exception as exc:
                log.exception(
                    "ingest.persist_failed",
                    extra={"source_id": outcome.company.source_id},
                )
                record(
                    SourceResultOut(
                        source_id=outcome.company.source_id,
                        company=outcome.company.company,
                        ats=outcome.company.ats,
                        errored=1,
                        ok=False,
                        error=f"persist failed: {type(exc).__name__}: {exc}",
                        duration_ms=outcome.duration_ms,
                    )
                )

        # Only jobs that cleared the eligibility filter are alertable. The
        # rest are stored and browsable, they just do not buzz the phone.
        async with session_scope() as session:
            alertable = await _new_jobs_for_run(session, run_ts, eligible_only=True)

        notified = 0
        if notify and alertable:
            active_notifier = notifier or build_notifier(settings, client=http_client)
            try:
                notified = await active_notifier.notify_new_jobs(alertable)
            except Exception:
                # Alerts are best-effort; the data is already committed.
                log.exception("ingest.notify_failed")

        totals = {
            "fetched": sum(r.fetched for r in results),
            "eligible": sum(r.eligible for r in results),
            "new": sum(r.new for r in results),
            "updated": sum(r.updated for r in results),
            "closed": sum(r.closed for r in results),
            "errored": sum(r.errored for r in results),
        }
        finished_at = utcnow()

        async with session_scope() as session:
            run = await session.get(IngestRun, run_id)
            if run is not None:
                run.finished_at = finished_at
                run.fetched = totals["fetched"]
                run.eligible = totals["eligible"]
                run.new = totals["new"]
                run.updated = totals["updated"]
                run.closed = totals["closed"]
                run.errored = totals["errored"]
                run.notified = notified
                run.sources = [r.model_dump() for r in results]

        log.info(
            "ingest.run_done",
            extra={
                "run_id": run_id,
                "sources": len(results),
                "notified": notified,
                "duration_ms": int((finished_at - run_ts).total_seconds() * 1000),
                **totals,
            },
        )

        return IngestRunOut(
            run_id=run_id,
            started_at=run_ts,
            finished_at=finished_at,
            notified=notified,
            sources=results,
            **totals,
        )
    finally:
        if owns_client:
            await http_client.aclose()
