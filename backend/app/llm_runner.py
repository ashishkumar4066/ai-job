"""The deep-read pass over the board — selection, caching, progress, persistence.

Sits to `llm.py` as `match_runner.py` sits to `matching.py`: that module knows
how to judge one posting, this one knows which postings to spend money on and
what to do with the answers.

Which rows get read
-------------------
Two ways in, and nothing else:

  * **The shortlist** (`app/shortlist.py`), capped at `max_reads` per pass —
    15 by default, never more than 50. Every row must pass preferences, score
    ≥ `llm_threshold`, be confidently scored, be verified ≥ 70, and carry no
    hard blocker.
  * **On request**, one job at a time from the drawer (`job_ids`). This is
    where low-confidence rows go now. They used to route automatically — 142
    of 198 in-preference routed rows, most of which came back weak or poor.

Caching, and why it is two keys
-------------------------------
A verdict is reusable when **the JD has not changed and the profile has not
changed**. `JobMatch` is already keyed by `(job_id, profile_version)`, which
covers the profile half; `llm_content_hash` covers the JD half. So a re-run
after an ingest that changed nothing issues zero requests, which is the
acceptance criterion CLAUDE.md sets.

`match_runner` clears `llm_verdict` when it re-scores a row whose JD moved, so
"hash matches" and "verdict is current" cannot come apart.

Failure is recorded, never silently skipped
-------------------------------------------
A row the model could not screen is stamped with an `error` in its verdict
rather than left looking unread. The alternative — leaving it null — makes a
failed row indistinguishable from one that has not been reached yet, which
matters when a pass over 400 rows takes an hour and a half and can be resumed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import LLMProvider, Settings, get_settings
from app.llm import LLMError, LLMNotConfigured, LLMRateLimited, LLMScreener, profile_brief
from app.matching import MatchWeights, get_weights
from app.models import JobMatch, JobPosting, LlmUsage, utcnow
from app.profile import Profile, ProfileError, get_profile
from app.shortlist import clamp_reads, is_pending, shortlisted_rows

log = logging.getLogger(__name__)


@dataclass
class LLMRunResult:
    """Outcome of one deep-read pass."""

    profile_version: str = ""
    routed: int = 0        # rows `needs_llm` selected
    cached: int = 0        # already screened against this JD + profile
    # Validity halves written to `job_postings` this pass. Tracked apart
    # from `screened` because the two diverge once a row is re-read after a
    # profile edit: the fit half is genuinely new, while the validity half
    # is a refresh of an answer that was already correct.
    validity_written: int = 0
    screened: int = 0      # calls that produced a verdict
    failed: int = 0        # calls that did not
    skipped_budget: int = 0  # left unread because the run hit its request cap
    requests: int = 0
    tokens: int = 0
    bands: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    blocked: int = 0       # sponsorship / location blockers the JD prose named
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def duration_ms(self) -> int:
        if not self.started_at or not self.finished_at:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


@dataclass
class LLMProgress:
    """Live state for a running pass, polled by the dashboard.

    Same shape of answer as `ingest_state.RunProgress`, for the same reason: a
    pass that takes an hour and a half cannot be a synchronous HTTP request,
    and a bare spinner over that long reads as broken.
    """

    total: int = 0
    done: int = 0
    cached: int = 0
    failed: int = 0
    tokens: int = 0
    current: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.started_at is not None and self.finished_at is None


class LLMTracker:
    """One tracker per process, plus the lock that serializes passes.

    The lock is this module's own rather than the ingest one: a deep read and a
    board sweep touch different tables and there is no reason a refresh should
    block behind 90 minutes of screening.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.progress = LLMProgress()
        self.result: LLMRunResult | None = None

    @property
    def busy(self) -> bool:
        return self.lock.locked()


tracker = LLMTracker()


async def _routed_rows(
    session: AsyncSession, version: str, weights: MatchWeights
) -> list[tuple[JobMatch, JobPosting]]:
    """The shortlist, in reading order. Kept under this name for the CLI.

    Still gated on `prefs_pass` (through `shortlist_clause`): without it the
    first paid Cerebras run was quoted for the preference-passing rows and then
    read 118 more (470k tokens, 43% of the bill) that preferences exclude.
    """
    return await shortlisted_rows(session, version, weights.llm_threshold)


async def _requested_rows(
    session: AsyncSession, version: str, job_ids: list[int]
) -> list[tuple[JobMatch, JobPosting]]:
    """Specific jobs asked for from the drawer — shortlist or not."""
    stmt = (
        select(JobMatch, JobPosting)
        .join(JobPosting, JobPosting.id == JobMatch.job_id)
        .where(JobMatch.profile_version == version, JobPosting.id.in_(job_ids))
    )
    return [(m, j) for m, j in (await session.execute(stmt)).all()]


async def run_llm_screen(
    session: AsyncSession,
    *,
    force: bool = False,
    limit: int | None = None,
    job_ids: list[int] | None = None,
    settings: Settings | None = None,
    profile: Profile | None = None,
    weights: MatchWeights | None = None,
    progress: LLMProgress | None = None,
    screener: LLMScreener | None = None,
) -> LLMRunResult:
    """Deep-read the shortlist (or `job_ids`), up to the per-pass cap."""
    settings = settings or get_settings()
    weights = weights or get_weights()
    result = LLMRunResult(started_at=utcnow())

    try:
        profile = profile or get_profile()
    except ProfileError as exc:
        result.error = str(exc)
        result.finished_at = utcnow()
        log.error("llm.no_profile", extra={"error": str(exc)})
        return result

    result.profile_version = profile.version
    if job_ids:
        rows = await _requested_rows(session, profile.version, job_ids)
    else:
        rows = await _routed_rows(session, profile.version, weights)
    result.routed = len(rows)

    pending = [(match, job) for match, job in rows if force or is_pending(match, job)]
    result.cached = result.routed - len(pending)

    # Clamped here, not only at the API, so no caller — CLI, test, or a stale
    # `.env` still carrying the old default of 500 — can spend past the ceiling.
    budget = clamp_reads(limit, default=settings.llm_max_requests_per_run)
    if len(pending) > budget:
        result.skipped_budget = len(pending) - budget
        pending = pending[:budget]

    if progress is not None:
        progress.total = len(pending)
        progress.cached = result.cached
        progress.started_at = result.started_at

    if not pending:
        result.finished_at = utcnow()
        if progress is not None:
            progress.finished_at = result.finished_at
        log.info(
            "llm.run_done",
            extra={"routed": result.routed, "cached": result.cached, "screened": 0},
        )
        return result

    brief = profile_brief(profile)
    now = utcnow()

    try:
        owned = screener is None
        active = screener or LLMScreener(settings)
    except LLMNotConfigured as exc:
        result.error = str(exc)
        result.finished_at = utcnow()
        if progress is not None:
            progress.error = result.error
            progress.finished_at = result.finished_at
        log.warning("llm.not_configured", extra={"error": str(exc)})
        return result

    # Spend so far, from the ledger, reaching back as far as the provider's
    # longest window (a day on Groq/Cerebras, a month on Mistral). No cap has a
    # response header, so without this a restarted pass believes it has spent
    # nothing and walks into it.
    provider = settings.llm
    active.limiter.seed(
        await recent_usage(
            session, provider.model, window=timedelta(seconds=active.limiter.longest_period)
        )
    )

    total = len(pending)
    log.info(
        "llm.run_start",
        extra={
            "provider": provider.name,
            "model": provider.model,
            "routed": result.routed,
            "cached": result.cached,
            "to_read": total,
            "skipped_budget": result.skipped_budget,
        },
    )

    async with active if owned else _null_context(active):
        for index, (match, job) in enumerate(pending, start=1):
            row = f"{index}/{total}"
            label = f"{job.company} — {job.title}"[:120]
            tokens_before = active.tokens_spent
            if progress is not None:
                progress.current = label
            log.info("llm.screen_start", extra={"row": row, "job_id": job.id, "job": label})
            try:
                screen = await active.screen(
                    title=job.title,
                    company=job.company,
                    description_text=job.description_text,
                    profile_text=brief,
                )
            except LLMRateLimited as exc:
                # Not this row's fault, and every row after it would hit the
                # same wall. Leave it unread (so a later pass picks it up) and
                # stop, rather than stamp the rest of the queue as failed.
                result.error = str(exc)[:500]
                if progress is not None:
                    progress.error = result.error
                log.warning(
                    "llm.run_stopped_rate_limited",
                    extra={
                        "row": row,
                        "job_id": job.id,
                        "retry_after_s": exc.retry_after,
                        "error": str(exc),
                    },
                )
                # Calls billed on this row before the stop still count.
                _write_usage(session, active, job.id)
                break
            except LLMError as exc:
                # Recorded, not skipped: a failed row must be distinguishable
                # from an unreached one when this is resumed.
                match.llm_verdict = {"error": str(exc)[:500], **_attribution(provider)}
                match.llm_used = False
                result.failed += 1
                if progress is not None:
                    progress.failed += 1
                log.warning(
                    "llm.screen_failed",
                    extra={
                        "row": row,
                        "job_id": job.id,
                        "job": label,
                        "tokens": active.tokens_spent - tokens_before,
                        "error": str(exc)[:200],
                    },
                )
            else:
                # One call, two homes. The validity half goes on the POSTING
                # keyed by `content_hash` alone, so it survives a profile edit
                # and stays usable by the validity layer even if this row
                # later drops out of LLM routing on its new score. The fit
                # half goes on the MATCH, keyed by
                # `content_hash + profile_version`. See
                # `JobScreen.VALIDITY_FIELDS` for why the seam is there.
                validity = screen.validity_half()
                validity.update(_attribution(provider))
                job.llm_validity = validity
                job.llm_validity_hash = job.content_hash
                result.validity_written += 1

                verdict = screen.fit_half()
                verdict.update(_attribution(provider))
                match.llm_verdict = verdict
                match.llm_used = True
                match.llm_content_hash = job.content_hash
                result.screened += 1
                result.bands[screen.fit_band] = result.bands.get(screen.fit_band, 0) + 1
                result.statuses[screen.posting_status] = (
                    result.statuses.get(screen.posting_status, 0) + 1
                )
                if screen.blocked:
                    result.blocked += 1
                log.info(
                    "llm.screen_done",
                    extra={
                        "row": row,
                        "job_id": job.id,
                        "band": screen.fit_band,
                        "status": screen.posting_status,
                        "blocked": screen.blocked,
                        "tokens": active.tokens_spent - tokens_before,
                        "total_tokens": active.tokens_spent,
                        "elapsed_s": round((utcnow() - result.started_at).total_seconds()),
                    },
                )
            match.scored_at = now
            _write_usage(session, active, job.id)

            if progress is not None:
                progress.done = index
                progress.tokens = active.tokens_spent

            # Commit as we go. A 90-minute pass that loses everything to a
            # crash at minute 80 is not resumable, and resumability is the
            # whole reason failures are recorded.
            if index % 10 == 0:
                await session.commit()

    await session.commit()
    result.requests = active.requests_made
    result.tokens = active.tokens_spent
    result.finished_at = utcnow()
    if progress is not None:
        progress.finished_at = result.finished_at
        progress.current = None
        progress.tokens = result.tokens

    log.info(
        "llm.run_done",
        extra={
            "profile_version": result.profile_version,
            "routed": result.routed,
            "cached": result.cached,
            "screened": result.screened,
            "failed": result.failed,
            "requests": result.requests,
            "tokens": result.tokens,
            "duration_ms": result.duration_ms,
        },
    )
    return result


async def recent_usage(
    session: AsyncSession, model: str, *, window: timedelta = timedelta(days=1)
) -> list[tuple[datetime, int]]:
    """`(created_at, total_tokens)` for every call billed to `model` in `window`."""
    since = utcnow() - window
    rows = await session.execute(
        select(LlmUsage.created_at, LlmUsage.total_tokens).where(
            LlmUsage.model == model, LlmUsage.created_at >= since
        )
    )
    return [(created_at, int(tokens)) for created_at, tokens in rows]


async def measured_completion_tokens(
    session: AsyncSession, model: str, *, default: int, sample: int = 200, min_sample: int = 20
) -> int:
    """Average completion tokens of `model`'s recent successful calls, never below `default`.

    `llm_completion_reserve` was measured on Groq's qwen (339-600 tokens).
    Cerebras' qwen-3.8-27b reasons at length even at `reasoning_effort=low` —
    1,522 on average over its first 315 calls — so pricing a pass off the
    reserve quoted about 60% of the real tokens. The ledger knows the real
    number; below `min_sample` calls it is too noisy and the default stands.
    """
    recent = (
        select(LlmUsage.completion_tokens)
        .where(LlmUsage.model == model, LlmUsage.status_code == 200)
        .order_by(LlmUsage.created_at.desc())
        .limit(sample)
    )
    values = [int(v) for v in (await session.scalars(recent)).all()]
    if len(values) < min_sample:
        return default
    return max(default, round(sum(values) / len(values)))


def _attribution(provider: LLMProvider) -> dict[str, str]:
    """Who wrote a verdict. Cached verdicts outlive a provider switch."""
    return {"provider": provider.name, "model": provider.model}


def _write_usage(session: AsyncSession, screener: LLMScreener, job_id: int) -> None:
    for event in screener.drain_usage():
        session.add(
            LlmUsage(
                created_at=event.created_at,
                model=event.model,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                total_tokens=event.total_tokens,
                status_code=event.status_code,
                job_id=job_id,
            )
        )


class _null_context:
    """Use a caller-supplied screener without closing it on the way out."""

    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *exc: object) -> None:
        return None
