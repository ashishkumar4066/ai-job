"""Himalayas aggregator-board adapter — the primary discovery source.

Endpoint (verified live 2026-08-03):
    GET https://himalayas.app/jobs/api/search?q=…&page=N
    -> {"comments", "updatedAt", "offset", "limit", "totalCount", "jobs": [...]}

Chosen as primary because it is the only free source that publishes candidate
eligibility and pay as *structured* data rather than prose:

    "locationRestrictions": ["India"]        # ISO country NAMES, [] = worldwide
    "timezoneRestrictions": [5.5]            # numeric UTC offsets
    "minSalary": 162400, "maxSalary": 290000, "currency": "USD"

Live quirks encoded below:
  * **Pagination is `page` (1-based), not `offset`.** The browse feed
    (`/jobs/api`) pages with `offset`; on `/jobs/api/search` an `offset` param
    is silently ignored — verified: `?offset=20` echoed `"offset": 0` and
    returned page 1 again.
  * **`totalCount` cannot drive pagination.** `q=engineer` reported
    `totalCount: 17` while page 1 held 16 jobs and page 2 held none;
    `q=software engineer` reported 226. Paging therefore stops on an empty
    page, bounded by `aggregator_max_pages`.
  * **A short page is not the last page.** `limit` is fixed at 20 and the
    server filters expired postings *after* taking its window, so a page of 16
    can still be followed by a full one.
  * **An empty `locationRestrictions` means worldwide**, not "unknown" —
    verified by cross-checking `?worldwide=true`, which returns only jobs whose
    restriction list is empty.
  * `limit` is not client-controllable (a `limit=2` request still returned 20).
  * There is no `id` field; identity comes from the `guid` URL slug.
  * `pubDate` / `expiryDate` are epoch **seconds**.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.config import get_settings
from app.geo import format_utc_offset, resolve_eligibility
from app.normalize import (
    build_source_key,
    clean_locations,
    html_to_text,
    parse_epoch_millis,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

API_URL = "https://himalayas.app/jobs/api/search"
BROWSE_URL = "https://himalayas.app/jobs/api"

# Swept when a config entry declares no `queries`. Two passes, because no
# single query covers both halves of the eligibility rule: `worldwide=true`
# returns unrestricted roles, `country=India` returns India-eligible ones.
DEFAULT_QUERIES: list[dict[str, Any]] = [
    {"worldwide": "true"},
    {"country": "India"},
]


class HimalayasJob(BaseModel):
    """Permissive model of one posting — this API is unversioned."""

    model_config = ConfigDict(extra="allow")

    title: str
    excerpt: str | None = None
    companyName: str | None = None
    companySlug: str | None = None
    employmentType: str | None = None
    seniority: list[str] = Field(default_factory=list)
    minSalary: float | None = None
    maxSalary: float | None = None
    salaryPeriod: str | None = None
    currency: str | None = None
    locationRestrictions: list[str] = Field(default_factory=list)
    # Numeric UTC offsets; 5.5 (India) and -9.5 both occur, so not ints.
    timezoneRestrictions: list[float] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    parentCategories: list[str] = Field(default_factory=list)
    description: str | None = None
    pubDate: int | None = None
    expiryDate: int | None = None
    applicationLink: str | None = None
    guid: str | None = None


def _native_id(payload: dict[str, Any]) -> str | None:
    """Identity from the posting URL slug — there is no `id` field.

    `https://himalayas.app/companies/acme/jobs/backend-engineer` -> `backend-engineer`.
    The company half of `source_key` disambiguates the same slug across employers.
    """
    for key in ("guid", "applicationLink"):
        url = payload.get(key)
        if not isinstance(url, str) or not url.strip():
            continue
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            return slug
    title = payload.get("title")
    company = payload.get("companySlug") or payload.get("companyName")
    if title and company:
        return slugify(f"{company}-{title}")
    return None


class HimalayasAdapter(BaseAdapter):
    ats = "himalayas"
    # A sweep returning nothing is more likely a rate limit or an API change
    # than every job on the board disappearing at once.
    empty_result_is_suspicious = True

    async def _fetch_query(
        self, params: dict[str, Any], source: SourceConfig
    ) -> list[dict[str, Any]]:
        """Page one query set out until the board stops returning jobs."""
        settings = get_settings()
        collected: list[dict[str, Any]] = []

        for page in range(1, settings.aggregator_max_pages + 1):
            try:
                data = await self.get_json(
                    API_URL, params={**params, "page": page}, company=source
                )
            except AdapterError:
                # Deep pages 503 under load — observed live from page ~25. Once
                # we hold results, a failed page ends the sweep instead of
                # discarding everything the earlier pages returned; a failure on
                # page 1 is a real outage and still propagates.
                if not collected:
                    raise
                log.warning(
                    "himalayas.page_failed_keeping_partial",
                    extra={"params": params, "page": page, "collected": len(collected)},
                )
                break

            if not isinstance(data, dict) or "jobs" not in data:
                raise AdapterError(
                    f"unexpected Himalayas payload: {type(data).__name__}",
                    ats=self.ats,
                    company=source.company,
                )
            jobs = data.get("jobs") or []
            if not isinstance(jobs, list):
                raise AdapterError(
                    f"Himalayas 'jobs' was {type(jobs).__name__}, expected list",
                    ats=self.ats,
                    company=source.company,
                )
            if not jobs:
                break  # the only reliable end-of-results signal
            collected.extend(j for j in jobs if isinstance(j, dict))
        else:
            log.warning(
                "himalayas.page_cap_reached",
                extra={"params": params, "max_pages": settings.aggregator_max_pages},
            )

        return collected

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES

        raws: list[RawJob] = []
        seen: set[str] = set()
        for params in queries:
            for payload in await self._fetch_query(params, company):
                native_id = _native_id(payload)
                if not native_id:
                    continue
                # The same job legitimately answers several queries in one
                # sweep; it is still one job.
                key = f"{payload.get('companySlug') or payload.get('companyName')}/{native_id}"
                if key in seen:
                    continue
                seen.add(key)
                raws.append(RawJob(native_id=native_id, payload=payload))

        return raws

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = HimalayasJob.model_validate(raw.payload)

        employer = (job.companyName or job.companySlug or company.company).strip()
        # An empty restriction list is this board's way of saying "worldwide".
        eligibility, _, unresolved = resolve_eligibility(
            job.locationRestrictions, empty_means_worldwide=True
        )
        if unresolved:
            log.debug(
                "himalayas.unresolved_location",
                extra={"company": employer, "tokens": unresolved},
            )

        locations = clean_locations(job.locationRestrictions) or ["Worldwide"]
        apply_url = job.applicationLink or job.guid or (
            f"https://himalayas.app/companies/{job.companySlug}/jobs/{raw.native_id}"
        )

        return JobPosting(
            # The employer, not the board, is the company half of the identity.
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=job.title.strip(),
            locations=locations,
            remote=True,  # Himalayas lists remote roles exclusively.
            department=next(iter(job.parentCategories or job.categories), None),
            apply_url=apply_url,
            description_html=job.description,
            description_text=html_to_text(job.description) or job.excerpt,
            posted_at=parse_epoch_millis(job.pubDate),
            updated_at=None,  # the feed exposes no per-job update stamp
            location_eligibility=eligibility,
            timezone_restrictions=[format_utc_offset(tz) for tz in job.timezoneRestrictions],
            salary_min=job.minSalary,
            salary_max=job.maxSalary,
            salary_currency=(job.currency or None),
            # The feed carries no employer HQ, so this stays unknown rather
            # than guessed — `locationRestrictions` describes the *candidate*.
            is_us_employer=None,
            raw_json=raw.payload,
        )
