"""Lever job board adapter.

Endpoint (verified live against `palantir`, 302 postings):
    GET https://api.lever.co/v0/postings/{slug}?mode=json
    -> a bare JSON **array** (no envelope, no pagination)

Live quirks encoded below:
  * `createdAt` is epoch **milliseconds**; there is no `updatedAt` at all.
  * The full JD is spread across three fields: `description` (which already
    contains `opening` + `descriptionBody` — verified), then `lists`
    (bulleted sections), then `additional`.
  * `workplaceType` is lowercase: onsite | hybrid | remote.
  * An unknown slug returns HTTP 404, so an empty array here is trustworthy.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.normalize import (
    build_source_key,
    clean_locations,
    detect_remote,
    html_to_text,
    parse_epoch_millis,
)
from app.schemas import CompanyConfig, JobPosting, RawJob

API_URL = "https://api.lever.co/v0/postings/{slug}"


class LeverCategories(BaseModel):
    model_config = ConfigDict(extra="allow")

    commitment: str | None = None
    location: str | None = None
    team: str | None = None
    department: str | None = None
    allLocations: list[str] = Field(default_factory=list)


class LeverList(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str | None = None
    content: str | None = None


class LeverPosting(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    text: str  # the job title
    categories: LeverCategories | None = None
    createdAt: int | None = None
    description: str | None = None
    descriptionPlain: str | None = None
    lists: list[LeverList] = Field(default_factory=list)
    additional: str | None = None
    additionalPlain: str | None = None
    hostedUrl: str | None = None
    applyUrl: str | None = None
    workplaceType: str | None = None
    country: str | None = None


def _compose_description_html(posting: LeverPosting) -> str | None:
    """Reassemble the JD Lever splits across `description` / `lists` / `additional`."""
    parts: list[str] = []
    if posting.description:
        parts.append(posting.description)
    for section in posting.lists:
        if not section.content:
            continue
        heading = f"<h3>{section.text}</h3>" if section.text else ""
        parts.append(f"{heading}<ul>{section.content}</ul>")
    if posting.additional:
        parts.append(posting.additional)
    return "\n".join(parts) if parts else None


class LeverAdapter(BaseAdapter):
    ats = "lever"
    # Lever 404s on an unknown slug, so a genuine empty board is unambiguous.
    empty_result_is_suspicious = False

    async def fetch(self, company: CompanyConfig) -> list[RawJob]:
        url = API_URL.format(slug=company.token_or_slug)
        data = await self.get_json(url, params={"mode": "json"}, company=company)

        if not isinstance(data, list):
            raise AdapterError(
                f"unexpected Lever payload for {company.company}: expected list, got "
                f"{type(data).__name__}",
                ats=self.ats,
                company=company.company,
            )

        return [
            RawJob(native_id=str(job["id"]), payload=job)
            for job in data
            if isinstance(job, dict) and job.get("id")
        ]

    def normalize(self, raw: RawJob, company: CompanyConfig) -> JobPosting:
        posting = LeverPosting.model_validate(raw.payload)
        categories = posting.categories or LeverCategories()

        locations = clean_locations([*categories.allLocations, categories.location])
        department = categories.department or categories.team
        description_html = _compose_description_html(posting)

        # `descriptionPlain` covers only part of the JD, so derive text from the
        # composed HTML and keep the API's version only as a fallback.
        description_text = html_to_text(description_html) or posting.descriptionPlain

        apply_url = posting.hostedUrl or posting.applyUrl or (
            f"https://jobs.lever.co/{company.token_or_slug}/{raw.native_id}"
        )

        return JobPosting(
            source_key=build_source_key(self.ats, company.key, raw.native_id),
            ats=self.ats,
            company=company.company,
            title=posting.text.strip(),
            locations=locations,
            remote=detect_remote(
                locations, title=posting.text, workplace_type=posting.workplaceType
            ),
            department=department,
            apply_url=apply_url,
            description_html=description_html,
            description_text=description_text,
            posted_at=parse_epoch_millis(posting.createdAt),
            updated_at=None,  # Lever exposes no update timestamp.
            raw_json=raw.payload,
        )
