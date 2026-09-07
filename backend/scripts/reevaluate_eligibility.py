"""Re-apply the eligibility filter to every stored job.

Ingestion decides eligibility as it writes, so editing `config/filters.yaml`
only affects rows touched by a later run. A job that is still open and unchanged
keeps whatever verdict it was given under the old rules — which, after a rule
change, is simply wrong. This walks the whole table and re-decides every row.

    python -m scripts.reevaluate_eligibility --dry-run
    python -m scripts.reevaluate_eligibility

Touches no network and no adapter: it re-runs `evaluate()` against columns
already in the database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.eligibility import evaluate, load_filters  # noqa: E402
from app.models import JobPosting  # noqa: E402


async def reevaluate(*, dry_run: bool, verbose: bool) -> int:
    settings = get_settings()
    filters = load_filters(settings.filters_file)

    print(f"Filters: {settings.filters_file}")
    print(f"  locations   : {filters.allowed_locations}")
    print(f"  remote only : {filters.require_remote}")
    print(f"  pay floor   : INR {filters.min_annual_salary_inr / 100_000:.1f}L/yr")
    role = filters.role
    print(f"  role        : {'on' if role.enabled else 'off'}, {len(role.patterns)} families")
    print(f"  experience  : {role.min_years}-{role.max_years} yrs when stated\n")

    flips: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    changed: list[tuple[str, bool, bool]] = []

    async with session_scope() as session:
        rows = (await session.execute(select(JobPosting))).scalars().all()
        for row in rows:
            verdict = evaluate(
                location_eligibility=row.location_eligibility or [],
                remote=row.remote,
                salary_min=row.salary_min,
                salary_max=row.salary_max,
                salary_currency=row.salary_currency,
                title=row.title,
                description_text=row.description_text,
                filters=filters,
            )
            before = bool(row.eligibility_pass)
            after = verdict.passed
            for reason in verdict.reasons:
                reasons[reason.split(":", 1)[0]] += 1

            if before != after:
                flips["gained" if after else "lost"] += 1
                changed.append((f"{row.company} — {row.title}", before, after))
            if not dry_run:
                row.eligibility_pass = after
                row.eligibility_reasons = verdict.reasons

        total = len(rows)
        now_eligible = sum(1 for r in rows if r.eligibility_pass) if not dry_run else None

        if dry_run:
            session.expunge_all()

    print(f"{total} rows examined")
    print(f"  newly eligible : {flips['gained']}")
    print(f"  newly blocked  : {flips['lost']}")
    if now_eligible is not None:
        print(f"  eligible now   : {now_eligible}")
    print("\nreason breakdown:")
    for reason, count in reasons.most_common():
        print(f"  {count:6d}  {reason}")

    if verbose and changed:
        print("\nchanged rows:")
        for title, before, after in changed[:40]:
            print(f"  {'+' if after else '-'} {title[:78]}")
        if len(changed) > 40:
            print(f"  … and {len(changed) - 40} more")

    print("\nDRY RUN — nothing written." if dry_run else "\nWritten.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--verbose", action="store_true", help="list the rows that changed")
    args = parser.parse_args()
    return asyncio.run(reevaluate(dry_run=args.dry_run, verbose=args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
