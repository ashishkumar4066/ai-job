"""Remotive aggregator-board adapter — the secondary feed.

Endpoint (verified live 2026-08-03):
    GET https://remotive.com/api/remote-jobs?category=software-dev
    -> {"00-warning", "0-legal-notice", "job-count": N, "jobs": [...]}

TERMS OF USE — these are conditions of access, not preferences. From the
`0-legal-notice` field the API returns on every call:

  * **Link back to the Remotive URL and name Remotive as the source.**
    `job["url"]` is stored as `apply_url` (never rewritten to the employer's
    own site) and `ats="remotive"` carries the attribution the dashboard and
    the Telegram alert both display.
  * **Do not submit Remotive jobs to third-party sites.** This tool is
    single-user and self-hosted; nothing here republishes.
  * **Listings are delayed 24h** — Remotive's own attribution window. Freshness
    checks must not treat that lag as staleness.
  * **At most ~4 calls/day.** Enforced by `min_fetch_interval_minutes: 360` on
    the `companies.yaml` entry, since the scheduler otherwise runs hourly.

Live quirks encoded below:
  * **The `category` param is silently ignored.** `?category=software-dev` and
    a bare request returned byte-identical payloads (33 jobs, same ids, mixed
    categories including Design and Medical). The param is still sent — it is
    the documented contract and may start working again — but the feed is
    treated as un-filtered, and nothing is dropped client-side on its account.
  * No pagination and no update timestamp; `job-count` is the whole response.
  * `candidate_required_location` is one free-text string mixing countries,
    regions and timezones: `"USA, Canada, USA timezones"`,
    `"Northern America, LATAM, Europe, APAC"`, `"Worldwide"`.
  * `salary` is free text or empty: `"$150k - $230k"`, `"$18 - $22/hr"`,
    `"OTE $25k - $35k"`, `"$31,2k- $52k"` (comma as a decimal point).
  * `publication_date` is naive ISO (`2026-07-28T14:23:05`), read as UTC.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.geo import resolve_eligibility, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    html_to_text,
    parse_iso_datetime,
    parse_salary_text,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

API_URL = "https://remotive.com/api/remote-jobs"

# Shown wherever a Remotive job is surfaced, per the terms above.
ATTRIBUTION = "via Remotive"

DEFAULT_PARAMS = {"category": "software-dev"}


class RemotiveJob(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | str
    url: str
    title: str
    company_name: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    job_type: str | None = None
    publication_date: str | None = None
    candidate_required_location: str | None = None
    salary: str | None = None
    description: str | None = None


class RemotiveAdapter(BaseAdapter):
    ats = "remotive"
    # The feed is a single un-paged response; an empty one means something
    # went wrong upstream, not that every remote job vanished.
    empty_result_is_suspicious = True

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        params = dict(DEFAULT_PARAMS)
        for query in company.queries:
            params.update(query)

        data = await self.get_json(API_URL, params=params, company=company)

        if not isinstance(data, dict) or "jobs" not in data:
            raise AdapterError(
                f"unexpected Remotive payload: {type(data).__name__}",
                ats=self.ats,
                company=company.company,
            )

        jobs = data.get("jobs") or []
        if not isinstance(jobs, list):
            raise AdapterError(
                f"Remotive 'jobs' was {type(jobs).__name__}, expected list",
                ats=self.ats,
                company=company.company,
            )

        return [
            RawJob(native_id=str(job["id"]), payload=job)
            for job in jobs
            # `url` is mandatory: without it we cannot honour the attribution
            # terms, so such a row must not be stored at all.
            if isinstance(job, dict) and job.get("id") is not None and job.get("url")
        ]

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = RemotiveJob.model_validate(raw.payload)

        employer = (job.company_name or company.company).strip()
        tokens = split_location_text(job.candidate_required_location or "")
        eligibility, timezones, unresolved = resolve_eligibility(tokens)
        if unresolved:
            log.debug(
                "remotive.unresolved_location",
                extra={"company": employer, "tokens": unresolved},
            )

        salary_min, salary_max, currency = parse_salary_text(job.salary)
        locations = clean_locations([job.candidate_required_location])

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=job.title.strip(),
            locations=locations,
            remote=True,  # Remotive lists remote roles exclusively
            department=job.category,
            # Remotive's own URL, never the employer's — required by the terms.
            apply_url=job.url,
            description_html=job.description,
            description_text=html_to_text(job.description),
            posted_at=parse_iso_datetime(job.publication_date),
            updated_at=None,  # the feed exposes no update stamp
            location_eligibility=eligibility,
            timezone_restrictions=timezones,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            # No employer-HQ field in the feed; unknown, not guessed.
            is_us_employer=None,
            raw_json=raw.payload,
        )
