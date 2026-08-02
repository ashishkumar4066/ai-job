"""Ashby job board adapter.

Endpoint (verified live against `openai` 754 postings, `linear` 23):
    GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
    -> {"jobs": [...], "apiVersion": "1"}      # no pagination

Live quirks encoded below:
  * **An unknown slug returns HTTP 200 with `{"jobs": []}`, not a 404.**
    A typo in `companies.yaml` would therefore look like "the board emptied"
    and close every job for that company. `empty_result_is_suspicious` is True
    so ingest skips the closure sweep on a zero-result fetch.
  * `workplaceType` is CamelCase (Remote | Hybrid | OnSite) and sometimes null,
    but `isRemote` is always present — prefer the explicit boolean.
  * `secondaryLocations` is a list of `{location, address}` objects (120 of
    OpenAI's 754 jobs use it), not plain strings.
  * There is no update timestamp; only `publishedAt`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.normalize import (
    build_source_key,
    clean_locations,
    detect_remote,
    html_to_text,
    parse_iso_datetime,
)
from app.schemas import CompanyConfig, JobPosting, RawJob

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class AshbySecondaryLocation(BaseModel):
    model_config = ConfigDict(extra="allow")
    location: str | None = None


class AshbyJob(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    department: str | None = None
    team: str | None = None
    employmentType: str | None = None
    location: str | None = None
    secondaryLocations: list[AshbySecondaryLocation] = Field(default_factory=list)
    publishedAt: str | None = None
    updatedAt: str | None = None  # not currently returned; tolerated if added
    isListed: bool = True
    isRemote: bool | None = None
    workplaceType: str | None = None
    jobUrl: str | None = None
    applyUrl: str | None = None
    descriptionHtml: str | None = None
    descriptionPlain: str | None = None
    compensation: dict[str, Any] | None = None


class AshbyAdapter(BaseAdapter):
    ats = "ashby"
    # See module docstring: an unknown slug is indistinguishable from an empty
    # board, so a zero-result fetch must never trigger the closure sweep.
    empty_result_is_suspicious = True

    async def fetch(self, company: CompanyConfig) -> list[RawJob]:
        url = API_URL.format(slug=company.token_or_slug)
        data = await self.get_json(url, params={"includeCompensation": "true"}, company=company)

        if not isinstance(data, dict) or "jobs" not in data:
            raise AdapterError(
                f"unexpected Ashby payload for {company.company}: {type(data).__name__}",
                ats=self.ats,
                company=company.company,
            )

        jobs = data.get("jobs") or []
        if not isinstance(jobs, list):
            raise AdapterError(
                f"Ashby 'jobs' was {type(jobs).__name__}, expected list",
                ats=self.ats,
                company=company.company,
            )

        return [
            RawJob(native_id=str(job["id"]), payload=job)
            for job in jobs
            # `isListed: false` means the posting is hidden from the public board.
            if isinstance(job, dict) and job.get("id") and job.get("isListed", True)
        ]

    def normalize(self, raw: RawJob, company: CompanyConfig) -> JobPosting:
        job = AshbyJob.model_validate(raw.payload)

        locations = clean_locations(
            [job.location, *(sec.location for sec in job.secondaryLocations)]
        )

        description_html = job.descriptionHtml
        description_text = html_to_text(description_html) or job.descriptionPlain

        apply_url = job.jobUrl or job.applyUrl or (
            f"https://jobs.ashbyhq.com/{company.token_or_slug}/{raw.native_id}"
        )

        return JobPosting(
            source_key=build_source_key(self.ats, company.key, raw.native_id),
            ats=self.ats,
            company=company.company,
            title=job.title.strip(),
            locations=locations,
            remote=detect_remote(
                locations,
                title=job.title,
                workplace_type=job.workplaceType,
                explicit=job.isRemote,
            ),
            department=job.department or job.team,
            apply_url=apply_url,
            description_html=description_html,
            description_text=description_text,
            posted_at=parse_iso_datetime(job.publishedAt),
            updated_at=parse_iso_datetime(job.updatedAt),
            raw_json=raw.payload,
        )
