"""Y Combinator jobs adapter — the public face of Work at a Startup.

Pages (verified live 2026-09-19):
    listing  GET https://www.ycombinator.com/jobs/role/{role}/{location}
    detail   GET https://www.ycombinator.com/companies/{slug}/jobs/{id}-{slug}

Neither is an API. Both are Rails + Inertia pages that embed their whole view
model as HTML-escaped JSON in a `data-page` attribute, so one plain GET yields
structured data: no Firecrawl, no browser. robots.txt allows both paths and
sets no crawl delay; requests are still spaced (`yc_request_interval_seconds`).
workatastartup.com itself answers 406 without a login and is not used.

Live quirks encoded below:
  * **An unknown role or location slug fails OPEN.** `/jobs/role/backend-engineer`
    and `/jobs/role/remote` answer 200 with the generic Software Engineer page,
    the same trap as Wellfound's unknown slugs. The page echoes what it
    actually served (`props.jobRoles` lists the valid roles, `props.location.slug`
    names the location), so a mismatch fails the source instead of silently
    storing the wrong list.
  * **No pagination.** `?page=N` is ignored; each listing is a fixed set served
    in shuffled order. Measured: `remote` 29, `india` 27, and the unfiltered
    page 40, which is a cap (`most_active_only`). A listing that fills the cap
    marks the fetch truncated, and ingest then skips the closure sweep.
  * **Locations are ISO country codes, not US states.** `"CA / Remote (CA)"` is
    Canada. The shared resolver reads a bare `CA` as California, so segments
    are parsed here: `" / "` separates segments, `"Remote (a; b)"` lists
    where a remote hire may live, anything else is an office
    (`"City, Region, CC"`, `"Region, CC"` or `"CC"`).
    One malformed form is live: `"San Francisco, CA / Remote (US)"` drops the
    country, so a two-part item is only read as `City, CC` when the second part
    is not also a US state code.
  * **A bare `"Remote"` is NOT worldwide.** Sampled rows: GoGoGrandparent asks
    for US-timezone overlap in its JD, and Authologic is a Polish remote-first
    company. It is read as unstated, like The Muse's "Flexible / Remote".
  * **`createdAt` is relative** ("18 days", "about 1 year"). The detail page's
    JSON-LD carries the exact `datePosted`, so that wins; the relative form is
    the fallback for rows whose detail page is not read.
  * **The JSON-LD is right about the date and the JD and wrong about the rest.**
    For the `"$150K - $180K CAD"`, `"CA / Remote (CA)"` role it claims
    `currency: USD` and `applicantLocationRequirements: US`. Pay and location
    come from the listing only.
  * **`visa` is kept out of the JD text.** 58 of 83 rows say "US citizen/visa
    only", which is about a US work visa. `blockers.py` would read it as a
    citizenship blocker even on a "Remote (IN)" role, so it stays in `raw_json`.
  * `applyUrl` is a YC sign-up redirect. `apply_url` stores the public job page
    instead, which links to the same application.

Detail pages are read only for rows the listing already makes India-eligible
(stage 2, as in Wellfound): they add the JD, the exact date and the company's
HQ country (`is_us_employer`), none of which can rescue a row that fails
location.

**Company mode** (`{mode: companies}`) gets past the listing caps. The public
company directory is an Algolia index (`YCCompany_production`) with an
`isHiring` facet and a `regions` facet ("India", "Fully Remote", ...). Hiring
companies are listed from it, then each one's `/companies/{slug}/jobs` page,
which is the same Inertia payload with every open role and no cap. Measured
2026-09-19: 251 hiring companies in India or Fully Remote, against 56 jobs on
the two capped listings.
  * **The Algolia key is read from `/companies` on every run** (its
    `window.AlgoliaOpts`), never stored. It is a secured, scoped key (restricted
    indices, `ycdc_public` tag filter) and can be rotated at any time.
  * One page per company, so `yc_company_budget` caps a run. Hitting the cap
    marks the fetch truncated.
  * The company page names the company's HQ country, so rows read this way get
    `is_us_employer` even when their detail page is not read.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter, RequestPacer
from app.adapters.wellfound import extract_json_ld
from app.config import get_settings
from app.geo import country_code, is_country_code, is_us_state
from app.normalize import (
    build_source_key,
    clean_locations,
    employment_type,
    html_to_text,
    parse_iso_datetime,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

SITE = "https://www.ycombinator.com"
LISTING_URL = SITE + "/jobs/role/{role}/{location}"

DEFAULT_QUERIES: list[dict[str, str]] = [
    {"role": "software-engineer", "location": "remote"},
    {"role": "software-engineer", "location": "india"},
]

# The unfiltered listing stopped at exactly this many rows.
LISTING_CAP = 40

COMPANIES_PAGE = SITE + "/companies"
COMPANY_JOBS_URL = SITE + "/companies/{slug}/jobs"
ALGOLIA_INDEX = "YCCompany_production"
ALGOLIA_URL = "https://{app}-dsn.algolia.net/1/indexes/{index}/query"
DEFAULT_REGIONS = ["India", "Fully Remote"]
_ALGOLIA_OPTS_RE = re.compile(r"window\.AlgoliaOpts\s*=\s*(\{.*?\})\s*;", re.S)

_DATA_PAGE_RE = re.compile(r'data-page="([^"]*)"')
_REMOTE_RE = re.compile(r"^remote\b\s*(?:\((?P<scope>.*)\))?\s*$", re.IGNORECASE)
_YEARS_RE = re.compile(r"^\s*(\d{1,2})\+?\s*years?\b", re.IGNORECASE)
_RELATIVE_RE = re.compile(
    r"(?P<n>\d+|an?|less than a)\s+(?P<unit>minute|hour|day|week|month|year)s?",
    re.IGNORECASE,
)
_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}
# "$100K - $175K", "₹1.5M - ₹3M INR", "₹25K - ₹75K INR / monthly".
_SALARY_RE = re.compile(
    r"^\s*(?P<sym>[^\d\s]*)\s*(?P<lo>\d+(?:\.\d+)?)\s*(?P<lo_u>[KkMm])?"
    r"(?:\s*[-–]\s*[^\d\s]*\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<hi_u>[KkMm])?)?"
    r"\s*(?P<cur>[A-Z]{3})?"
)
_SYMBOL_CURRENCY = {"$": "USD", "₹": "INR", "£": "GBP", "€": "EUR"}
_MULTIPLIER = {"k": 1_000, "m": 1_000_000}


class YcJob(BaseModel):
    """One `jobPostings` entry (listing) or `job` (detail). Unversioned."""

    model_config = ConfigDict(extra="allow")

    id: int | str
    title: str
    url: str
    location: str | None = None
    type: str | None = None
    roleSpecificType: str | None = None
    salaryRange: str | None = None
    minExperience: str | None = None
    companyName: str | None = None
    createdAt: str | None = None
    description: str | None = None
    skills: list[Any] = Field(default_factory=list)


# ----------------------------------------------------------------- Parsing
def extract_data_page(page_html: str) -> dict[str, Any] | None:
    """The Inertia view model embedded in a YC page, or None."""
    match = _DATA_PAGE_RE.search(page_html or "")
    if not match:
        return None
    try:
        parsed = json.loads(html.unescape(match.group(1)))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_algolia_opts(page_html: str) -> dict[str, str] | None:
    """`{"app": ..., "key": ...}` from the directory page, or None."""
    match = _ALGOLIA_OPTS_RE.search(page_html or "")
    if not match:
        return None
    try:
        opts = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None
    if isinstance(opts, dict) and opts.get("app") and opts.get("key"):
        return {"app": str(opts["app"]), "key": str(opts["key"])}
    return None


def item_country(item: str) -> str | None:
    """Country of one YC location item: `"City, Region, CC"`, `"Region, CC"` or `"CC"`."""
    parts = [part.strip() for part in item.split(",") if part.strip()]
    if not parts:
        return None
    last = parts[-1]
    if len(parts) == 1:
        # YC writes countries as ISO codes, so a bare "CA" is Canada.
        return last.upper() if is_country_code(last) else country_code(last)
    if len(parts) >= 3:
        return last.upper() if is_country_code(last) else country_code(last)
    # Two parts: "OR, US" or "Madrid, ES" is (place, country), but the live
    # "San Francisco, CA" drops the country, and "CA" is both.
    if is_country_code(last) and not is_us_state(last):
        return last.upper()
    city = country_code(parts[0])
    if city:
        return city
    if is_us_state(last):
        return "US"
    return country_code(last)


def parse_location(value: str | None) -> tuple[list[str], bool, list[str]]:
    """Split a YC location into (eligible countries, has remote, unresolved items).

    Office countries count as well as remote ones: an office in India is a job
    India-based candidates can take, as on every other board.
    """
    codes: list[str] = []
    unresolved: list[str] = []
    remote = False

    def add(item: str) -> None:
        code = item_country(item)
        if code is None:
            unresolved.append(item)
        elif code not in codes:
            codes.append(code)

    for segment in (value or "").split(" / "):
        segment = segment.strip()
        if not segment:
            continue
        remote_match = _REMOTE_RE.match(segment)
        if remote_match:
            remote = True
            for item in (remote_match.group("scope") or "").split(";"):
                if item.strip():
                    add(item)
        else:
            add(segment)
    return codes, remote, unresolved


def parse_salary(value: str | None) -> tuple[float | None, float | None, str | None]:
    """YC's fixed pay format. The shared parser reads `₹1.8M` as 1.0."""
    match = _SALARY_RE.match(value or "")
    if not match:
        return None, None, None

    def amount(number: str | None, unit: str | None) -> float | None:
        if number is None:
            return None
        return float(number) * _MULTIPLIER.get((unit or "").lower(), 1)

    low = amount(match.group("lo"), match.group("lo_u"))
    # A bare lower figure carries no unit when the upper one does ("$6K - $7.5K"
    # always carries both; this guards "150 - 180K").
    high = amount(match.group("hi"), match.group("hi_u") or match.group("lo_u"))
    currency = match.group("cur") or _SYMBOL_CURRENCY.get(match.group("sym") or "")
    return low, high, currency


def parse_relative_age(value: str | None, now: datetime) -> datetime | None:
    """`"about 1 month"` -> a date, floored to the day so it does not drift intra-day."""
    match = _RELATIVE_RE.search(value or "")
    if not match:
        return None
    raw = match.group("n").lower()
    count = 0 if raw.startswith("less") else 1 if raw in ("a", "an") else int(raw)
    days = count * _UNIT_DAYS[match.group("unit").lower()]
    moment = now - timedelta(days=days)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def years_line(min_experience: str | None) -> str | None:
    """`"6+ years"` -> a sentence the role rule's years pattern reads."""
    match = _YEARS_RE.match(min_experience or "")
    return f"Minimum {match.group(1)}+ years experience." if match else None


# ----------------------------------------------------------------- Adapter
class YcAdapter(BaseAdapter):
    ats = "yc"
    # A listing that comes back empty means the page shape moved, not that
    # every YC company stopped hiring.
    empty_result_is_suspicious = True

    def __init__(self, client: Any = None) -> None:
        super().__init__(client=client)
        self._pacer = RequestPacer(get_settings().yc_request_interval_seconds)

    async def _page(self, url: str, source: SourceConfig) -> tuple[str, dict[str, Any]]:
        await self._pacer.wait()
        text = await self.get_text(url, company=source)
        page = extract_data_page(text)
        if page is None:
            raise AdapterError(
                f"no data-page payload in {url}; the page shape changed",
                ats=self.ats,
                company=source.company,
            )
        return text, page

    async def _fetch_listing(
        self, query: dict[str, Any], source: SourceConfig
    ) -> list[dict[str, Any]]:
        role = str(query.get("role") or "software-engineer")
        location = str(query.get("location") or "remote")
        url = LISTING_URL.format(role=role, location=location)
        _, page = await self._page(url, source)
        props = page.get("props") or {}

        # Fail-open guards: the page names what it actually served.
        valid_roles = [r.get("slug") for r in props.get("jobRoles") or [] if isinstance(r, dict)]
        if valid_roles and role not in valid_roles:
            raise AdapterError(
                f"unknown YC role '{role}' (valid: {', '.join(valid_roles)}); the page "
                "silently served the default list instead",
                ats=self.ats,
                company=source.company,
            )
        served = (props.get("location") or {}).get("slug")
        if served != location:
            raise AdapterError(
                f"YC served location '{served}' for '{location}'; unknown slugs fail open",
                ats=self.ats,
                company=source.company,
            )

        jobs = props.get("jobPostings")
        if not isinstance(jobs, list):
            raise AdapterError(
                f"no jobPostings in {url}", ats=self.ats, company=source.company
            )
        if len(jobs) >= LISTING_CAP:
            self.fetch_truncated = True
            log.warning("yc.listing_capped", extra={"url": url, "rows": len(jobs)})
        return [{**job, "_listing_url": url} for job in jobs if isinstance(job, dict)]

    async def _hiring_companies(
        self, query: dict[str, Any], source: SourceConfig
    ) -> list[str]:
        """Slugs of hiring companies in the query's regions, via the directory index."""
        await self._pacer.wait()
        opts = extract_algolia_opts(await self.get_text(COMPANIES_PAGE, company=source))
        if opts is None:
            raise AdapterError(
                f"no window.AlgoliaOpts on {COMPANIES_PAGE}; the page shape changed",
                ats=self.ats,
                company=source.company,
            )
        regions = [str(r) for r in query.get("regions") or DEFAULT_REGIONS]
        facet_filters = [["isHiring:true"], [f"regions:{region}" for region in regions]]
        url = ALGOLIA_URL.format(app=opts["app"], index=ALGOLIA_INDEX)
        headers = {
            "X-Algolia-Application-Id": opts["app"],
            "X-Algolia-API-Key": opts["key"],
            "Referer": SITE + "/",
        }

        slugs: list[str] = []
        page, pages = 0, 1
        while page < pages:
            params = (
                f"hitsPerPage=100&page={page}"
                f"&facetFilters={json.dumps(facet_filters)}"
                '&attributesToRetrieve=["slug"]&attributesToHighlight=[]'
            )
            data = await self.get_json(
                url, method="POST", json_body={"params": params}, headers=headers, company=source
            )
            if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
                raise AdapterError(
                    f"unexpected Algolia payload for YC companies page {page}",
                    ats=self.ats,
                    company=source.company,
                )
            for hit in data["hits"]:
                slug = hit.get("slug") if isinstance(hit, dict) else None
                if slug and slug not in slugs:
                    slugs.append(str(slug))
            pages = int(data.get("nbPages") or 0)
            page += 1
        return slugs

    async def _fetch_companies(
        self, query: dict[str, Any], source: SourceConfig
    ) -> list[dict[str, Any]]:
        slugs = await self._hiring_companies(query, source)
        budget = get_settings().yc_company_budget
        if len(slugs) > budget:
            self.fetch_truncated = True
            log.warning(
                "yc.company_budget_exhausted",
                extra={"budget": budget, "companies": len(slugs)},
            )

        jobs: list[dict[str, Any]] = []
        for slug in slugs[:budget]:
            url = COMPANY_JOBS_URL.format(slug=slug)
            try:
                _, page = await self._page(url, source)
            except AdapterError as exc:
                # One company's page must not cost the other 250.
                log.warning("yc.company_failed", extra={"slug": slug, "error": str(exc)})
                continue
            props = page.get("props") or {}
            company = props.get("company") if isinstance(props.get("company"), dict) else {}
            for job in props.get("jobPostings") or []:
                if isinstance(job, dict):
                    jobs.append(
                        {**job, "_listing_url": url, "_company_country": company.get("country")}
                    )
        return jobs

    async def _fetch_detail(self, job: dict[str, Any], source: SourceConfig) -> None:
        """Enrich one listing row in place. A miss degrades to listing data."""
        url = SITE + str(job["url"])
        try:
            text, page = await self._page(url, source)
        except AdapterError as exc:
            log.warning("yc.detail_failed", extra={"job_id": job.get("id"), "error": str(exc)})
            return
        props = page.get("props") or {}
        detail = props.get("job") if isinstance(props.get("job"), dict) else {}
        company = props.get("company") if isinstance(props.get("company"), dict) else {}
        job["_detail"] = {
            "description": detail.get("description"),
            "company_country": company.get("country"),
            "company_location": company.get("location"),
            "company_website": company.get("website"),
        }
        job_ld = extract_json_ld(text)
        if job_ld:
            job["_detail"]["date_posted"] = job_ld.get("datePosted")
            job["_detail"]["description_html"] = job_ld.get("description")

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES
        fetched_at = datetime.now(UTC).isoformat()

        by_id: dict[str, dict[str, Any]] = {}
        for query in queries:
            if query.get("mode") == "companies":
                found = await self._fetch_companies(query, company)
            else:
                found = await self._fetch_listing(query, company)
            for job in found:
                if job.get("id") is None or not job.get("url"):
                    continue
                # The same job sits on both the remote and the India page.
                by_id.setdefault(str(job["id"]), {**job, "_fetched_at": fetched_at})

        budget = get_settings().yc_detail_budget
        spent = 0
        for job in by_id.values():
            codes, _, _ = parse_location(job.get("location"))
            if "IN" not in codes:
                continue  # fails location outright; the detail cannot change that
            if spent >= budget:
                log.warning("yc.detail_budget_exhausted", extra={"budget": budget})
                break
            await self._fetch_detail(job, company)
            spent += 1

        return [RawJob(native_id=job_id, payload=payload) for job_id, payload in by_id.items()]

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = YcJob.model_validate(raw.payload)
        detail: dict[str, Any] = raw.payload.get("_detail") or {}

        employer = (job.companyName or company.company).strip()
        codes, remote, unresolved = parse_location(job.location)
        if unresolved:
            log.debug("yc.unresolved_location", extra={"company": employer, "items": unresolved})

        description_html = detail.get("description_html") or None
        body = html_to_text(description_html) or detail.get("description") or None
        # The board's structured experience bar, stated where the role rule
        # reads years from. Only the bar goes in; `visa` stays out (see above).
        parts = [p for p in (years_line(job.minExperience), body) if p]
        description_text = "\n\n".join(parts) or None

        posted_at = parse_iso_datetime(detail.get("date_posted"))
        if posted_at is None:
            fetched_at = parse_iso_datetime(raw.payload.get("_fetched_at")) or datetime.now(UTC)
            posted_at = parse_relative_age(job.createdAt, fetched_at)

        country = detail.get("company_country") or raw.payload.get("_company_country")
        salary_min, salary_max, currency = parse_salary(job.salaryRange)

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=job.title.strip(),
            locations=clean_locations((job.location or "").split(" / ")),
            remote=remote,
            employment_type=employment_type(job.type),
            workplace_type="remote" if remote else "onsite",
            department=job.roleSpecificType or None,
            # The public job page, not the sign-up redirect in `applyUrl`.
            apply_url=SITE + job.url,
            description_html=description_html,
            description_text=description_text,
            posted_at=posted_at,
            updated_at=None,  # YC exposes no update stamp
            location_eligibility=codes,
            timezone_restrictions=[],
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            is_us_employer=(country == "US") if country else None,
            raw_json=raw.payload,
        )
