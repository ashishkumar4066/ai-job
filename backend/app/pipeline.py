"""The Matches "Run" pipeline — validity, then ranking, then a costed estimate.

What one click does
-------------------
Pressing Run in the Matches view executes the two free passes back to back and
then **stops**, handing back what the expensive pass would cost:

    1. validity   (`validation_runner`)  — whole board, ~12s, free
    2. ranking    (`match_runner`)       — open+eligible, ~10s, free, and
                                           applies the preference gate
    3. estimate   (this module)          — how many rows the deep read would
                                           touch, and what that costs

Step 3 is not step 4. The deep read runs under the provider's rate limits —
on Groq a 200,000 tokens/day cap that a full ~430-row pass exceeds several
times over — so it is never started by the same click that ranked the board. The caller sees the price and confirms, then calls `POST /matches/llm`.

Why validity runs first
-----------------------
Ordering is load-bearing. `prefs.yaml` can gate on `validity.min_score`, and
the preference gate runs inside the ranking pass — so validity has to be
current before ranking, or a first-ever Run would gate against NULL scores and
admit everything. Ranking-then-validity would need a second ranking pass to
converge.

Why this is a background task
-----------------------------
~22 seconds of combined CPU is past the point where a synchronous HTTP request
is honest about what it is doing, and the dashboard already has the vocabulary
for a polled background job from the ingest sweep. Same shape, same lock
discipline.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.llm import build_messages, estimate_tokens, profile_brief
from app.llm_runner import measured_completion_tokens, recent_usage
from app.ratelimit import WINDOWS
from app.match_runner import MatchRunResult, run_matching
from app.matching import get_weights
from app.models import utcnow
from app.prefs import get_prefs
from app.profile import Profile, ProfileError, get_profile
from app.shortlist import clamp_reads, is_pending, shortlisted_rows
from app.validation_runner import ValidityRunResult, run_validation

log = logging.getLogger(__name__)

Stage = Literal["idle", "validity", "ranking", "estimating", "done", "failed"]


@dataclass
class LLMEstimate:
    """What the deep read would cost, if you asked for it.

    Every number here is derived from the real prompts that would be sent —
    `estimate_tokens` over `build_messages` for each pending row — not from a
    per-row average. The JD length varies by an order of magnitude across
    boards, so an average would misprice a pass by minutes.
    """

    # Shortlisted rows, how many are already read, and how many this pass
    # reads — at most `cap`. `over_cap` wait for a later pass.
    routed: int = 0
    cached: int = 0
    pending: int = 0
    cap: int = 0
    over_cap: int = 0
    est_tokens: int = 0
    est_minutes: float = 0.0
    requests: int = 0
    # The provider's long token cap — 200k per DAY on Groq, per MONTH on
    # Mistral — and this pass's share of it. Tokens, not requests: at ~1,800
    # tokens a row Groq's daily cap binds at ~110 rows, long before 1,000
    # requests/day does.
    token_cap: int = 0
    token_cap_period: str = ""  # "day" | "month" | "" when the provider has none
    tokens_used: int = 0
    cap_budget_pct: float = 0.0
    # Pending rows, in the order the pass reads them, whose estimates fit in
    # what is left of the cap. The rest wait for the window.
    fits_in_cap: int = 0
    provider: str = ""
    model: str = ""
    configured: bool = True
    note: str = ""


@dataclass
class PipelineProgress:
    """Live state for `GET /matches/run/status`."""

    stage: Stage = "idle"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    profile_version: str = ""
    prefs_version: str = ""
    validity: ValidityRunResult | None = None
    ranking: MatchRunResult | None = None
    estimate: LLMEstimate | None = None

    @property
    def running(self) -> bool:
        return self.stage in ("validity", "ranking", "estimating")


class PipelineTracker:
    """Owns the lock and the latest progress. One pipeline at a time."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.progress = PipelineProgress()

    @property
    def busy(self) -> bool:
        return self.lock.locked()


tracker = PipelineTracker()


async def estimate_llm_pass(
    session: AsyncSession,
    *,
    limit: int | None = None,
    profile: Profile | None = None,
    settings: Settings | None = None,
) -> LLMEstimate:
    """Price the deep read without calling anything.

    Selects rows exactly as `llm_runner.run_llm_screen` does — the same
    `shortlist.shortlisted_rows`, the same cache test, the same `clamp_reads`
    cap — so the quote and the pass cannot disagree. They did once: the quote
    counted preference-passing rows only and the pass read 118 more.
    """
    settings = settings or get_settings()
    weights = get_weights()
    provider = settings.llm
    est = LLMEstimate(
        model=provider.model, provider=provider.name, configured=bool(provider.api_key)
    )

    try:
        profile = profile or get_profile()
    except ProfileError as exc:
        est.note = str(exc)
        return est

    routed = await shortlisted_rows(session, profile.version, weights.llm_threshold)
    est.routed = len(routed)

    uncached = [(m, j) for m, j in routed if is_pending(m, j)]
    est.cached = est.routed - len(uncached)
    est.cap = clamp_reads(limit, default=settings.llm_max_requests_per_run)
    pending = uncached[: est.cap]
    est.over_cap = len(uncached) - len(pending)
    est.pending = len(pending)
    est.requests = len(pending)

    if not pending:
        est.note = "every shortlisted job is already read — this pass would cost nothing"
        return est

    # The provider's long token cap: per day on Groq and Cerebras, per month on
    # Mistral. Cerebras' per-hour cap equals its daily one, so the day is shown.
    cap_name = next(
        (n for n in ("tokens_per_day", "tokens_per_month") if n in provider.limits), None
    )
    left_in_cap = None
    if cap_name is not None:
        period = WINDOWS[cap_name][0]
        est.token_cap = provider.limits[cap_name]
        est.token_cap_period = "day" if cap_name == "tokens_per_day" else "month"
        est.tokens_used = sum(
            tokens
            for _at, tokens in await recent_usage(
                session, provider.model, window=timedelta(seconds=period)
            )
        )
        left_in_cap = max(0, est.token_cap - est.tokens_used)

    brief = profile_brief(profile)
    completion = await measured_completion_tokens(
        session, provider.model, default=settings.llm_completion_reserve
    )
    total = 0
    for _match, job in pending:
        messages = build_messages(
            title=job.title,
            company=job.company,
            description_text=job.description_text,
            profile_text=brief,
            max_jd_chars=settings.llm_max_jd_chars,
        )
        total += estimate_tokens(messages, completion)
        if left_in_cap is None or total <= left_in_cap:
            est.fits_in_cap += 1
    est.est_tokens = total

    # Minutes are bounded by whichever per-minute limit binds first.
    # Wall clock is bounded by whichever short limit binds first. A per-second
    # limit of N is N*60 a minute (headroom is applied the same way the
    # limiter applies it).
    headroom = settings.llm_budget_headroom
    per_minute_demand = {
        "tokens_per_minute": (total, 1),
        "requests_per_minute": (len(pending), 1),
        "requests_per_second": (len(pending), 60),
    }
    est.est_minutes = round(
        max(
            (
                amount / max(1.0, provider.limits[name] * headroom * scale)
                for name, (amount, scale) in per_minute_demand.items()
                if name in provider.limits
            ),
            default=0.0,
        ),
        1,
    )
    if est.token_cap:
        est.cap_budget_pct = round(100 * total / est.token_cap, 1)

    if not est.configured:
        est.note = "GROQ_API_KEY is not configured — the deep read cannot run"

    return est


async def run_pipeline(
    session: AsyncSession,
    *,
    force: bool = False,
    progress: PipelineProgress | None = None,
) -> PipelineProgress:
    """Validity, then ranking, then the estimate. Never the deep read.

    A failure in validity does not abort ranking: the two are independent, and
    a ranked list with stale validity is far more useful than no list at all.
    The stage and the error are both recorded so the dashboard can say which
    half degraded.
    """
    p = progress or PipelineProgress()
    p.started_at = utcnow()
    p.error = None

    try:
        p.prefs_version = get_prefs().version
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        log.warning("pipeline.prefs_unreadable", extra={"error": str(exc)})

    # --- 1. Validity -------------------------------------------------------
    p.stage = "validity"
    try:
        p.validity = await run_validation(session, force=force)
    except Exception as exc:  # noqa: BLE001
        p.error = f"validity failed: {type(exc).__name__}: {exc}"
        log.exception("pipeline.validity_failed")

    # --- 2. Ranking (this is where the preference gate is applied) ---------
    p.stage = "ranking"
    try:
        p.ranking = await run_matching(session, force=force)
        p.profile_version = p.ranking.profile_version
    except Exception as exc:  # noqa: BLE001
        p.error = f"ranking failed: {type(exc).__name__}: {exc}"
        p.stage = "failed"
        p.finished_at = utcnow()
        log.exception("pipeline.ranking_failed")
        return p

    # --- 3. Price the deep read, and stop ----------------------------------
    p.stage = "estimating"
    try:
        p.estimate = await estimate_llm_pass(session)
    except Exception as exc:  # noqa: BLE001
        p.error = f"estimate failed: {type(exc).__name__}: {exc}"
        log.exception("pipeline.estimate_failed")

    p.stage = "done"
    p.finished_at = utcnow()

    log.info(
        "pipeline.done",
        extra={
            "profile_version": p.profile_version,
            "prefs_version": p.prefs_version,
            "validity_scored": p.validity.scored if p.validity else None,
            "ranked": p.ranking.considered if p.ranking else None,
            "matching_prefs": p.ranking.matching_prefs if p.ranking else None,
            "llm_pending": p.estimate.pending if p.estimate else None,
            "llm_est_minutes": p.estimate.est_minutes if p.estimate else None,
        },
    )
    return p
