"""The funnel — every number on the dashboard, explained as a chain of cuts.

The complaint this answers: "5,480 jobs, 740 with Remote on, then matching
says 430 of 910 eligible — what is going on?" Each number was correct and none
of them explained the others. A funnel shows one chain:

    Jobs:     5,481 open → 910 eligible → 868 posted ≤ 30d → 740 remote
    Matches:  740 sent from Jobs → 700 posted ≤ 30d → ... → 20 shortlisted → 12 read

Every step reports what it removed, so the difference between two numbers is
always on screen.

Jobs steps are counted live with `job_filters.apply_job_filters`, the builder
`/jobs` itself uses, so the last step equals the table's total by
construction. Matches steps are read from the verdicts the last Run stored
(`prefs_reasons`, `blocked`, validity), because some preference rules — pay
annualized through the FX table, years read out of JD prose — have no SQL
form. The response says when those verdicts predate the current preferences.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.job_filters import JobFilterSet, apply_job_filters, funnel_prefixes
from app.models import JobMatch, JobPosting
from app.prefs import Prefs
from app.shortlist import MIN_VALIDITY, clamp_reads, is_pending


def _step(key: str, label: str, count: int, previous: int | None, **extra: Any) -> dict:
    return {
        "key": key,
        "label": label,
        "count": count,
        "dropped": (previous - count) if previous is not None else 0,
        **extra,
    }


async def jobs_funnel(
    session: AsyncSession, filters: JobFilterSet, *, status: str = "open"
) -> dict[str, Any]:
    base = select(func.count()).select_from(JobPosting)
    start_label = {"open": "Open jobs", "closed": "Closed jobs"}.get(status, "All jobs")
    start = await session.scalar(apply_job_filters(base, None, status=status)) or 0
    steps = [_step("start", start_label, start, None)]
    previous = start
    for key, label, prefix in funnel_prefixes(filters):
        count = await session.scalar(apply_job_filters(base, prefix, status=status)) or 0
        steps.append(_step(key, label, count, previous))
        previous = count
    return {"steps": steps, "total": previous}


# Preference-gate reasons, grouped into funnel steps in the gate's own order.
_PREF_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("transfer", "Sent from Jobs", ("not_transferred",)),
    ("posted", "Posted recently", ("posted_too_old",)),
    ("verified", "Verifier", ("validity_below",)),
    ("preferences", "Your preferences", (
        "workplace_mismatch", "workplace_unstated",
        "employment_mismatch", "employment_unstated",
        "years_out_of_band", "years_unstated",
        "pay_below_floor", "pay_unstated",
        "company_excluded", "ats_excluded",
    )),
)

_PREF_LABELS = {
    "workplace_mismatch": "work mode",
    "workplace_unstated": "work mode not stated",
    "employment_mismatch": "commitment",
    "employment_unstated": "commitment not stated",
    "years_out_of_band": "experience asked",
    "years_unstated": "experience not stated",
    "pay_below_floor": "below pay floor",
    "pay_unstated": "pay not stated",
    "company_excluded": "company excluded",
    "ats_excluded": "platform excluded",
}


def _failing_keys(prefs: Prefs) -> set[str]:
    """Reason keys that FAIL a row under these preferences.

    Some keys are notes or failures depending on configuration —
    `workplace_unstated` is a note while silence passes — so a stored reason
    list alone cannot say which entry excluded the row.
    """
    keys = {
        "not_transferred", "posted_too_old", "validity_below",
        "workplace_mismatch", "employment_mismatch", "years_out_of_band",
        "pay_below_floor", "company_excluded", "ats_excluded",
    }
    if not prefs.include_unstated:
        keys |= {"workplace_unstated", "employment_unstated", "years_unstated"}
    if prefs.compensation.require_stated:
        keys.add("pay_unstated")
    return keys


async def matches_funnel(
    session: AsyncSession,
    *,
    profile_version: str,
    prefs: Prefs,
    threshold: int,
    default_reads: int,
) -> dict[str, Any]:
    stmt = (
        select(
            JobMatch.prefs_pass,
            JobMatch.prefs_reasons,
            JobMatch.prefs_version,
            JobMatch.score,
            JobMatch.confident,
            JobMatch.blocked,
            JobMatch.llm_used,
            JobMatch.llm_verdict,
            JobMatch.llm_content_hash,
            JobPosting.content_hash,
            JobPosting.validity_score,
        )
        .join(JobPosting, JobPosting.id == JobMatch.job_id)
        .where(
            JobMatch.profile_version == profile_version,
            JobPosting.status == "open",
            JobPosting.eligibility_pass.is_(True),
        )
    )
    # Jobs outside the scope sent from Jobs are not part of this funnel at all,
    # and the scope is applied LIVE with the same builder the Jobs list used.
    # Counting every eligible job and then subtracting made the funnel open at
    # 910 right after 534 were sent; reading the scope off stored verdicts
    # would keep showing 910 until the next Run.
    if prefs.transfer is not None:
        stmt = apply_job_filters(stmt, prefs.transfer.filters)
    rows = (await session.execute(stmt)).all()

    # `not_transferred` cannot be why an in-scope row failed; a stored one is
    # left over from an earlier scope, and `stale` already says so.
    failing = _failing_keys(prefs) - {"not_transferred"}
    group_of = {key: group for group, _label, keys in _PREF_GROUPS for key in keys}
    dropped_at: Counter[str] = Counter()
    pref_breakdown: Counter[str] = Counter()
    stale = False
    unverified = 0

    passing = []
    in_scope = len(rows)
    for row in rows:
        if row.prefs_version != prefs.version:
            stale = True
        if row.validity_score is None:
            unverified += 1
        first = next(
            (r.split(":")[0] for r in (row.prefs_reasons or []) if r.split(":")[0] in failing),
            None,
        )
        if row.prefs_pass or first is None:
            passing.append(row)
            continue
        group = group_of.get(first or "", "preferences")
        dropped_at[group] += 1
        if group == "preferences" and first:
            pref_breakdown[_PREF_LABELS.get(first, first)] += 1

    total = in_scope
    if prefs.transfer is not None:
        sent = prefs.transfer.count_at_transfer
        start_extra: dict[str, Any] = {}
        if sent != total:
            # Fewer than were sent: some closed since, or were sent with "All
            # roles" and are not eligible — Matches only ranks eligible jobs.
            start_extra["note"] = f"{sent:,} sent · {sent - total:,} closed or not eligible"
        steps = [_step("transfer", "Sent from Jobs", total, None, **start_extra)]
    else:
        steps = [_step("eligible", "Eligible jobs", total, None)]
    remaining = total
    for group, label, _keys in _PREF_GROUPS:
        if group == "transfer":
            continue
        remaining -= dropped_at[group]
        extra: dict[str, Any] = {}
        if group == "posted":
            label = f"Posted ≤ {prefs.freshness.max_age_days}d"
        elif group == "verified":
            label = (
                f"Verifier ≥ {prefs.validity.min_score}"
                if prefs.validity.min_score
                else "Verifier"
            )
            extra["note"] = (
                f"verifier has not run on {unverified} of {total} jobs"
                if unverified
                else f"verifier ran on all {total} jobs"
            )
        elif group == "preferences":
            extra["breakdown"] = dict(pref_breakdown.most_common())
        steps.append(_step(group, label, remaining, remaining + dropped_at[group], **extra))

    # --- The shortlist: first failing rule, in `shortlist.reasons_excluded` order
    short_breakdown: Counter[str] = Counter()
    shortlisted = []
    for row in passing:
        if row.score < threshold:
            short_breakdown[f"score below {threshold}"] += 1
        elif not row.confident:
            short_breakdown["low confidence"] += 1
        elif row.validity_score is None:
            short_breakdown["not verified"] += 1
        elif row.validity_score < MIN_VALIDITY:
            short_breakdown[f"validity below {MIN_VALIDITY}"] += 1
        elif row.blocked:
            short_breakdown["blocker in JD"] += 1
        else:
            shortlisted.append(row)
    steps.append(
        _step(
            "shortlist",
            "LLM shortlist",
            len(shortlisted),
            len(passing),
            breakdown=dict(short_breakdown.most_common()),
        )
    )

    read = sum(1 for row in shortlisted if not is_pending(row, _Hash(row.content_hash)))
    unread = len(shortlisted) - read
    cap = clamp_reads(None, default=default_reads)
    steps.append(
        _step(
            "llm",
            "Read by LLM",
            read,
            None,
            note=(
                f"next pass reads {min(cap, unread)} of {unread} unread"
                if unread
                else "every shortlisted job is read"
            ),
        )
    )
    return {
        "steps": steps,
        "stale": stale,
        "transfer": (
            {
                "filters": prefs.transfer.filters.model_dump(mode="json"),
                "labels": prefs.transfer.filters.describe(),
                "count_at_transfer": prefs.transfer.count_at_transfer,
                "transferred_at": prefs.transfer.transferred_at,
            }
            if prefs.transfer
            else None
        ),
        "shortlisted": len(shortlisted),
        "read": read,
        "cap": cap,
    }


class _Hash:
    """The one attribute `is_pending` reads off a posting."""

    def __init__(self, content_hash: str) -> None:
        self.content_hash = content_hash
