"""Greenhouse job board adapter.

Endpoint (verified live against `stripe`, 548 postings):
    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    -> {"jobs": [...], "meta": {"total": N}}   # no pagination

Live quirks encoded below:
  * `content` is HTML **entity-escaped** (`&lt;p&gt;`) and must be unescaped.
  * `location.name` holds either one place ("San Francisco, CA") or several
    ("SF, NYC, SEA, CHI") — splitting is a Phase 2 concern, so the raw string
    is kept and office names are appended as additional locations.
  * Field set drifts: `education` was present on only 6 of 548 jobs, so the
    payload model is permissive.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.normalize import (
    build_source_key,
    clean_locations,
    detect_remote,
    html_to_text,
    maybe_unescape_html,
    parse_iso_datetime,
)
from app.schemas import CompanyConfig, JobPosting, RawJob

API_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

# Placeholder department names Greenhouse boards use for "unset".
_EMPTY_DEPARTMENTS = {"no department", "none", ""}


class GreenhouseNamed(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str | None = None


class GreenhouseJob(BaseModel):
    """Permissive model of one Greenhouse posting — these APIs are unversioned."""

    model_config = ConfigDict(extra="allow")

    id: int | str
    title: str
    absolute_url: str | None = None
    location: GreenhouseNamed | None = None
    departments: list[GreenhouseNamed] = Field(default_factory=list)
    offices: list[GreenhouseNamed] = Field(default_factory=list)
    content: str | None = None
    updated_at: str | None = None
    first_published: str | None = None
    requisition_id: str | None = None
    company_name: str | None = None


class GreenhouseAdapter(BaseAdapter):
    ats = "greenhouse"

    async def fetch(self, company: CompanyConfig) -> list[RawJob]:
        url = API_URL.format(token=company.token_or_slug)
        data = await self.get_json(url, params={"content": "true"}, company=company)

        if not isinstance(data, dict) or "jobs" not in data:
            raise AdapterError(
                f"unexpected Greenhouse payload for {company.company}: {type(data).__name__}",
                ats=self.ats,
                company=company.company,
            )

        jobs = data.get("jobs") or []
        if not isinstance(jobs, list):
            raise AdapterError(
                f"Greenhouse 'jobs' was {type(jobs).__name__}, expected list",
                ats=self.ats,
                company=company.company,
            )

        return [
            RawJob(native_id=str(job["id"]), payload=job)
            for job in jobs
            if isinstance(job, dict) and job.get("id") is not None
        ]

    def normalize(self, raw: RawJob, company: CompanyConfig) -> JobPosting:
        job = GreenhouseJob.model_validate(raw.payload)

        locations = clean_locations(
            [job.location.name if job.location else None, *(o.name for o in job.offices)]
        )

        department = next(
            (
                d.name
                for d in job.departments
                if d.name and d.name.strip().lower() not in _EMPTY_DEPARTMENTS
            ),
            None,
        )

        description_html = maybe_unescape_html(job.content)
        apply_url = job.absolute_url or (
            f"https://boards.greenhouse.io/{company.token_or_slug}/jobs/{raw.native_id}"
        )

        return JobPosting(
            source_key=build_source_key(self.ats, company.key, raw.native_id),
            ats=self.ats,
            company=company.company,
            title=job.title.strip(),
            locations=locations,
            remote=detect_remote(locations, title=job.title),
            department=department,
            apply_url=apply_url,
            description_html=description_html,
            description_text=html_to_text(description_html),
            posted_at=parse_iso_datetime(job.first_published),
            updated_at=parse_iso_datetime(job.updated_at),
            raw_json=raw.payload,
        )
