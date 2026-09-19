"""Cutshort adapter — an India tech-hiring board, read through its own JSON.

Endpoint (verified live 2026-09-19):
    GET https://cutshort.io/backend-api/webpage/jobs/{hub}?page=N
    -> {"success": true, "data": {"pageData": {"jobs": [...50],
        "liveJobCount", "title", ...}}}

This is the JSON the site's own listing pages are rendered from (the `jobListData`
query in their bundle). It is not a documented API: Cutshort's advertised
"public API" (developers.cutshort.io) is employer-side only, i.e. talent search and
job posting, and has no way to read listings. robots.txt does not disallow
`/backend-api/`; it disallows `/view/j/`, `/vj/`, `/apply/` and query forms
(`?job_listing`, `?ref`, `?free_text`), none of which are used here.

Live quirks encoded below:
  * **Only the JSON pages.** The HTML page's `?page=` barely paginates: pages
    1 and 2 shared 40 of 50 jobs and pages 3-200 were identical. The JSON
    endpoint pages cleanly, newest first, 50 per page.
  * **Past the last page it answers `success: false`**,
    `errorCode: "jobs_not_found"`, with a 200. That is the normal end of a
    sweep; any other `success: false` fails the source.
  * **Sweeps stop at `cutshort_max_age_days`.** The `remote-jobs` hub holds
    5,604 jobs going back years, while 30 days is ~4 pages. Stopping early (by
    age or by `cutshort_max_pages`) marks the fetch truncated, so closure never
    runs here; the validity pass's "missed last sweep" check covers staleness.
  * **USD rows store INR in `salaryRange.min/max`.** A "$2K - $3.5K / yr" role
    carries `min: 95602, max: 334608, currency: "USD"`. The figures the page
    shows, in the row's own currency, are `userMinVanity`/`userMaxVanity`.
    `hideSalary: true` rows show no pay at all (`jobFactSummary.salary` is
    absent), so they are read as unstated.
  * **Remote rows name no place.** `remote-jobs` rows are `"Remote only"` with
    `locations: []`. Cutshort hires into India (INR pay, Indian candidate pool),
    so a remote row with no location is read as India-eligible. A remote row
    that *does* name places is resolved from them like any other row: a remote
    role listed at "Indianapolis" is not taken on trust.
  * `expRange` is a min-max band. Only the minimum is passed to the role rule,
    because the rule reads a range's lower bound, and "12 - 18 years" means
    "at least 12".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from itertools import chain
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter, RequestPacer
from app.config import get_settings
from app.geo import resolve_eligibility, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    employment_type,
    html_to_text,
    parse_iso_datetime,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

SITE = "https://cutshort.io"
API_URL = SITE + "/backend-api/webpage/jobs/{hub}"

DEFAULT_QUERIES: list[dict[str, str]] = [{"hub": "remote-jobs"}]

END_OF_LIST = "jobs_not_found"

# `remoteType` values seen live, and what they mean for workplace_type.
_WORKPLACE = {
    "remote_only": "remote",
    "remote_okay": "remote",  # "Remote okay": remote is on offer
    "remote_not_okay": "onsite",
}


class CutshortJob(BaseModel):
    """One `pageData.jobs` entry. Unversioned; permissive."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(alias="_id")
    headline: str
    publicUrl: str
    remoteType: str | None = None
    locations: list[str] = Field(default_factory=list)
    roleTypes: list[str] = Field(default_factory=list)
    allSkills: list[str] = Field(default_factory=list)
    salaryRange: dict[str, Any] = Field(default_factory=dict)
    expRange: dict[str, Any] = Field(default_factory=dict)
    companyDetails: dict[str, Any] = Field(default_factory=dict)
    jobFactSummary: dict[str, Any] = Field(default_factory=dict)
    sanitizedComment: str | None = None


def posted_at(job: dict[str, Any]) -> datetime | None:
    summary = job.get("jobFactSummary")
    return parse_iso_datetime(summary.get("postedDate")) if isinstance(summary, dict) else None


def parse_salary(job: CutshortJob) -> tuple[float | None, float | None, str | None]:
    """Displayed pay in the row's own currency, or nothing when it is hidden."""
    pay = job.salaryRange
    if pay.get("hideSalary") or not job.jobFactSummary.get("salary"):
        return None, None, None

    def figure(*keys: str) -> float | None:
        for key in keys:
            value = pay.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
        return None

    # Not `min`/`max`: on USD rows those hold the INR conversion.
    low = figure("userMinVanity", "userMin")
    high = figure("userMaxVanity", "userMax")
    if low is None and high is None:
        return None, None, None
    return low, high, (pay.get("currency") or None)


def years_line(exp: dict[str, Any]) -> str | None:
    low = exp.get("min")
    if not isinstance(low, (int, float)) or low <= 0:
        return None
    return f"Minimum {int(low)}+ years experience."


class CutshortAdapter(BaseAdapter):
    ats = "cutshort"
    # The remote hub has thousands of rows; an empty first page is breakage.
    empty_result_is_suspicious = True

    def __init__(self, client: Any = None) -> None:
        super().__init__(client=client)
        self._pacer = RequestPacer(get_settings().cutshort_request_interval_seconds)

    async def _fetch_hub(
        self, hub: str, source: SourceConfig, cutoff: datetime
    ) -> list[dict[str, Any]]:
        settings = get_settings()
        url = API_URL.format(hub=hub)
        rows: list[dict[str, Any]] = []

        for page in range(1, settings.cutshort_max_pages + 1):
            await self._pacer.wait()
            data = await self.get_json(url, params={"page": page}, company=source)
            if not isinstance(data, dict):
                raise AdapterError(
                    f"unexpected Cutshort payload for {hub} page {page}",
                    ats=self.ats,
                    company=source.company,
                )
            if not data.get("success"):
                code = ((data.get("error") or {}).get("errorCode")) or "unknown"
                if code == END_OF_LIST and page > 1:
                    return rows  # walked off the end: a complete sweep
                raise AdapterError(
                    f"Cutshort {hub} page {page} failed: {code}",
                    ats=self.ats,
                    company=source.company,
                )

            jobs = ((data.get("data") or {}).get("pageData") or {}).get("jobs")
            if not isinstance(jobs, list):
                raise AdapterError(
                    f"no jobs list in Cutshort {hub} page {page}; the shape changed",
                    ats=self.ats,
                    company=source.company,
                )
            fresh = [job for job in jobs if isinstance(job, dict) and self._is_fresh(job, cutoff)]
            rows.extend(fresh)
            if len(fresh) < len(jobs):
                # Newest first, so the first stale row ends the window.
                self.fetch_truncated = True
                return rows

        self.fetch_truncated = True
        log.warning("cutshort.page_cap", extra={"hub": hub, "pages": settings.cutshort_max_pages})
        return rows

    @staticmethod
    def _is_fresh(job: dict[str, Any], cutoff: datetime) -> bool:
        stamp = posted_at(job)
        return stamp is None or stamp >= cutoff

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES
        cutoff = datetime.now(UTC) - timedelta(days=get_settings().cutshort_max_age_days)

        by_id: dict[str, dict[str, Any]] = {}
        for query in queries:
            hub = str(query.get("hub") or "").strip().strip("/")
            if not hub:
                raise AdapterError(
                    "cutshort query needs a `hub` slug", ats=self.ats, company=company.company
                )
            for job in await self._fetch_hub(hub, company, cutoff):
                if job.get("_id") and job.get("publicUrl"):
                    by_id.setdefault(str(job["_id"]), job)

        return [RawJob(native_id=job_id, payload=job) for job_id, job in by_id.items()]

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = CutshortJob.model_validate(raw.payload)

        employer = (job.companyDetails.get("name") or company.company).strip()
        workplace = _WORKPLACE.get(job.remoteType or "")
        remote = workplace == "remote"

        codes, timezones, unresolved = resolve_eligibility(
            chain.from_iterable(split_location_text(loc) for loc in job.locations)
        )
        if unresolved:
            log.debug(
                "cutshort.unresolved_location",
                extra={"company": employer, "items": unresolved},
            )
        if remote and not job.locations:
            codes = ["IN"]  # an India board's remote role with no place named

        body = html_to_text(job.sanitizedComment)
        description_text = "\n\n".join(p for p in (years_line(job.expRange), body) if p) or None
        salary_min, salary_max, currency = parse_salary(job)

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=job.headline.strip(),
            locations=clean_locations(job.locations or [job.jobFactSummary.get("locations")]),
            remote=remote,
            employment_type=employment_type(next(iter(job.roleTypes), None)),
            workplace_type=workplace,
            department=None,
            apply_url=job.publicUrl,
            description_html=job.sanitizedComment or None,
            description_text=description_text,
            posted_at=posted_at(raw.payload),
            updated_at=None,  # no update stamp in the payload
            location_eligibility=codes,
            timezone_restrictions=timezones,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            is_us_employer=None,  # no employer-HQ field
            raw_json=raw.payload,
        )
