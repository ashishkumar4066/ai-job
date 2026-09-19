"""Arc.dev adapter — remote jobs, each tagged with the countries it hires from.

Pages (verified live 2026-09-19):
    listing  GET https://arc.dev/remote-jobs[/{category}]?countries=IN&jobRoles=engineering…
    detail   GET https://arc.dev/remote-jobs/details/{urlString}-{randomKey}   (Arc's own)
             GET https://arc.dev/remote-jobs/j/{urlString}-{randomKey}         (external)

Next.js pages. Everything is in the `__NEXT_DATA__` script: plain HTTP, no
Firecrawl. robots.txt allows `/remote-jobs`; it sets no crawl delay for `*`
and 10s for named crawlers, so requests are spaced
(`arc_request_interval_seconds`) and a source entry polls at most every 6h.

A listing carries TWO lists, and they differ in kind:

  * `arcJobs` — Arc's own client roles. The client is confidential
    (`company.randomKey` is null), so the employer is stored as
    `Arc.dev client`. **An empty `requiredCountries` here means worldwide**:
    every sampled detail page for one said `requiredLocations: ["worldwide"]`.
  * `externalJobs` — re-posts of LinkedIn/Indeed jobs, with the employer
    named. **An empty `requiredCountries` here means unknown, not
    worldwide:** CrowdStrike's "Sr. Intelligence Software Engineer (Remote,
    DEU)" had it empty. Only a stated `IN` makes one a candidate.

Live quirks encoded below:
  * **No server-side pagination.** `?page=N` is ignored; each list is the first
    30 of its filter (the rest load client-side from a bundle the CDN will not
    serve to a plain client). Coverage therefore comes from several filter
    URLs, not pages. Measured on the default set: 35 distinct India/worldwide
    candidates, 28 of them Arc's own. A list that fills its 30 marks the fetch
    truncated, so ingest never closes a job that merely fell off a list.
  * `countries=IN` narrows the lists but is not strict: externals for
    Colombia, Israel and the US still come back. Eligibility is read per row.
  * An unknown category path (`/remote-jobs/india`) answers 200 with both lists
    empty. That fails closed, so it is logged rather than raised.
  * Pay: `min/maxAnnualSalary` or `min/maxHourlyRate`, in USD; 0 means unset.
  * `postedAt` is epoch seconds. There is no update stamp.
  * Descriptions are markdown, stored as `description_text`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter, RequestPacer
from app.adapters.wellfound import extract_next_data
from app.config import get_settings
from app.geo import WORLDWIDE, is_country_code, is_worldwide
from app.normalize import build_source_key, clean_locations, employment_type
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

SITE = "https://arc.dev"
LISTING_PATH = "/remote-jobs"

ARC_KIND = "arc"
EXTERNAL_KIND = "external"
CONFIDENTIAL_EMPLOYER = "Arc.dev client"

# Each is one listing request. `category` becomes a path segment; everything
# else is a query parameter. Measured 2026-09-19: these seven cover 34 of the
# 35 India/worldwide candidates the ten probed filters found between them.
DEFAULT_QUERIES: list[dict[str, str]] = [
    {"countries": "IN", "jobRoles": "engineering"},
    {"countries": "IN", "jobRoles": "engineering", "jobTypes": "permanent"},
    {"category": "python", "countries": "IN"},
    {"category": "ai", "countries": "IN"},
    {"category": "reactjs", "countries": "IN"},
    {"category": "nodejs", "countries": "IN"},
    {"category": "typescript", "countries": "IN"},
]

# Rows per list on a server-rendered listing page.
LISTING_CAP = 30


class ArcJob(BaseModel):
    """One `arcJobs` / `externalJobs` entry. Unversioned; everything optional."""

    model_config = ConfigDict(extra="allow")

    randomKey: str
    title: str
    urlString: str
    jobType: str | None = None
    jobRole: str | None = None
    positionType: str | None = None
    requiredCountries: list[str] = Field(default_factory=list)
    timeZone: str | None = None
    minAnnualSalary: float | None = None
    maxAnnualSalary: float | None = None
    minHourlyRate: float | None = None
    maxHourlyRate: float | None = None
    postedAt: int | float | None = None
    company: dict[str, Any] | None = None


def listing_url(query: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    params = dict(query)
    category = params.pop("category", None)
    path = f"{LISTING_PATH}/{category}" if category else LISTING_PATH
    return SITE + path, params


def detail_url(kind: str, job: dict[str, Any]) -> str:
    segment = "details" if kind == ARC_KIND else "j"
    return f"{SITE}{LISTING_PATH}/{segment}/{job['urlString']}-{job['randomKey']}"


def resolve_countries(kind: str, countries: list[str], locations: list[str]) -> list[str]:
    """Candidate eligibility for one row. See the module docstring for why the
    two kinds read an empty list differently."""
    codes: list[str] = []
    for code in countries:
        if isinstance(code, str) and is_country_code(code) and code.upper() not in codes:
            codes.append(code.upper())
    if codes:
        return codes
    if any(isinstance(loc, str) and is_worldwide(loc) for loc in locations):
        return [WORLDWIDE]
    return [WORLDWIDE] if kind == ARC_KIND else []


def _positive(value: float | None) -> float | None:
    return value if value else None


class ArcAdapter(BaseAdapter):
    ats = "arc"
    # Seven filters all coming back empty is a changed page, not an empty board.
    empty_result_is_suspicious = True

    def __init__(self, client: Any = None) -> None:
        super().__init__(client=client)
        self._pacer = RequestPacer(get_settings().arc_request_interval_seconds)

    async def _next_data(
        self, url: str, source: SourceConfig, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        await self._pacer.wait()
        text = await self.get_text(url, params=params, company=source)
        data = extract_next_data(text)
        if data is None:
            raise AdapterError(
                f"no __NEXT_DATA__ in {url}; the page shape changed",
                ats=self.ats,
                company=source.company,
            )
        return data

    async def _fetch_listing(
        self, query: dict[str, Any], source: SourceConfig
    ) -> list[dict[str, Any]]:
        url, params = listing_url(query)
        data = await self._next_data(url, source, params)
        props = (data.get("props") or {}).get("pageProps") or {}

        rows: list[dict[str, Any]] = []
        for key, kind in (("arcJobs", ARC_KIND), ("externalJobs", EXTERNAL_KIND)):
            jobs = props.get(key)
            if not isinstance(jobs, list):
                continue
            if len(jobs) >= LISTING_CAP:
                self.fetch_truncated = True
            rows.extend(
                {**job, "_kind": kind}
                for job in jobs
                if isinstance(job, dict) and job.get("randomKey") and job.get("urlString")
            )
        if not rows:
            log.warning("arc.empty_listing", extra={"url": url, "params": params})
        return rows

    @staticmethod
    def _is_candidate(job: dict[str, Any]) -> bool:
        codes = resolve_countries(job["_kind"], job.get("requiredCountries") or [], [])
        return "IN" in codes or WORLDWIDE in codes

    async def _fetch_detail(self, job: dict[str, Any], source: SourceConfig) -> None:
        """Enrich one row in place. A miss degrades to listing data."""
        url = detail_url(job["_kind"], job)
        try:
            data = await self._next_data(url, source)
        except AdapterError as exc:
            log.warning("arc.detail_failed", extra={"key": job.get("randomKey"), "error": str(exc)})
            return
        props = (data.get("props") or {}).get("pageProps") or {}
        detail = props.get("job") if isinstance(props.get("job"), dict) else {}
        job["_detail"] = {
            "description": detail.get("description"),
            "required_countries": detail.get("requiredCountries"),
            "required_locations": detail.get("requiredLocations"),
            "closed": detail.get("closed"),
            "state": detail.get("aasmState"),
            "source_url": detail.get("url"),  # the LinkedIn/Indeed original
        }

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES

        by_key: dict[str, dict[str, Any]] = {}
        for query in queries:
            for job in await self._fetch_listing(query, company):
                # Filters overlap heavily; a job is one job however it was found.
                by_key.setdefault(str(job["randomKey"]), job)

        budget = get_settings().arc_detail_budget
        spent = 0
        for job in by_key.values():
            if not self._is_candidate(job):
                continue
            if spent >= budget:
                log.warning("arc.detail_budget_exhausted", extra={"budget": budget})
                break
            await self._fetch_detail(job, company)
            spent += 1

        raws = []
        for key, job in by_key.items():
            if (job.get("_detail") or {}).get("closed") is True:
                continue  # listed but closed on its own page: not an open job
            raws.append(RawJob(native_id=key, payload=job))
        return raws

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = ArcJob.model_validate(raw.payload)
        kind = raw.payload.get("_kind", ARC_KIND)
        detail: dict[str, Any] = raw.payload.get("_detail") or {}

        employer = ((job.company or {}).get("name") or CONFIDENTIAL_EMPLOYER).strip()
        countries = detail.get("required_countries")
        if not isinstance(countries, list):
            countries = job.requiredCountries
        locations = [loc for loc in detail.get("required_locations") or [] if isinstance(loc, str)]
        eligibility = resolve_countries(kind, countries, locations)

        if _positive(job.minAnnualSalary) or _positive(job.maxAnnualSalary):
            salary_min, salary_max = _positive(job.minAnnualSalary), _positive(job.maxAnnualSalary)
        else:
            salary_min, salary_max = _positive(job.minHourlyRate), _positive(job.maxHourlyRate)
        currency = "USD" if (salary_min or salary_max) else None

        timezone = job.timeZone if job.timeZone and job.timeZone != "no-preference" else None

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=job.title.strip(),
            locations=clean_locations(
                ["Worldwide" if eligibility == [WORLDWIDE] else ", ".join(eligibility) or "Remote"]
            ),
            remote=True,  # Arc lists remote roles only
            employment_type=employment_type(job.jobType),
            workplace_type="remote",
            department=job.jobRole or job.positionType or None,
            # Arc's own page: it credits the source and, for externals, links
            # on to the original posting.
            apply_url=detail_url(kind, raw.payload),
            description_html=None,
            description_text=detail.get("description") or None,
            posted_at=(
                datetime.fromtimestamp(job.postedAt, tz=UTC) if job.postedAt else None
            ),
            updated_at=None,
            location_eligibility=eligibility,
            timezone_restrictions=[timezone] if timezone else [],
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            # Arc's clients are confidential and externals name only a city HQ.
            is_us_employer=None,
            raw_json=raw.payload,
        )
