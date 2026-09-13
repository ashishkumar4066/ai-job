"""The LLM shortlist — the free, hard gate between the ranked list and a paid read.

Before this, a job was sent to the LLM when it scored ≥ `llm_threshold` OR the
ranker was not confident. On the live board (2026-09-13) the second clause
carried 142 of the 198 in-preference rows, and what came back justified
cutting it: of the rows read, 96 were weak or poor and 39 strong or excellent.
The pass had spent 1.26M tokens reading 427 jobs.

Now a job is shortlisted only when EVERY rule below holds, and a pass reads at
most `max_reads` of them (default 15, hard ceiling 50), best score first:

  1. it passed the preference gate (transferred, posted ≤ 30d, work mode,
     commitment, experience, pay)
  2. score ≥ `llm_threshold` (65)
  3. the ranker was confident — a JD naming almost no technologies is read
     on request from the job drawer instead, one call at a time
  4. the verifier ran and scored it ≥ `MIN_VALIDITY` (70)
  5. no hard blocker in the JD prose (`app/blockers.py`)

`shortlist_clause` is the SQL form used to select rows, and `reasons_excluded`
the per-row explanation shown in the UI. They express the same five rules, and
`test_shortlist.py` asserts they agree.
"""

from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobMatch, JobPosting

MIN_VALIDITY = 70
DEFAULT_MAX_READS = 15
HARD_MAX_READS = 50


def clamp_reads(requested: int | None, default: int = DEFAULT_MAX_READS) -> int:
    """A pass never reads more than `HARD_MAX_READS`, whatever was asked."""
    value = default if requested is None else requested
    return max(1, min(HARD_MAX_READS, value))


def shortlist_clause(threshold: int):
    return and_(
        JobMatch.prefs_pass.is_(True),
        JobMatch.score >= threshold,
        JobMatch.confident.is_(True),
        JobPosting.validity_score.is_not(None),
        JobPosting.validity_score >= MIN_VALIDITY,
        JobMatch.blocked.is_(False),
    )


def reasons_excluded(match: JobMatch, job: JobPosting, threshold: int) -> list[str]:
    """Why a row is NOT shortlisted. Empty means it is."""
    out: list[str] = []
    if not match.prefs_pass:
        out.append("outside_preferences")
    if match.score < threshold:
        out.append(f"score_below:{match.score}<{threshold}")
    if not match.confident:
        out.append("low_confidence")
    if job.validity_score is None:
        out.append("not_verified")
    elif job.validity_score < MIN_VALIDITY:
        out.append(f"validity_below:{job.validity_score}<{MIN_VALIDITY}")
    if match.blocked:
        out.append("blocked")
    return out


def is_pending(match: JobMatch, job: JobPosting) -> bool:
    """Not yet read against this JD and profile — a read would cost a call."""
    return (
        match.llm_verdict is None
        or not match.llm_used
        or match.llm_content_hash != job.content_hash
    )


async def shortlisted_rows(
    session: AsyncSession, version: str, threshold: int
) -> list[tuple[JobMatch, JobPosting]]:
    """Every shortlisted row, in the order a capped pass reads them.

    Best score first, newest posting breaking ties, so a pass that stops at its
    cap has spent its calls on the jobs most worth applying to.
    """
    stmt = (
        select(JobMatch, JobPosting)
        .join(JobPosting, JobPosting.id == JobMatch.job_id)
        .where(
            JobMatch.profile_version == version,
            JobPosting.status == "open",
            JobPosting.eligibility_pass.is_(True),
            shortlist_clause(threshold),
        )
        .order_by(JobMatch.score.desc(), JobPosting.posted_at.desc(), JobPosting.id.desc())
    )
    return [(m, j) for m, j in (await session.execute(stmt)).all()]
