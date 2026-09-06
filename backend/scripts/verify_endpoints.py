"""Live endpoint check — the last Phase 1 acceptance criterion.

Hits every source in `companies.yaml` once and prints the fetched count per
source, so it can be eyeballed against that source's public listing page. Also
prints how many postings clear the eligibility filter, which is the number that
actually matters day to day.

    python -m scripts.verify_endpoints
    python -m scripts.verify_endpoints --company Himalayas --sample
    python -m scripts.verify_endpoints --ats greenhouse

This is the ONLY code path that touches live endpoints; the test suite never
does.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters import get_adapter  # noqa: E402
from app.companies import load_companies  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.eligibility import evaluate, load_filters  # noqa: E402
from app.schemas import CompanyConfig  # noqa: E402


class Result:
    __slots__ = ("company", "count", "eligible", "error", "elapsed")

    def __init__(
        self,
        company: CompanyConfig,
        count: int | None,
        eligible: int,
        error: str | None,
        elapsed: float,
    ) -> None:
        self.company = company
        self.count = count
        self.eligible = eligible
        self.error = error
        self.elapsed = elapsed


async def check(
    company: CompanyConfig, client: httpx.AsyncClient, sample: bool, filters
) -> Result:
    started = time.perf_counter()
    try:
        adapter = get_adapter(company.ats, client=client)
        raws = await adapter.fetch(company)
        postings = adapter.normalize_all(raws, company)
        elapsed = time.perf_counter() - started

        eligible = 0
        for job in postings:
            verdict = evaluate(
                location_eligibility=job.location_eligibility,
                salary_currency=job.salary_currency,
                is_us_employer=job.is_us_employer,
                filters=filters,
            )
            job.eligibility_pass = verdict.passed
            job.eligibility_reasons = verdict.reasons
            eligible += int(verdict.passed)

        if sample and postings:
            # Prefer showing one that passed — that is the interesting case.
            job = next((j for j in postings if j.eligibility_pass), postings[0])
            print(f"\n    sample:  {job.title}")
            print(f"    key:     {job.source_key}")
            print(f"    where:   {job.primary_location}  remote={job.remote}")
            print(f"    dept:    {job.department}")
            print(f"    posted:  {job.posted_at}")
            print(f"    url:     {job.apply_url}")
            print(f"    eligible:{job.location_eligibility}  tz={job.timezone_restrictions[:4]}")
            print(f"    pay:     {job.salary_min}-{job.salary_max} {job.salary_currency}")
            print(f"    verdict: pass={job.eligibility_pass}  {job.eligibility_reasons}")
            body = (job.description_text or "")[:160].replace("\n", " ")
            print(f"    jd:      {body}...\n")

        if len(postings) != len(raws):
            print(f"    ! {len(raws) - len(postings)} posting(s) failed to normalize")

        return Result(company, len(postings), eligible, None, elapsed)
    except Exception as exc:
        return Result(
            company, None, 0, f"{type(exc).__name__}: {exc}", time.perf_counter() - started
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Verify live ATS endpoints.")
    parser.add_argument("--company", action="append", help="Limit to these companies")
    parser.add_argument("--ats", action="append", help="Limit to these ATS platforms")
    parser.add_argument("--sample", action="store_true", help="Print one normalized job per board")
    parser.add_argument("--all", action="store_true", help="Include disabled entries")
    args = parser.parse_args()

    settings = get_settings()
    companies = load_companies(settings.companies_file, include_disabled=args.all)

    if args.company:
        wanted = {c.lower() for c in args.company}
        companies = [c for c in companies if c.company.lower() in wanted]
    if args.ats:
        wanted_ats = {a.lower() for a in args.ats}
        companies = [c for c in companies if c.ats in wanted_ats]

    if not companies:
        print("No matching companies in companies.yaml")
        return 1

    filters = load_filters(settings.filters_file)
    print(f"Verifying {len(companies)} source(s) live…")
    print(
        f"Filter: locations={filters.allowed_locations} currency={filters.required_currency} "
        f"us_employer_fallback={filters.allow_us_employer_when_currency_unknown}\n"
    )
    print(
        f"{'SOURCE':<16} {'ATS':<11} {'SLUG':<14} {'JOBS':>6} {'ELIGIBLE':>9}  "
        f"{'TIME':>7}  LISTING PAGE"
    )
    print("-" * 108)

    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": "personal-job-aggregator/1.0", "Accept": "application/json"},
    ) as client:
        semaphore = asyncio.Semaphore(settings.ingest_concurrency)

        async def guarded(company: CompanyConfig):
            async with semaphore:
                return await check(company, client, args.sample, filters)

        results = await asyncio.gather(*(guarded(c) for c in companies))

    failures = 0
    empty = 0
    total = 0
    total_eligible = 0
    for r in results:
        company = r.company
        if r.error is not None:
            failures += 1
            print(
                f"{company.company:<16} {company.ats:<11} {company.token_or_slug:<14} "
                f"{'FAIL':>6} {'-':>9}  {r.elapsed:>6.2f}s  {r.error[:52]}"
            )
            continue
        total += r.count or 0
        total_eligible += r.eligible
        if r.count == 0:
            empty += 1
        print(
            f"{company.company:<16} {company.ats:<11} {company.token_or_slug:<14} "
            f"{r.count:>6} {r.eligible:>9}  {r.elapsed:>6.2f}s  {company.careers_url or ''}"
        )

    print("-" * 108)
    print(
        f"{len(results) - failures}/{len(results)} sources OK · {total} postings · "
        f"{total_eligible} eligible"
    )
    if empty:
        print(
            f"\n!  {empty} board(s) returned 0 jobs. On Ashby that can mean a bad slug "
            "(it answers HTTP 200 with an empty list), so double-check those."
        )
    if failures:
        print(f"\n!  {failures} board(s) failed — fix the slug in companies.yaml or disable them.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
