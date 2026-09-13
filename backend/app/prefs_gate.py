"""Applying `prefs.yaml` to rows — one place, two forms.

A preference gate has to exist twice over: as SQL, so the Matches list can
page through 929 rows without loading them, and as Python, so the scoring pass
can explain per row *which* preference excluded a job. Two implementations of
one rule is exactly how filters drift apart, so both live in this file, next
to each other, and `test_prefs_gate.py` asserts they agree on the live board.

The doctrine every rule here follows
------------------------------------
NULL passes. A row whose board never published an employment type, a workplace
type, a salary or a years requirement is *not* excluded — it is admitted with
a reason recording that it was admitted on silence. Governed by
`Prefs.include_unstated`; see `prefs.py` for why the default is that way.

This is the same decision `eligibility.py` made for pay and years, and for the
same measured reason: Greenhouse publishes neither structured field, so a
strict reading of silence would drop 2,224 rows — 40% of the open board — for
facts nobody ever wrote down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, or_
from sqlalchemy.sql.elements import BooleanClauseList

from app.eligibility import annualize, get_filters
from app.job_filters import is_older_than, posted_within, posting_age_days
from app.models import JobPosting
from app.prefs import Prefs
from app.roles import years_required

log = logging.getLogger(__name__)


@dataclass
class PrefVerdict:
    """Whether one row matches the preferences, and what decided it."""

    passed: bool = True
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.passed = False
        self.reasons.append(reason)

    def note(self, reason: str) -> None:
        self.reasons.append(reason)


def sql_clause(prefs: Prefs) -> ColumnElement[bool] | BooleanClauseList:
    """The preference gate as a SQL predicate over `job_postings`.

    Every clause is written as "matches, OR the field is NULL" when
    `include_unstated` is on, which is what keeps the NULL doctrine in the
    database rather than only in the Python path.

    Pay and years are NOT expressible here and are deliberately absent:
    annualizing a salary needs the FX table and the period-band inference from
    `eligibility.py`, and a years requirement has to be read out of JD prose
    by `roles.years_required`. Both are applied in `evaluate` instead. So this
    clause is a *superset* filter — cheap narrowing for the list query, with
    the exact verdict coming from the Python pass. `list_matches` relies on
    that: it uses the stored per-row verdict for correctness and this clause
    only to keep the scan small.
    """
    clauses: list[ColumnElement[bool]] = [posted_within(prefs.freshness.max_age_days)]
    unstated_ok = prefs.include_unstated

    if prefs.work.workplace_types:
        wanted = list(prefs.work.workplace_types)
        clause = JobPosting.workplace_type.in_(wanted)
        if unstated_ok:
            clause = or_(clause, JobPosting.workplace_type.is_(None))
        clauses.append(clause)

    if prefs.work.employment_types:
        wanted = list(prefs.work.employment_types)
        clause = JobPosting.employment_type.in_(wanted)
        if unstated_ok:
            clause = or_(clause, JobPosting.employment_type.is_(None))
        clauses.append(clause)

    if prefs.validity.min_score > 0:
        # A row never scored for validity passes: an unrun validity pass must
        # not silently empty the Matches list.
        clauses.append(
            or_(
                JobPosting.validity_score >= prefs.validity.min_score,
                JobPosting.validity_score.is_(None),
            )
        )

    if prefs.scope.exclude_companies:
        clauses.append(
            func.lower(JobPosting.company).notin_(prefs.scope.exclude_companies)
        )
    if prefs.scope.exclude_ats:
        clauses.append(func.lower(JobPosting.ats).notin_(prefs.scope.exclude_ats))

    if not clauses:
        return and_(True)
    return and_(*clauses)


def evaluate(
    row: JobPosting,
    prefs: Prefs,
    *,
    in_transfer: bool = True,
    now: datetime | None = None,
) -> PrefVerdict:
    """The exact per-row verdict, with a reason for every decision.

    Reasons are emitted for passes as well as failures, following
    `eligibility.py`: knowing a job passed *because* it stated no salary,
    rather than because it cleared the floor, is the signal that tells you
    whether a preference is doing what you meant.

    `in_transfer` is decided by the caller, which runs the transferred Jobs
    filters as one SQL query (`job_filters.apply_job_filters`) rather than
    re-implementing keyword search and the remote rule here in Python.

    Reason order is the funnel's order: the broadest cut first, so the FIRST
    failing reason on a row is the step of the funnel that removed it.
    """
    v = PrefVerdict()
    unstated_ok = prefs.include_unstated

    # --- Sent from Jobs -----------------------------------------------------
    if prefs.transfer is not None and not in_transfer:
        v.fail("not_transferred")

    # --- Posting age — a hard limit, never admitted on silence --------------
    # Compared as an instant, not whole days: `posted_within` in SQL cuts at
    # exactly now - N days, and rounding the age down here admitted jobs up to
    # 30.99 days old — 619 rows in Matches against 612 in Jobs.
    age = posting_age_days(row, now)
    if is_older_than(row, prefs.freshness.max_age_days, now):
        v.fail(f"posted_too_old:{age}d")
    else:
        v.note(f"posted_ok:{age}d")

    # --- Validity -----------------------------------------------------------
    # Ahead of the fit preferences: the verifier runs before them in the
    # pipeline, so it is the earlier funnel step.
    if prefs.validity.min_score > 0:
        if row.validity_score is None:
            v.note("validity_unchecked")
        elif row.validity_score >= prefs.validity.min_score:
            v.note(f"validity_ok:{row.validity_score}")
        else:
            v.fail(f"validity_below:{row.validity_score}")

    # --- Workplace type (remote / hybrid / onsite) --------------------------
    if prefs.work.workplace_types:
        actual = (row.workplace_type or "").lower()
        if not actual:
            if unstated_ok:
                v.note("workplace_unstated")
            else:
                v.fail("workplace_unstated")
        elif actual in prefs.work.workplace_types:
            v.note(f"workplace_ok:{actual}")
        else:
            v.fail(f"workplace_mismatch:{actual}")

    # --- Employment type (full-time / contract / ...) -----------------------
    if prefs.work.employment_types:
        actual = (row.employment_type or "").lower()
        if not actual:
            if unstated_ok:
                v.note("employment_unstated")
            else:
                v.fail("employment_unstated")
        elif actual in prefs.work.employment_types:
            v.note(f"employment_ok:{actual}")
        else:
            v.fail(f"employment_mismatch:{actual}")

    # --- Pay ----------------------------------------------------------------
    # Reuses `eligibility.annualize`, so the FX table and the
    # hourly/monthly/annual inference are not reimplemented here. A second
    # copy of that logic is how "$25 - $30/hr" gets read as INR 2,640/yr.
    cfg = get_filters()
    stated = row.salary_min is not None or row.salary_max is not None
    if not stated:
        if prefs.compensation.require_stated:
            v.fail("pay_unstated")
        else:
            v.note("pay_unstated")
    else:
        best = max(x for x in (row.salary_min, row.salary_max) if x is not None)
        annual, basis = annualize(best, row.salary_currency or "USD", cfg)
        if annual is None:
            # Unconvertible currency. Treated as unstated, never as zero —
            # zero would fail the floor and drop the row for our gap, not the
            # posting's. `eligibility.annualize` returns None for exactly this.
            v.note(f"pay_unreadable:{basis}")
        elif annual >= prefs.compensation.min_annual_inr:
            v.note(f"pay_ok:{int(annual)}/{basis}")
        else:
            v.fail(f"pay_below_floor:{int(annual)}/{basis}")

    # --- Years of experience ------------------------------------------------
    # Read out of the JD prose, and it takes the MAXIMUM figure stated, not the
    # minimum — see `roles.years_required`. A JD's several year counts are a
    # headline requirement plus narrower sub-clauses, not alternatives.
    years = years_required(row.description_text or "")
    if years is None:
        if unstated_ok:
            v.note("years_unstated")
        else:
            v.fail("years_unstated")
    elif prefs.experience.min_years <= years <= prefs.experience.max_years:
        v.note(f"years_ok:{years}")
    else:
        v.fail(f"years_out_of_band:{years}")

    # --- Scope --------------------------------------------------------------
    if (row.company or "").lower() in prefs.scope.exclude_companies:
        v.fail("company_excluded")
    if (row.ats or "").lower() in prefs.scope.exclude_ats:
        v.fail("ats_excluded")

    return v
