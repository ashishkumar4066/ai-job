"""Wellfound (ex-AngelList Talent) adapter, reached through Firecrawl.

Wellfound is the one source here with **no public API**. AngelList ran a
documented REST API at `api.angel.co`; it is gone and Wellfound never replaced
it. Plain HTTP is also useless — a bare `curl` to `wellfound.com/robots.txt`
returns a "Security Check" interstitial, not the file. So this adapter is the
principle-1 fallback: Firecrawl renders the page in a real browser and we lift
the data the page ships to itself.

Verified live 2026-09-06. Two stages, because no single page has everything:

**Stage 1 — listing pages** (`/role/r/{role}`, `/role/l/{role}/{loc}`,
`/location/{loc}`), paged with `?page=N`. These are Next.js pages carrying a
`<script id="__NEXT_DATA__">` blob whose `props.pageProps.apolloState.data`
holds Wellfound's own GraphQL cache — roughly 37 `JobListingSearchResult` and
20 `StartupResult` entities per page, linked by `__ref`. One credit per page,
and no HTML parsing at all. Fields used:

    id, title, slug            -> identity + apply_url
    liveStartAt                -> posted_at (epoch SECONDS)
    locationNames[]            -> where the job sits
    acceptedRemoteLocationNames[] -> where the CANDIDATE may sit
    compensation               -> display string, "$150k – $185k • 0.1% – 0.5%"
    remote, remoteConfig.kind, jobType, yearsExperienceMin/Max
    atsSource                  -> "AtsIntegration::Ashby::Listing" when the job
                                  was piped in from an ATS we may also poll

**Stage 2 — detail pages** (`/jobs/{id}-{slug}`), only for jobs that survive
the cheap screen. These render differently — no `__NEXT_DATA__` at all — but
embed a schema.org `JobPosting` in JSON-LD:

    baseSalary   -> {currency, minValue, maxValue}  (structured; beats parsing
                    the stage-1 display string)
    datePosted   -> exact ISO timestamp
    hiringOrganization.location[].address.addressCountry -> is_us_employer
    description  -> full HTML JD

There is deliberately **no `applicantLocationRequirements`** in that JSON-LD,
so candidate eligibility never comes from stage 2's structured half.

The trap that forces two stages
------------------------------
An empty `acceptedRemoteLocationNames` renders on the site as "Hires remotely
in: **Everywhere**", and reading it as `worldwide` is wrong. Verified case —
job 4627451 (YipitData), empty list, displayed "Remote (Everywhere)", while its
own policy prose said:

    "This role may be performed fully remotely within the United States."

Mapping empty -> worldwide would push US-only roles through the India filter
and into Telegram. So an empty list is treated as *claimed* worldwide and sent
to stage 2, where `_restriction_from_prose` re-reads the rendered text. What it
finds narrows eligibility; what it cannot resolve stays worldwide but carries a
reason, because per the spec nothing is silently dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from itertools import chain
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.config import get_settings
from app.geo import WORLDWIDE, country_code, resolve_eligibility, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    html_to_text,
    parse_epoch_millis,
    parse_iso_datetime,
    parse_salary_text,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
SITE = "https://wellfound.com"

# Only paths robots.txt allows. `/search` is disallowed (it is also the
# logged-in personalised feed), as are `?role=`/`?jobId=` query forms — the
# `/role/...` path form below is a different, permitted surface.
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_LD_JSON_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)

# Prose that overrides a "worldwide" claim. Ordered: first match wins.
_RESTRICTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:with)?in\s+the\s+(United States|USA|US)\b", re.I), "United States"),
    (re.compile(r"\bUS[- ]based\b|\bU\.S\.[- ]based\b", re.I), "United States"),
    (re.compile(r"\b(?:must|required to)\s+(?:be\s+)?(?:located|reside|live)\s+in\s+the\s+([A-Z][A-Za-z .]+)", re.I), ""),
    (re.compile(r"\b(?:must|required to)\s+(?:be\s+)?(?:located|reside|live)\s+in\s+([A-Z][A-Za-z .]+)", re.I), ""),
    (re.compile(r"\bauthoriz(?:ed|ation) to work in the\s+([A-Z][A-Za-z .]+)", re.I), ""),
    (re.compile(r"\bonly\s+(?:accepting|considering)\s+.{0,40}?\bin\s+([A-Z][A-Za-z .]+)", re.I), ""),
]

# Wellfound writes the display line "Hires remotely in <X>" client-side, so it
# exists in the rendered markdown but not in the raw HTML.
_HIRES_REMOTELY_RE = re.compile(r"Hires remotely in\s*\n+\s*(.+)", re.I)


class WellfoundListing(BaseModel):
    """One `JobListingSearchResult` from the Apollo cache. Unversioned."""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    slug: str | None = None
    description: str | None = None
    jobType: str | None = None
    liveStartAt: int | None = None
    locationNames: list[str] = Field(default_factory=list)
    acceptedRemoteLocationNames: list[str] = Field(default_factory=list)
    remote: bool = False
    remoteConfig: dict[str, Any] | None = None
    compensation: str | None = None
    primaryRoleTitle: str | None = None
    atsSource: str | None = None
    yearsExperienceMin: int | None = None
    yearsExperienceMax: int | None = None


def feed_url(query: dict[str, Any]) -> str:
    """Build one allowed listing URL from a `companies.yaml` query entry.

        {role: software-engineer, remote: true}      -> /role/r/software-engineer
        {role: software-engineer, location: india}   -> /role/l/software-engineer/india
        {role: software-engineer}                    -> /role/software-engineer
        {location: india}                            -> /location/india
    """
    role = str(query.get("role") or "").strip().strip("/")
    location = str(query.get("location") or "").strip().strip("/")
    remote = bool(query.get("remote"))

    if role and location:
        return f"{SITE}/role/l/{quote(role)}/{quote(location)}"
    if role:
        return f"{SITE}/role/r/{quote(role)}" if remote else f"{SITE}/role/{quote(role)}"
    if location:
        return f"{SITE}/location/{quote(location)}"
    raise AdapterError(
        "wellfound query needs at least one of `role` or `location`", ats="wellfound"
    )


def extract_next_data(html: str) -> dict[str, Any] | None:
    """Pull `__NEXT_DATA__` out of a listing page.

    The tag carries a `crossorigin` attribute, so the match must not assume
    `type="application/json">` closes it.
    """
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_json_ld(html: str) -> dict[str, Any] | None:
    """Pull the schema.org JobPosting out of a detail page."""
    for match in _LD_JSON_RE.finditer(html or ""):
        try:
            payload = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def iter_listings(next_data: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield `(startup, listing)` pairs from the Apollo cache.

    Jobs hang off `StartupResult.highlightedJobListings` as `__ref` pointers,
    which is the only place the employer name lives — the listing entity itself
    has no company field.
    """
    apollo = (next_data.get("props") or {}).get("pageProps", {}).get("apolloState")
    if not isinstance(apollo, dict):
        return []
    data = apollo.get("data") if isinstance(apollo.get("data"), dict) else apollo

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()
    for key, startup in data.items():
        if not key.startswith("StartupResult:") or not isinstance(startup, dict):
            continue
        for ref in startup.get("highlightedJobListings") or []:
            ref_key = ref.get("__ref") if isinstance(ref, dict) else None
            if not ref_key or ref_key in seen:
                continue
            listing = data.get(ref_key)
            if isinstance(listing, dict) and listing.get("id"):
                seen.add(ref_key)
                pairs.append((startup, listing))
    return pairs


def _resolve_names(names: list[str]) -> tuple[list[str], list[str]]:
    """Resolve Wellfound location names into ISO codes.

    `acceptedRemoteLocationNames` holds compound strings — "Mumbai,
    Maharashtra", "California, United States", "Austin, Texas" — so each name
    must be split before resolution or it resolves to nothing at all. Skipping
    the split silently drops India eligibility on any job that names an Indian
    city rather than the country.
    """
    codes, _, unresolved = resolve_eligibility(
        chain.from_iterable(split_location_text(name) for name in names)
    )
    return codes, unresolved


def _restriction_from_prose(text: str) -> str | None:
    """Find a country restriction stated in free text, or None.

    Deliberately conservative: it only reports a restriction it can name, so an
    unparsed sentence leaves eligibility as-is rather than inventing a limit.
    """
    if not text:
        return None
    for pattern, fixed in _RESTRICTION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = fixed or (match.group(1) if match.groups() else "")
        name = name.strip().rstrip(".,;")
        if name and country_code(name):
            return name
    return None


def _hires_remotely_in(markdown: str) -> list[str]:
    """The rendered 'Hires remotely in' line, which is client-side only."""
    match = _HIRES_REMOTELY_RE.search(markdown or "")
    if not match:
        return []
    line = match.group(1).strip()
    # Rendered as markdown links: "[India](https://wellfound.com/remote)".
    names = re.findall(r"\[([^\]]+)\]\([^)]*\)", line) or [line]
    return [n.strip() for n in names if n.strip()]


def _is_us_employer(job_ld: dict[str, Any]) -> bool | None:
    """Derive `is_us_employer` from the hiring org's addressCountry.

    Unknown stays None — the pay rule treats unknown differently from False.
    """
    org = job_ld.get("hiringOrganization")
    if not isinstance(org, dict):
        return None
    places = org.get("location")
    if isinstance(places, dict):
        places = [places]
    if not isinstance(places, list):
        return None

    countries: list[str] = []
    for place in places:
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if isinstance(address, dict) and address.get("addressCountry"):
            countries.append(str(address["addressCountry"]))
    if not countries:
        return None
    return any(country_code(c) == "US" for c in countries)


class WellfoundAdapter(BaseAdapter):
    ats = "wellfound"
    # A zero-job sweep means Firecrawl was blocked or the page shape moved, not
    # that Wellfound emptied out.
    empty_result_is_suspicious = True

    def __init__(self, client: Any = None) -> None:
        super().__init__(client=client)
        # One adapter instance handles a whole source for one run, so the
        # limiter below spans every Firecrawl call that run makes.
        self._pace_lock = asyncio.Lock()
        self._last_scrape: float | None = None

    # -- Firecrawl transport ------------------------------------------------
    async def _pace(self) -> None:
        """Hold each Firecrawl call back to the plan's request rate.

        Firecrawl's cap is per minute and it does *not* send a `Retry-After`
        header — the wait it wants is buried in the 429 body — so the retry
        path is flying blind and expensive. Spacing calls here means the limit
        is never tripped: ~4s of deliberate waiting instead of a 30s+ backoff
        ladder, and no risk of exhausting the retry budget and failing the
        whole source, which is what left Wellfound with zero rows.
        """
        rpm = get_settings().firecrawl_requests_per_minute
        if rpm <= 0:
            return
        interval = 60.0 / rpm
        async with self._pace_lock:
            if self._last_scrape is not None:
                wait = interval - (time.monotonic() - self._last_scrape)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_scrape = time.monotonic()

    async def _scrape(self, url: str, formats: list[str], source: SourceConfig) -> dict[str, Any]:
        settings = get_settings()
        if not settings.firecrawl_api_key:
            raise AdapterError(
                "FIRECRAWL_API_KEY is not set — Wellfound has no public API and "
                "cannot be fetched without it",
                ats=self.ats,
                company=source.company,
            )

        await self._pace()

        body = await self.get_json(
            FIRECRAWL_SCRAPE_URL,
            method="POST",
            json_body={
                "url": url,
                "formats": formats,
                "onlyMainContent": False,
                "maxAge": settings.firecrawl_max_age_ms,
            },
            headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"},
            company=source,
        )
        if not isinstance(body, dict) or not body.get("success"):
            raise AdapterError(
                f"firecrawl refused {url}: {str(body)[:200]}",
                ats=self.ats,
                company=source.company,
            )
        data = body.get("data")
        if not isinstance(data, dict):
            raise AdapterError(
                f"firecrawl returned no data for {url}", ats=self.ats, company=source.company
            )
        return data

    # -- Stage 1 ------------------------------------------------------------
    async def _fetch_feed(self, query: dict[str, Any], source: SourceConfig) -> list[dict[str, Any]]:
        settings = get_settings()
        base = feed_url(query)
        collected: list[dict[str, Any]] = []

        for page in range(1, settings.wellfound_max_pages + 1):
            url = base if page == 1 else f"{base}?page={page}"
            try:
                data = await self._scrape(url, ["rawHtml"], source)
            except AdapterError:
                # Keep what earlier pages produced; a page-1 failure is a real
                # outage and still propagates.
                if not collected:
                    raise
                log.warning(
                    "wellfound.page_failed_keeping_partial",
                    extra={"url": url, "page": page, "collected": len(collected)},
                )
                break

            next_data = extract_next_data(data.get("rawHtml") or "")
            if next_data is None:
                if not collected:
                    raise AdapterError(
                        f"no __NEXT_DATA__ in {url} — page shape changed or the "
                        "request was challenged",
                        ats=self.ats,
                        company=source.company,
                    )
                log.warning("wellfound.no_next_data", extra={"url": url, "page": page})
                break

            pairs = iter_listings(next_data)
            if not pairs:
                break  # the only reliable end-of-results signal

            for startup, listing in pairs:
                collected.append(
                    {
                        **listing,
                        "_company": startup.get("name") or source.company,
                        "_company_slug": startup.get("slug"),
                        "_feed_url": url,
                    }
                )
        else:
            log.warning(
                "wellfound.page_cap_reached",
                extra={"feed": base, "max_pages": settings.wellfound_max_pages},
            )

        return collected

    # -- Stage 2 ------------------------------------------------------------
    def _needs_detail(self, listing: dict[str, Any]) -> bool:
        """Fetch a detail page only when it can change the eligibility verdict.

        Two cases qualify: an unstated candidate location (the "Everywhere"
        trap), and an India-eligible job whose pay we could not read from the
        display string. Everything else is already decided, so we do not spend
        the credit.
        """
        accepted = listing.get("acceptedRemoteLocationNames") or []
        if not accepted:
            return True

        codes, _ = _resolve_names(accepted)
        if not ({"IN", WORLDWIDE} & set(codes)):
            return False  # fails location outright; no pay lookup can save it

        _, _, currency = parse_salary_text(listing.get("compensation"))
        return currency is None

    async def _fetch_detail(self, listing: dict[str, Any], source: SourceConfig) -> None:
        """Enrich one listing in place with JSON-LD + rendered prose."""
        url = f"{SITE}/jobs/{listing['id']}-{listing.get('slug') or ''}".rstrip("-")
        try:
            # Both formats in one call: JSON-LD lives in rawHtml, while the
            # "Hires remotely in" line and the policy prose are client-rendered
            # and only ever appear in markdown.
            data = await self._scrape(url, ["rawHtml", "markdown"], source)
        except AdapterError as exc:
            # A detail miss must not lose the job — it degrades to stage-1 data.
            log.warning(
                "wellfound.detail_failed",
                extra={"job_id": listing.get("id"), "error": str(exc)},
            )
            return

        listing["_detail_url"] = url
        job_ld = extract_json_ld(data.get("rawHtml") or "")
        if job_ld:
            listing["_json_ld"] = job_ld
        markdown = data.get("markdown") or ""
        listing["_hires_remotely_in"] = _hires_remotely_in(markdown)
        # Scope the prose scan to the rendered page, which holds the remote-work
        # policy block that contradicted the structured field.
        listing["_prose_restriction"] = _restriction_from_prose(markdown)

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries)
        if not queries:
            raise AdapterError(
                "wellfound entry needs `queries` (role and/or location)",
                ats=self.ats,
                company=company.company,
            )

        settings = get_settings()
        by_id: dict[str, dict[str, Any]] = {}
        for query in queries:
            for listing in await self._fetch_feed(query, company):
                # The same job legitimately appears in several feeds; it is
                # still one job.
                by_id.setdefault(str(listing["id"]), listing)

        if settings.wellfound_verify_details:
            budget = settings.wellfound_detail_budget
            spent = 0
            for listing in by_id.values():
                if spent >= budget:
                    log.warning(
                        "wellfound.detail_budget_exhausted",
                        extra={"budget": budget, "candidates": len(by_id)},
                    )
                    break
                if self._needs_detail(listing):
                    await self._fetch_detail(listing, company)
                    spent += 1

        return [RawJob(native_id=jid, payload=payload) for jid, payload in by_id.items()]

    # -- Mapping ------------------------------------------------------------
    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        payload = raw.payload
        listing = WellfoundListing.model_validate(payload)
        job_ld = payload.get("_json_ld") if isinstance(payload.get("_json_ld"), dict) else {}

        employer = str(payload.get("_company") or company.company).strip()
        slug = listing.slug or slugify(listing.title)
        apply_url = f"{SITE}/jobs/{listing.id}-{slug}"

        eligibility, reasons = self._resolve_eligibility(listing, payload)

        # Structured JSON-LD pay beats the stage-1 display string; fall back to
        # parsing "$150k – $185k • 0.1% – 0.5%" when the detail page is absent.
        salary_min, salary_max, currency = self._resolve_salary(listing, job_ld)

        posted_at = parse_iso_datetime(job_ld.get("datePosted")) or parse_epoch_millis(
            listing.liveStartAt
        )

        description_html = job_ld.get("description") if isinstance(
            job_ld.get("description"), str
        ) else None
        description_text = html_to_text(description_html) or listing.description

        remote_kind = (listing.remoteConfig or {}).get("kind")

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), listing.id),
            ats=self.ats,
            company=employer,
            title=listing.title.strip(),
            locations=clean_locations(listing.locationNames) or ["Remote"],
            remote=bool(listing.remote or remote_kind in {"REMOTE", "ONSITE_OR_REMOTE"}),
            department=listing.primaryRoleTitle,
            apply_url=apply_url,
            description_html=description_html,
            description_text=description_text,
            posted_at=posted_at,
            updated_at=None,  # Wellfound exposes no per-job update stamp
            location_eligibility=eligibility,
            timezone_restrictions=[],  # not published in either payload
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            is_us_employer=_is_us_employer(job_ld) if job_ld else None,
            eligibility_reasons=reasons,
            raw_json=payload,
        )

    def _resolve_eligibility(
        self, listing: WellfoundListing, payload: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        """Candidate eligibility, with the "Everywhere" claim treated as a claim.

        Returns `(codes, reasons)`. Reasons are advisory notes carried into the
        posting so a later verdict is auditable; the ingest filter appends its
        own decision on top.
        """
        reasons: list[str] = []
        accepted = list(listing.acceptedRemoteLocationNames)

        # The rendered line can name places the structured list omitted.
        if not accepted:
            rendered = payload.get("_hires_remotely_in") or []
            accepted = [r for r in rendered if r.lower() not in {"everywhere", "anywhere"}]

        if accepted:
            codes, unresolved = _resolve_names(accepted)
            if unresolved:
                log.debug(
                    "wellfound.unresolved_location",
                    extra={"job_id": listing.id, "tokens": unresolved[:10]},
                )
            return codes, reasons

        # Nothing stated anywhere -> Wellfound shows "Everywhere". Trust it only
        # as far as the prose allows.
        restriction = payload.get("_prose_restriction")
        if restriction:
            codes, _ = _resolve_names([restriction])
            reasons.append(
                f"wellfound: listed as worldwide but the posting text restricts "
                f"candidates to {restriction}"
            )
            return codes, reasons

        reasons.append(
            "wellfound: candidate location unstated by the source; treated as "
            "worldwide (unverified)"
            if not payload.get("_detail_url")
            else "wellfound: candidate location unstated; posting text states no restriction"
        )
        return [WORLDWIDE], reasons

    @staticmethod
    def _resolve_salary(
        listing: WellfoundListing, job_ld: dict[str, Any]
    ) -> tuple[float | None, float | None, str | None]:
        base = job_ld.get("baseSalary") if isinstance(job_ld.get("baseSalary"), dict) else None
        if base:
            value = base.get("value")
            if isinstance(value, dict):
                minimum = value.get("minValue")
                maximum = value.get("maxValue")
                currency = base.get("currency")
                if minimum is not None or maximum is not None:
                    return (
                        float(minimum) if minimum is not None else None,
                        float(maximum) if maximum is not None else None,
                        str(currency).upper() if currency else None,
                    )

        # "$80k – $90k • 5.0% – 10.0%" — equity trails the bullet, so cut there
        # before parsing or the percentages read as a pay range.
        text = (listing.compensation or "").split("•")[0]
        return parse_salary_text(text)
