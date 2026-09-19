"""Hirist adapter — an India-only tech job board, read through its site's API.

Endpoints (verified live 2026-09-19; found in the site's JS bundle, no auth):
    list    GET https://gladiator.hirist.tech/job/category/?categoryId=N&page=P&size=100
            GET https://gladiator.hirist.tech/job/search?query=Q&page=P&size=100
            -> {"data": [...], "page", "totalJobs", "totalPages", "hasMore", ...}
    detail  GET https://gladiator.hirist.tech/job/detail?jobcode={id}
            -> {"data": {"introText": "<p>...", ...}}
Both need the header `version: 2`, which the site's own client sends.

Two stages, as with Wellfound. The list has everything except the JD, so the
JD is fetched only for **candidates**: remote rows whose title already passes
the configured role rule. The location stage cannot pick candidates here the
way it does on Wellfound, because every Hirist job is in India.

Live quirks encoded below:
  * **Pacing is 10s per request.** www.hirist.tech's robots.txt sets
    `Crawl-delay: 10`; the API host has no robots.txt, and the same delay is
    honoured there (`hirist_request_interval_seconds`). A sweep is therefore
    minutes long, so `companies.yaml` polls it at most twice a day.
  * **`wfh=1` and friends are ignored by the API.** Remote is read per row:
    `workFromHome == 1`, or a `"Remote"` location.
  * **Salary is in lakhs and ~93% hidden.** `minSal: 30, maxSal: 35` is
    ₹30L-35L. `hideSal: 1` rows are read as unstated, which the pay rule passes.
  * **Category order is only roughly newest first** (one page ran 09-14,
    09-17, 09-08, 09-16…). Stale rows are dropped one by one, and a query stops
    once a whole page is past `hirist_max_age_days`. A query that stops before
    `hasMore` goes false marks the fetch truncated, which skips the closure sweep.
  * **190 of 200 titles lead with the employer**: "Siemens - Golang Developer -
    Backend Services". That prefix is removed when it matches the company name.
  * **Locations are Indian place names the shared resolver partly misses**
    ("Delhi NCR", "Anywhere in India/Multiple Locations"), so a row with no
    resolvable place is read as India, the only country this board lists.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from itertools import chain
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter, RequestPacer
from app.config import get_settings
from app.eligibility import get_filters
from app.geo import resolve_eligibility, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    html_to_text,
    parse_epoch_millis,
)
from app.roles import classify
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

API = "https://gladiator.hirist.tech/job"
SITE = "https://www.hirist.tech"
HEADERS = {"version": "2", "Origin": SITE, "Referer": SITE + "/"}
PAGE_SIZE = 100
LAKH = 100_000

# Engineering categories, named by the titles sampled from each on 2026-09-19.
DEFAULT_QUERIES: list[dict[str, Any]] = [
    {"categoryId": 1},   # backend (4,122 jobs)
    {"categoryId": 16},  # software development / full stack (2,658)
    {"categoryId": 14},  # AI / ML (3,872)
    {"categoryId": 2},   # frontend (498)
]

_SPLIT_TITLE = re.compile(r"\s+-\s+")


class HiristJob(BaseModel):
    """One list row. Numeric flags arrive as ints or strings; both are read."""

    model_config = ConfigDict(extra="allow")

    id: int | str
    title: str
    jobdesignation: str | None = None
    jobDetailUrl: str | None = None
    workFromHome: int | str | None = 0
    hideSal: int | str | None = 1
    minSal: float | str | None = None
    maxSal: float | str | None = None
    min: int | str | None = None
    max: int | str | None = None
    createdTimeMs: int | str | None = None
    locations: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[dict[str, Any]] = Field(default_factory=list)
    companyData: dict[str, Any] | None = None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def location_names(row: dict[str, Any]) -> list[str]:
    return [
        str(loc["name"]).strip()
        for loc in row.get("locations") or []
        if isinstance(loc, dict) and loc.get("name")
    ]


def is_remote(row: dict[str, Any]) -> bool:
    if _int(row.get("workFromHome")) == 1:
        return True
    return any(name.lower() == "remote" for name in location_names(row))


def company_name(row: dict[str, Any]) -> str | None:
    data = row.get("companyData")
    name = data.get("companyName") if isinstance(data, dict) else None
    return str(name).strip() if name else None


def clean_title(title: str, company: str | None) -> str:
    """Drop a leading "Employer - " segment when it names the company."""
    title = title.strip()
    parts = _SPLIT_TITLE.split(title, maxsplit=1)
    if len(parts) < 2 or not company:
        return title
    lead, rest = parts[0].strip().lower(), parts[1].strip()
    first_word = company.split()[0].lower()
    if lead == company.lower() or company.lower().startswith(lead) or lead.startswith(first_word):
        return rest
    return title


def posted_at(row: dict[str, Any]) -> datetime | None:
    return parse_epoch_millis(row.get("createdTimeMs"))


class HiristAdapter(BaseAdapter):
    ats = "hirist"
    # Each category holds hundreds of rows; an empty sweep is breakage.
    empty_result_is_suspicious = True

    def __init__(self, client: Any = None) -> None:
        super().__init__(client=client)
        self._pacer = RequestPacer(get_settings().hirist_request_interval_seconds)

    async def _get(self, path: str, params: dict[str, Any], source: SourceConfig) -> Any:
        await self._pacer.wait()
        return await self.get_json(API + path, params=params, company=source, headers=HEADERS)

    async def _fetch_query(
        self, query: dict[str, Any], source: SourceConfig, cutoff: datetime
    ) -> list[dict[str, Any]]:
        settings = get_settings()
        if query.get("categoryId") is not None:
            path, base = "/category/", {"categoryId": query["categoryId"]}
        elif query.get("query"):
            path, base = "/search", {"query": query["query"]}
        else:
            raise AdapterError(
                "hirist query needs `categoryId` or `query`", ats=self.ats, company=source.company
            )

        rows: list[dict[str, Any]] = []
        for page in range(settings.hirist_max_pages):
            data = await self._get(path, {**base, "page": page, "size": PAGE_SIZE}, source)
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise AdapterError(
                    f"unexpected Hirist payload for {query} page {page}",
                    ats=self.ats,
                    company=source.company,
                )
            batch = [row for row in data["data"] if isinstance(row, dict) and row.get("id")]
            fresh = [row for row in batch if (posted_at(row) or cutoff) >= cutoff]
            rows.extend(fresh)
            if not data.get("hasMore"):
                return rows  # the whole query was read
            if batch and not fresh:
                break  # a full page past the window: older pages are older still
        self.fetch_truncated = True
        return rows

    def _is_candidate(self, row: dict[str, Any]) -> bool:
        if not is_remote(row):
            return False
        title = clean_title(str(row.get("title") or ""), company_name(row))
        return classify(title, None, get_filters().role).passed

    async def _fetch_detail(self, row: dict[str, Any], source: SourceConfig) -> None:
        """Attach the JD in place. A miss degrades to list data."""
        try:
            data = await self._get("/detail", {"jobcode": row["id"]}, source)
        except AdapterError as exc:
            log.warning("hirist.detail_failed", extra={"job_id": row.get("id"), "error": str(exc)})
            return
        detail = data.get("data") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            row["_detail"] = {
                "introText": detail.get("introText"),
                "hasExpired": detail.get("hasExpired"),
            }

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        settings = get_settings()
        queries = list(company.queries) or DEFAULT_QUERIES
        cutoff = datetime.now(UTC) - timedelta(days=settings.hirist_max_age_days)

        by_id: dict[str, dict[str, Any]] = {}
        for query in queries:
            for row in await self._fetch_query(query, company, cutoff):
                by_id.setdefault(str(row["id"]), row)

        candidates = [row for row in by_id.values() if self._is_candidate(row)]
        budget = settings.hirist_detail_budget
        if len(candidates) > budget:
            log.warning(
                "hirist.detail_budget_exhausted",
                extra={"budget": budget, "candidates": len(candidates)},
            )
        # Newest first, so a tight budget spends itself on the freshest rows.
        candidates.sort(key=lambda row: posted_at(row) or cutoff, reverse=True)
        for row in candidates[:budget]:
            await self._fetch_detail(row, company)

        return [RawJob(native_id=job_id, payload=row) for job_id, row in by_id.items()]

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = HiristJob.model_validate(raw.payload)
        detail: dict[str, Any] = raw.payload.get("_detail") or {}

        employer = company_name(raw.payload) or company.company
        places = location_names(raw.payload)
        remote = is_remote(raw.payload)

        codes, timezones, _ = resolve_eligibility(
            chain.from_iterable(split_location_text(p) for p in places if p.lower() != "remote")
        )
        if not codes:
            codes = ["IN"]  # India-only board; see the module docstring

        description_html = detail.get("introText") or None
        low_years = _int(job.min)
        skills = [str(t["name"]) for t in job.tags if isinstance(t, dict) and t.get("name")]
        parts = [
            f"Minimum {low_years}+ years experience." if low_years else None,
            f"Skills: {', '.join(skills)}." if skills else None,
            html_to_text(description_html),
        ]
        description_text = "\n\n".join(p for p in parts if p) or None

        salary_min = salary_max = None
        currency = None
        if _int(job.hideSal) == 0:
            low, high = _int(job.minSal), _int(job.maxSal)
            if high:
                salary_min = low * LAKH if low else None
                salary_max = high * LAKH
                currency = "INR"

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=clean_title(job.title, employer),
            locations=clean_locations(places),
            remote=remote,
            employment_type=None,  # the board does not say
            workplace_type="remote" if remote else "onsite",
            department=job.jobdesignation or None,
            apply_url=job.jobDetailUrl or f"{SITE}/j/{raw.native_id}",
            description_html=description_html,
            description_text=description_text,
            posted_at=posted_at(raw.payload),
            updated_at=None,
            location_eligibility=codes,
            timezone_restrictions=timezones,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            is_us_employer=None,
            raw_json=raw.payload,
        )
