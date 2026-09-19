"""The Muse aggregator-board adapter — large-employer roles, India offices included.

Endpoint (verified live 2026-09-17, docs https://www.themuse.com/developers/api/v2):
    GET https://www.themuse.com/api/public/jobs?page=0&category=…&level=…&location=…
    -> {"page", "page_count", "items_per_page": 20, "took", "timed_out",
        "total", "results": [...]}

Terms: a registered key is required "for any use beyond testing"
(`THEMUSE_API_KEY`, sent as the `api_key` query param). `refs.landing_page` is
stored as `apply_url`, and `ats="themuse"` carries the attribution.

Where the live API disagrees with its own docs (both checked 2026-09-17):
  * Rate limit: docs say 500/hour unkeyed and 3,600 keyed. The registered key
    here answered `x-ratelimit-limit: 12000`. Unkeyed was 500 as documented.
  * Errors: docs list 403 as "usually a rate limit". An invalid key is also
    a 403 (`{"code": 403, "error": "Invalid API key"}`), so a 403 on page 0
    means "check the key" first. It is not retried: `get_json` treats only 429
    as a rate limit.
  * Out-of-range pages: docs say they "return 0 results". Live, any page above
    99 is a 400 (below). Paging stops on either, and on `page_count`.

Live quirks encoded below — three of them fail silently:

  * **`page` is capped at 99.** `page=100` answers 400 "Value `page` is too
    high" even when `page_count` is 5,080, so at most 2,000 rows are reachable
    per query. The default query totals 3,370.
  * **Results are NOT in date order**, with or without `descending`. Pages mix
    postings from 2024 with yesterday's. So which rows fall past the cap is
    arbitrary, and a capped fetch sets `fetch_truncated` to stop ingest from
    closing jobs it merely did not reach.
  * **An unknown `location` fails OPEN.** `location=Bengaluru, India`,
    `location=India` and `location=Nonsense, Nowhere` all returned the same
    1,223 rows, the "Flexible / Remote" set, instead of nothing. The valid
    spelling is `Bangalore, India`. Every `location` value is therefore
    checked against the rows that came back, and one no row carries is logged.
  * **`location` also always includes remote rows.** Filtering on a city
    returns that city's roles plus every "Flexible / Remote" role, so remote
    rows cannot be excluded and need not be asked for.
  * **"Flexible / Remote" does not say who may apply.** Of 200 remote
    engineering rows sampled, the remote-only ones with a silent description
    were Unum, Visa, GoodRx and "GitLab … EMEA": in practice, US or regional roles.
    So it is NOT mapped to `worldwide`, the Wellfound "Everywhere" lesson.
    Candidate eligibility comes only from the concrete city locations; a
    remote-only row gets none, and the filter says `location_unknown`.
  * `category` fails CLOSED (`category=Nonsense` returned 0). The engineering
    category is `Software Engineering`; `Software Engineer` matched 5 rows.
  * No salary, employment type or update timestamp anywhere in the payload.
    `type` is always `external` (the posting lives elsewhere), not a commitment.
  * `publication_date` is ISO-8601 with a `Z`.
"""

from __future__ import annotations

import logging
from itertools import chain
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.config import get_settings
from app.geo import resolve_eligibility, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    html_to_text,
    maybe_unescape_html,
    parse_iso_datetime,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

API_URL = "https://www.themuse.com/api/public/jobs"

# The Muse's only remote marker, spelled exactly as the feed spells it.
REMOTE_LOCATION = "Flexible / Remote"

# The API refuses any page above this, whatever `page_count` says.
MAX_PAGE = 99

DEFAULT_QUERIES: list[dict[str, Any]] = [
    {
        "category": "Software Engineering",
        "level": ["Mid Level", "Senior Level"],
        "location": [
            "Bangalore, India",
            "Hyderabad, India",
            "Chennai, India",
            "Mumbai, India",
            "Gurgaon, India",
        ],
    },
]


class MuseNamed(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str


class MuseCompany(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | str | None = None
    short_name: str | None = None
    name: str


class MuseJob(BaseModel):
    """Permissive model of one posting — this API is unversioned."""

    model_config = ConfigDict(extra="allow")

    id: int | str
    name: str
    contents: str | None = None
    publication_date: str | None = None
    locations: list[MuseNamed] = Field(default_factory=list)
    categories: list[MuseNamed] = Field(default_factory=list)
    levels: list[MuseNamed] = Field(default_factory=list)
    refs: dict[str, Any] = Field(default_factory=dict)
    company: MuseCompany


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


class TheMuseAdapter(BaseAdapter):
    ats = "themuse"
    empty_result_is_suspicious = True

    async def _fetch_query(
        self, params: dict[str, Any], source: SourceConfig
    ) -> list[dict[str, Any]]:
        """Page one query out to `page_count`, or to the API's page cap."""
        api_key = get_settings().themuse_api_key
        collected: list[dict[str, Any]] = []
        page = 0

        while True:
            request = {**params, "page": page}
            if api_key:
                request["api_key"] = api_key
            try:
                data = await self.get_json(API_URL, params=request, company=source)
            except AdapterError:
                # Keep what earlier pages returned, but never close on it.
                if not collected:
                    raise
                log.warning(
                    "themuse.page_failed_keeping_partial",
                    extra={"params": params, "page": page, "collected": len(collected)},
                )
                self.fetch_truncated = True
                break

            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise AdapterError(
                    f"unexpected The Muse payload: {type(data).__name__}",
                    ats=self.ats,
                    company=source.company,
                )
            collected.extend(r for r in results if isinstance(r, dict))

            page_count = int(data.get("page_count") or 0)
            if not results or page + 1 >= page_count:
                break
            if page >= MAX_PAGE:
                log.warning(
                    "themuse.page_cap_reached",
                    extra={"params": params, "page_count": page_count},
                )
                self.fetch_truncated = True
                break
            page += 1

        return collected

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES

        raws: list[RawJob] = []
        seen: set[str] = set()
        for params in queries:
            payloads = await self._fetch_query(params, company)

            # An unknown `location` silently returns the remote set instead.
            returned = {
                loc.get("name")
                for p in payloads
                for loc in _as_list(p.get("locations"))
                if isinstance(loc, dict)
            }
            for wanted in _as_list(params.get("location")):
                if wanted != REMOTE_LOCATION and wanted not in returned:
                    log.warning(
                        "themuse.location_not_matched",
                        extra={
                            "location": wanted,
                            "reason": "no returned row carries it; the API ignores "
                            "unknown locations rather than rejecting them",
                        },
                    )

            for payload in payloads:
                if payload.get("id") is None:
                    continue
                native_id = str(payload["id"])
                if native_id in seen:  # pages are not stable; ids are
                    continue
                seen.add(native_id)
                raws.append(RawJob(native_id=native_id, payload=payload))

        return raws

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = MuseJob.model_validate(raw.payload)

        employer = job.company.name.strip()
        names = [loc.name for loc in job.locations]
        remote = REMOTE_LOCATION in names
        places = [n for n in names if n != REMOTE_LOCATION]

        # Eligibility from concrete places only; see the module docstring for
        # why "Flexible / Remote" is not read as worldwide.
        eligibility, timezones, unresolved = resolve_eligibility(
            chain.from_iterable(split_location_text(n) for n in places)
        )
        if unresolved:
            log.debug(
                "themuse.unresolved_location",
                extra={"company": employer, "tokens": unresolved},
            )

        description_html = maybe_unescape_html(job.contents)
        apply_url = job.refs.get("landing_page") or (
            f"https://www.themuse.com/jobs/{job.company.short_name}/{raw.native_id}"
        )

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=job.name.strip(),
            locations=clean_locations(names),
            remote=remote,
            employment_type=None,  # the feed publishes none
            # "Flexible / Remote" is the board's only remote marker, so its
            # absence means the role is office-based. Hybrid cannot be told
            # apart from on-site here; both fail a remote-only preference.
            workplace_type="remote" if remote else "onsite",
            department=next((c.name for c in job.categories), None),
            apply_url=apply_url,
            description_html=description_html,
            description_text=html_to_text(description_html),
            posted_at=parse_iso_datetime(job.publication_date),
            updated_at=None,  # the feed exposes no update stamp
            location_eligibility=eligibility,
            timezone_restrictions=timezones,
            salary_min=None,
            salary_max=None,
            salary_currency=None,
            is_us_employer=None,
            raw_json=raw.payload,
        )
