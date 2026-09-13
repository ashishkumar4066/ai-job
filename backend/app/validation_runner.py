"""The validity pass — runs `validation.evaluate_all` and persists it.

Sits to `validation.py` as `match_runner.py` sits to `matching.py`: that module
knows how to judge, this one knows what to load, how to batch it and what to
write back.

Scope is the WHOLE board, not the eligible slice
------------------------------------------------
Unlike matching, which scores only `open AND eligibility_pass`, validity runs
over every stored row including closed ones. Two reasons:

  * The Jobs tile shows the whole board, and a validity badge that only
    appears on eligible rows would be a badge you cannot trust the absence of.
  * A closed row's validity is the audit trail for why it closed. Discarding
    it would destroy the only record that distinguishes "the board removed a
    filled role" from "the board removed a ghost".

It is free, so scope costs nothing: 6,317 rows score in about two seconds of
pure CPU, no network and no tokens.

Board freshness
---------------
The ghost check needs the bounds of the last *successful* run that covered
each board, which live in `ingest_runs.sources`. A run killed mid-sweep leaves
`finished_at` NULL and correctly contributes nothing — the same rule the
dashboard's refresh gate uses. The bound compared against a row is that run's
`started_at`; `board_last_success` explains why the finish is the wrong end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IngestRun, JobPosting, utcnow
from app.validation import (
    ValidityConfig,
    evaluate_all,
    get_validity_config,
    reasons_summary,
)

log = logging.getLogger(__name__)

# Rows per flush. Validity is cheap, but 6,317 dirty ORM objects in one unit
# of work is a large transaction for SQLite to hold while the dashboard reads.
_BATCH = 1000


@dataclass
class ValidityRunResult:
    """Outcome of one validity pass."""

    considered: int = 0
    scored: int = 0
    unchanged: int = 0
    suspect: int = 0
    llm_applied: int = 0
    duplicates: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    bands: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        if not self.started_at or not self.finished_at:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


def _band(score: int) -> str:
    if score >= 85:
        return "solid"
    if score >= 70:
        return "ok"
    if score >= 50:
        return "questionable"
    return "suspect"


async def board_last_success(session: AsyncSession) -> dict[str, datetime]:
    """Per-board `started_at` of the most recent completed run that fetched it OK.

    Reads `ingest_runs.sources`, the per-source audit blob each run writes.
    Only finished runs count, and within a run only sources that reported
    `ok` — a board that errored was not observed, so its rows must not be
    judged absent from a fetch that never happened.

    Returns `started_at`, not `finished_at`, and that is the whole subtlety of
    this check. `last_seen_at` is stamped per row as its board is swept, while
    `finished_at` lands once the *last* board finishes. A full sweep takes
    minutes — measured at 6.5 on the live board, with Wellfound in it — so
    comparing a row against `finished_at` flags every row fetched before the
    end of its own run. The first cut of this did exactly that and reported
    2,123 perfectly live rows as ghosts, every one of them from the boards
    that are actually being swept.

    A slack window was the obvious patch and the wrong one: the right number
    is however long the slowest sweep happens to take, which is unknowable in
    advance. `started_at` needs no slack — any row seen at or after the run
    began was in that run.
    """
    stmt = (
        select(IngestRun.started_at, IngestRun.sources)
        .where(IngestRun.finished_at.is_not(None))
        .order_by(IngestRun.started_at.asc())
    )
    out: dict[str, datetime] = {}
    for started_at, sources in (await session.execute(stmt)).all():
        if not started_at or not sources:
            continue
        for entry in sources:
            if not isinstance(entry, dict):
                continue
            source_id = entry.get("source_id")
            if not source_id or not entry.get("ok", True):
                continue
            # A source that fetched nothing and had its closure sweep skipped
            # was not really observed either — the guard fired precisely
            # because the result was not trustworthy.
            if entry.get("skipped_closure_sweep"):
                continue
            out[source_id] = started_at
    return out


async def run_validation(
    session: AsyncSession,
    *,
    force: bool = False,
    config: ValidityConfig | None = None,
) -> ValidityRunResult:
    """Score every stored posting's validity and persist score + reasons.

    Idempotent: a row whose score and reasons are unchanged is left alone, so
    a re-run reports `unchanged` and writes nothing. `force` only matters for
    the log line — there is no cache to bypass, because the pass is free.
    """
    cfg = config or get_validity_config()
    result = ValidityRunResult(started_at=utcnow())

    if not cfg.enabled:
        result.finished_at = utcnow()
        result.error = "validity checks are disabled in config/filters.yaml"
        log.info("validity.disabled")
        return result

    successes = await board_last_success(session)
    rows = list((await session.execute(select(JobPosting))).scalars().all())
    result.considered = len(rows)

    verdicts = evaluate_all(rows, board_last_success=successes, config=cfg)
    result.reasons = reasons_summary(verdicts.values())
    result.duplicates = sum(
        1 for v in verdicts.values() if any(r.startswith("duplicate") for r in v.reasons)
    )
    result.llm_applied = sum(
        1 for v in verdicts.values() if any(r.startswith("llm:") for r in v.reasons)
    )

    checked_at = utcnow()
    pending = 0
    for row in rows:
        verdict = verdicts[row.id]
        band = _band(verdict.score)
        result.bands[band] = result.bands.get(band, 0) + 1
        if verdict.score < cfg.suspect_below:
            result.suspect += 1

        if (
            not force
            and row.validity_score == verdict.score
            and list(row.validity_reasons or []) == verdict.reasons
        ):
            result.unchanged += 1
            continue

        row.validity_score = verdict.score
        row.validity_reasons = verdict.reasons
        row.validity_checked_at = checked_at
        result.scored += 1
        pending += 1
        if pending >= _BATCH:
            await session.commit()
            pending = 0

    await session.commit()
    result.finished_at = utcnow()

    log.info(
        "validity.run_done",
        extra={
            "considered": result.considered,
            "scored": result.scored,
            "unchanged": result.unchanged,
            "suspect": result.suspect,
            "duplicates": result.duplicates,
            "llm_applied": result.llm_applied,
            "bands": result.bands,
            "duration_ms": result.duration_ms,
        },
    )
    return result
