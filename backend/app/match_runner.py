"""The scoring pass — runs `matching.evaluate` over the board and persists it.

Separate from ingest on purpose. A sweep already takes ~90s with Wellfound in
it, and scoring 917 rows adds ~8s of pure CPU; folding that into the page-load
refresh would push the dashboard's first paint past where it feels broken.
This runs on its own trigger and reports progress the way `/ingest/status`
does.

Scoring surface
---------------
`status = open AND eligibility_pass = true`. Phase 1's filter is a hard binary
gate; fit is a ranking *on top of it*. Ranking rows already rejected on
location or pay would spend CPU building an ordering over jobs that cannot be
taken, and would put them in front of me in a list whose whole purpose is
"what should I apply to next".

Idempotence
-----------
Keyed on `(job_id, profile_version)`. Re-running with an unchanged profile and
an unchanged board rewrites nothing and reports `skipped`. Editing
`profile.yaml` changes `profile_version`, so the next run scores into fresh
rows and the old ones stay readable — a score is never silently mutated under
a résumé it was not computed against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blockers import find_blockers
from app.job_filters import apply_job_filters
from app.matching import MatchWeights, evaluate, get_weights
from app.prefs import Prefs, PrefsError, get_prefs
from app.prefs_gate import evaluate as evaluate_prefs
from app.models import JobMatch, JobPosting, utcnow
from app.profile import Profile, ProfileError, get_profile
from app.shortlist import MIN_VALIDITY, reasons_excluded

log = logging.getLogger(__name__)


@dataclass
class MatchRunResult:
    """Outcome of one scoring pass."""

    profile_version: str = ""
    prefs_version: str = ""
    considered: int = 0
    # Eligible rows inside the scope sent from Jobs (== considered when
    # nothing was sent).
    in_transfer: int = 0
    # Rows that cleared the preference gate — the size of the
    # Matches list. Always <= `considered`.
    matching_prefs: int = 0
    scored: int = 0
    updated: int = 0
    skipped: int = 0
    # The counts below cover ONLY the rows in scope (`in_transfer`). They were
    # once counted over every eligible row, so a Run over 46 sent jobs reported
    # "34 blocked by the JD" — the whole board's figure; 6 of the 46 were.
    # Rows that pass every shortlist rule, before the per-pass read cap.
    shortlisted: int = 0
    blocked: int = 0
    low_confidence: int = 0
    # Verified below `shortlist.MIN_VALIDITY` (70).
    low_validity: int = 0
    # Rows whose stored LLM verdict was dropped because the JD moved under it.
    llm_invalidated: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    bands: dict[str, int] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        if not self.started_at or not self.finished_at:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


def _band(score: int) -> str:
    if score >= 80:
        return "excellent"
    if score >= 65:
        return "strong"
    if score >= 50:
        return "moderate"
    if score >= 35:
        return "weak"
    return "poor"


def _stamp_blockers(row: JobMatch, job: JobPosting) -> None:
    found = find_blockers(job.description_text)
    row.blockers = found
    row.blocked = bool(found)


def _count_in_scope(
    result: MatchRunResult,
    row: JobMatch,
    job: JobPosting,
    weights: MatchWeights,
    in_transfer: bool,
) -> None:
    if not in_transfer:
        return
    result.bands[_band(row.score)] = result.bands.get(_band(row.score), 0) + 1
    if not row.confident:
        result.low_confidence += 1
    if row.blocked:
        result.blocked += 1
    if job.validity_score is not None and job.validity_score < MIN_VALIDITY:
        result.low_validity += 1
    if not reasons_excluded(row, job, weights.llm_threshold):
        result.shortlisted += 1


async def run_matching(
    session: AsyncSession,
    *,
    force: bool = False,
    profile: Profile | None = None,
    weights: MatchWeights | None = None,
    prefs: Prefs | None = None,
) -> MatchRunResult:
    """Score every open, eligible posting against the current profile."""
    result = MatchRunResult(started_at=utcnow())

    try:
        profile = profile or get_profile()
    except ProfileError as exc:
        result.error = str(exc)
        result.finished_at = utcnow()
        log.error("matching.no_profile", extra={"error": str(exc)})
        return result

    weights = weights or get_weights()
    version = profile.version
    result.profile_version = version

    # Preferences never fail the run. A malformed `prefs.yaml` degrades to
    # "no preference expressed" with the error recorded, because an
    # unparseable preferences file must not cost you the scoring pass too.
    try:
        prefs = prefs or get_prefs()
    except PrefsError as exc:
        prefs = Prefs()
        result.error = f"preferences ignored: {exc}"
        log.warning("matching.prefs_unreadable", extra={"error": str(exc)})
    result.prefs_version = prefs.version

    if not weights.enabled:
        result.error = "matching disabled in config"
        result.finished_at = utcnow()
        return result

    jobs = (
        (
            await session.execute(
                select(JobPosting).where(
                    JobPosting.status == "open",
                    JobPosting.eligibility_pass.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    result.considered = len(jobs)

    # The scope sent from Jobs, resolved with the SAME filter builder the Jobs
    # list uses, so "740 jobs sent" and the rows gated here cannot disagree.
    transfer_ids: set[int] | None = None
    if prefs.transfer is not None:
        transfer_ids = set(
            (
                await session.execute(
                    apply_job_filters(select(JobPosting.id), prefs.transfer.filters)
                )
            )
            .scalars()
            .all()
        )

    existing = {
        row.job_id: row
        for row in (
            await session.execute(
                select(JobMatch).where(JobMatch.profile_version == version)
            )
        )
        .scalars()
        .all()
    }

    now = utcnow()
    for job in jobs:
        row = existing.get(job.id)

        # The preference gate runs on EVERY row, including rows whose score is
        # cached below. It has to: preferences are the one input that changes
        # between passes without touching a score, so skipping the gate on a
        # cache hit would leave the list filtered by whichever preferences
        # happened to be in force the last time a JD moved. It is pure CPU on
        # a row already loaded, so running it unconditionally costs nothing.
        in_transfer = transfer_ids is None or job.id in transfer_ids
        if in_transfer:
            result.in_transfer += 1
        pref_verdict = evaluate_prefs(job, prefs, in_transfer=in_transfer, now=now)
        if pref_verdict.passed:
            result.matching_prefs += 1

        # An existing row for this profile_version is already correct unless
        # the posting itself changed. `content_hash` is ingest's own
        # change-detector, so reuse it rather than re-deriving one here.
        if row is not None and not force and row.llm_content_hash == job.content_hash:
            result.skipped += 1
            # Re-stamp the gate even on a skip — see above. Guarded so an
            # unchanged verdict does not dirty the row and force a rewrite of
            # all 929 on every pass.
            if (
                row.prefs_version != prefs.version
                or row.prefs_pass != pref_verdict.passed
                or list(row.prefs_reasons or []) != pref_verdict.reasons
            ):
                row.prefs_version = prefs.version
                row.prefs_pass = pref_verdict.passed
                row.prefs_reasons = pref_verdict.reasons
            # Blockers depend on the JD alone, so a cached row keeps its
            # answer; only rows from before migration 0007 need one.
            if row.blockers is None:
                _stamp_blockers(row, job)
            _count_in_scope(result, row, job, weights, in_transfer)
            continue

        verdict = evaluate(
            title=job.title,
            description_text=job.description_text,
            posted_at=job.posted_at,
            now=now,
            profile=profile,
            weights=weights,
        )

        if row is None:
            row = JobMatch(job_id=job.id, profile_version=version)
            session.add(row)
            result.scored += 1
        else:
            result.updated += 1
            # Reaching here means the JD changed (or `force`), so the stored
            # LLM verdict was formed against text that no longer exists. It
            # must go before the stamp below moves `llm_content_hash` onto the
            # new hash — otherwise the deep-read pass sees a matching hash,
            # counts it as a cache hit, and the dashboard shows a verdict about
            # a superseded posting as if it were current.
            if row.llm_used or row.llm_verdict is not None:
                row.llm_used = False
                row.llm_verdict = None
                result.llm_invalidated += 1

        row.score = verdict.score
        row.subscores = verdict.subscores
        row.match_reasons = verdict.reasons
        row.confident = verdict.confident
        row.matched_skills = verdict.matched_skills
        row.missing_stacks = verdict.missing_stacks
        row.years_required = verdict.years_required
        row.prefs_version = prefs.version
        row.prefs_pass = pref_verdict.passed
        row.prefs_reasons = pref_verdict.reasons
        row.scored_at = now
        # Stamp the JD text this score was computed against. The LLM pass reads
        # the same field to decide whether its cached verdict is still valid.
        row.llm_content_hash = job.content_hash
        _stamp_blockers(row, job)
        _count_in_scope(result, row, job, weights, in_transfer)

    await session.commit()
    result.finished_at = utcnow()

    log.info(
        "matching.run_done",
        extra={
            "profile_version": version,
            "prefs_version": result.prefs_version,
            "considered": result.considered,
            "matching_prefs": result.matching_prefs,
            "scored": result.scored,
            "updated": result.updated,
            "skipped": result.skipped,
            "shortlisted": result.shortlisted,
            "blocked": result.blocked,
            "duration_ms": result.duration_ms,
        },
    )
    return result
