"""Jobicy aggregator-board adapter — remote jobs with a stated candidate region.

Endpoint (verified live 2026-09-17, API v2.2.16):
    GET https://jobicy.com/api/v2/remote-jobs?count=200&industry=engineering&geo=apac
    -> {"apiVersion", "friendlyNotice", "jobCount", "lastUpdate",
        "appliedFilters", "jobs": [...]}

TERMS OF USE (docs at https://jobi.cy/apidocs and the `friendlyNotice` field):

  * **Credit Jobicy and keep its job URL.** `job["url"]` is stored as
    `apply_url`, never rewritten to the employer's site, and `ats="jobicy"`
    carries the attribution the dashboard and Telegram display.
  * **Poll "a few times per day", never more than once per hour.** Enforced by
    `min_fetch_interval_minutes: 60` on the `companies.yaml` entry, and the
    default query set is a single request for the same reason.

Live quirks encoded below:
  * **There is no `india` geo.** `geo` takes one of 55 fixed slugs
    (`?get=locations`); anything else answers 200 with `success: false` and an
    `error` instead of `jobs`. The slugs that can hold India are `anywhere`
    and `apac`.
  * **`geo=apac` is a superset of `geo=anywhere`.** Measured: engineering +
    anywhere returned 47 jobs, engineering + apac returned 72, and all 47 were
    among the 72. One request covers both halves of the location rule.
  * **"Anywhere" is read as worldwide, with no prose override.** Wellfound
    narrows its "Everywhere" claim from the JD text; that pattern was tried
    here and its one hit on 47 Anywhere rows was a false positive. Canonical's
    "compensation for US based candidates lies between…" is a *pay* sentence,
    and Canonical hires globally. Real restrictions in the prose are left to
    `app/blockers.py`, which is tuned to skip pay sentences.
  * `count` is capped at 200 server-side (`count=500` returned 200). There is
    no pagination: the feed is the newest N matching jobs.
  * `jobGeo` is one string with double-space separators:
    `"APAC,  EMEA,  LATAM,  USA"`.
  * Salary is structured but ~60% absent. `salaryPeriod` is `yearly`,
    `monthly`, `hourly` or missing; the pay rule infers period from magnitude,
    so it is kept in `raw_json` only.
  * `pubDate` is ISO-8601 with an offset. No update timestamp exists.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.geo import resolve_eligibility, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    employment_type,
    html_to_text,
    maybe_unescape_html,
    parse_iso_datetime,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

API_URL = "https://jobicy.com/api/v2/remote-jobs"

# One request: `apac` already includes every `anywhere` row (see above).
DEFAULT_QUERIES: list[dict[str, str | int]] = [
    {"count": 200, "industry": "engineering", "geo": "apac"},
]


class JobicyJob(BaseModel):
    """Permissive model of one posting — this API is unversioned."""

    model_config = ConfigDict(extra="allow")

    id: int | str
    url: str
    jobTitle: str
    companyName: str | None = None
    jobIndustry: list[str] = Field(default_factory=list)
    jobType: list[str] = Field(default_factory=list)
    jobGeo: str | None = None
    jobLevel: str | None = None
    jobExcerpt: str | None = None
    jobDescription: str | None = None
    pubDate: str | None = None
    salaryMin: float | None = None
    salaryMax: float | None = None
    salaryCurrency: str | None = None
    salaryPeriod: str | None = None


class JobicyAdapter(BaseAdapter):
    ats = "jobicy"
    # A single un-paged feed; empty means an upstream problem, not that every
    # remote engineering job vanished at once.
    empty_result_is_suspicious = True

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES

        raws: list[RawJob] = []
        seen: set[str] = set()
        for params in queries:
            data = await self.get_json(API_URL, params=dict(params), company=company)

            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                # A bad `geo`/`industry` slug answers 200 with an `error`.
                detail = data.get("error") if isinstance(data, dict) else type(data).__name__
                raise AdapterError(
                    f"unexpected Jobicy payload for {params}: {detail}",
                    ats=self.ats,
                    company=company.company,
                )

            for job in data["jobs"]:
                # `url` is mandatory: without it the attribution terms cannot be
                # honoured, so such a row is not stored at all.
                if not isinstance(job, dict) or job.get("id") is None or not job.get("url"):
                    continue
                native_id = str(job["id"])
                if native_id in seen:
                    continue
                seen.add(native_id)
                raws.append(RawJob(native_id=native_id, payload=job))

        return raws

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = JobicyJob.model_validate(raw.payload)

        employer = (job.companyName or company.company).strip()
        description_html = maybe_unescape_html(job.jobDescription)
        description_text = html_to_text(description_html) or job.jobExcerpt

        eligibility, timezones, unresolved = resolve_eligibility(
            split_location_text(job.jobGeo or "")
        )
        if unresolved:
            log.debug(
                "jobicy.unresolved_location",
                extra={"company": employer, "tokens": unresolved},
            )

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=job.jobTitle.strip(),
            locations=clean_locations([job.jobGeo]),
            remote=True,  # Jobicy lists remote roles exclusively
            employment_type=employment_type(next(iter(job.jobType), None)),
            workplace_type="remote",
            department=next(iter(job.jobIndustry), None),
            # Jobicy's own URL, never the employer's — required by the terms.
            apply_url=job.url,
            description_html=description_html,
            description_text=description_text,
            posted_at=parse_iso_datetime(job.pubDate),
            updated_at=None,  # the feed exposes no update stamp
            location_eligibility=eligibility,
            timezone_restrictions=timezones,
            salary_min=job.salaryMin,
            salary_max=job.salaryMax,
            salary_currency=job.salaryCurrency or None,
            # No employer-HQ field in the feed; unknown, not guessed.
            is_us_employer=None,
            raw_json=raw.payload,
        )
