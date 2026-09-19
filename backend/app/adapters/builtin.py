"""Built In adapter — US-centric tech board with a working country filter.

Pages (verified live 2026-09-19):
    listing  GET https://builtin.com/jobs/{path}?country=IND&page=N   (HTML)
    detail   GET https://builtin.com/job/{slug}/{id}                   (HTML + JSON-LD)

No API and no embedded JSON on the listing, so job cards are parsed out of the
HTML. The parser keys only on Built In's own hooks (`id="job-card-N"`,
`data-id="job-card-title"`, `data-id="company-title"`, and the icon that labels
each attribute), and a first page with no cards fails the source loudly rather
than storing nothing.

robots.txt disallows `/jobs*?search=`, `/jobs/*mid-level` and similar facet
paths, so queries are restricted to a category path plus `country`. With
`country=IND` every card is a role Built In lists for India.

Live quirks encoded below:
  * **Deep pages never run dry.** Page 15 of a ~113-job result still answered
    with 10 cards. A sweep stops at the first page that adds no new id, or at
    `builtin_max_pages`, and either way marks the fetch truncated: nothing
    tells a complete sweep from a cut one, so closure never runs here and the
    validity pass's "missed last sweep" check covers staleness.
  * **Countries are ISO-3166 alpha-3** on both cards (`"Pune, Maharashtra,
    IND"`, bare `"IND"`) and JSON-LD (`applicantLocationRequirements:
    [{"name": "IND"}, ...]`). The shared resolver reads a bare `IN` as Indiana
    and does not know `IND`, so they are mapped here.
  * **Card pay has no currency** ("110K-286K Annually", "4M-4M Annually" on
    an India role), and the JSON-LD has no `baseSalary`. It is kept in
    `raw_json` and read as unstated rather than guessed as USD.
  * **The JSON-LD tag is HTML-escaped**, `type="application/ld&#x2B;json"`,
    and the posting sits inside `@graph`. The shared Wellfound extractor finds
    neither, so this module has its own.
  * **Detail pages are read only for candidates** (titles the role rule already
    passes); they add the full JD, the exact `datePosted` and the stated
    candidate countries. Without one, the card's relative age ("Reposted 4
    Hours Ago", "One Month Ago") dates the row.
  * Built In is also a discovery surface: many of its employers post through
    Greenhouse, Lever or Ashby, which `companies.yaml` can read directly.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.adapters.base import AdapterError, BaseAdapter, RequestPacer
from app.config import get_settings
from app.eligibility import get_filters
from app.geo import country_code, resolve_eligibility, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    employment_type,
    html_to_text,
    parse_iso_datetime,
)
from app.roles import classify
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

SITE = "https://builtin.com"

DEFAULT_QUERIES: list[dict[str, str]] = [
    {"path": "remote/dev-engineering", "country": "IND"},
]

# Alpha-3 -> alpha-2 for the countries Built In's India-filtered results name.
ISO3: dict[str, str] = {
    "IND": "IN", "USA": "US", "CAN": "CA", "GBR": "GB", "IRL": "IE", "AUS": "AU",
    "NZL": "NZ", "SGP": "SG", "DEU": "DE", "FRA": "FR", "NLD": "NL", "ESP": "ES",
    "PRT": "PT", "ITA": "IT", "POL": "PL", "ROU": "RO", "CHE": "CH", "AUT": "AT",
    "BEL": "BE", "SWE": "SE", "NOR": "NO", "DNK": "DK", "FIN": "FI", "CZE": "CZ",
    "HUN": "HU", "GRC": "GR", "ISR": "IL", "ARE": "AE", "SAU": "SA", "TUR": "TR",
    "BRA": "BR", "MEX": "MX", "ARG": "AR", "COL": "CO", "CHL": "CL", "PER": "PE",
    "CRI": "CR", "URY": "UY", "PHL": "PH", "IDN": "ID", "MYS": "MY", "THA": "TH",
    "VNM": "VN", "JPN": "JP", "KOR": "KR", "CHN": "CN", "HKG": "HK", "TWN": "TW",
    "PAK": "PK", "BGD": "BD", "LKA": "LK", "NPL": "NP", "ZAF": "ZA", "NGA": "NG",
    "KEN": "KE", "EGY": "EG", "UKR": "UA", "SRB": "RS", "BGR": "BG", "HRV": "HR",
    "LTU": "LT", "LVA": "LV", "EST": "EE", "SVK": "SK", "SVN": "SI",
}

_CARD_SPLIT = re.compile(r'(?=<div id="job-card-\d+")')
_CARD_ID = re.compile(r'<div id="job-card-(\d+)"')
_TITLE = re.compile(r'data-id="job-card-title"[^>]*data-alias="([^"]+)"[^>]*>([^<]+)</a>')
_TITLE_HREF = re.compile(r'<a href="(/job/[^"]+)"[^>]*data-id="job-card-title"[^>]*>([^<]+)</a>')
_COMPANY = re.compile(r'data-id="company-title"[^>]*>\s*<span>([^<]+)</span>')
_AGE = re.compile(r'fa-clock[^>]*></i>\s*([^<]+?)\s*</span>')
_ATTRIBUTE = re.compile(
    r'<i class="fa-regular (fa-[a-z-]+) fs-xs text-pretty-blue[^"]*"></i></div>\s*'
    r'(?:<div>)?\s*<span class="font-barlow text-gray-04[^"]*"([^>]*)>([^<]*)</span>'
)
_TOOLTIP = re.compile(r'(?:data-bs-title|title)="([^"]*)"')
_SUMMARY = re.compile(r'<div class="fs-sm fw-regular mb-md text-gray-04">([^<]+)</div>')
_SKILL = re.compile(r'<span class="fs-xs text-gray-04 mx-sm">([^<]+)</span>')
_LD_JSON = re.compile(
    r'<script[^>]*type="application/ld(?:\+|&#x2B;|&#43;)json"[^>]*>(.*?)</script>', re.S
)
_AGE_PARTS = re.compile(
    r"(?P<n>\d+|an?|one)\s+(?P<unit>minute|hour|day|week|month|year)s?", re.IGNORECASE
)
_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}

# The icon that labels each card attribute.
_ICON_FIELDS = {
    "fa-house-building": "workplace",
    "fa-location-dot": "location",
    "fa-trophy": "seniority",
    "fa-sack-dollar": "salary_text",
}


# ----------------------------------------------------------------- Parsing
def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _tooltip_places(attrs: str) -> list[str]:
    """Places listed in a "8 Locations" / "25 Offices" hover tooltip."""
    match = _TOOLTIP.search(attrs)
    if not match:
        return []
    inner = html.unescape(match.group(1))  # tooltip markup is itself escaped
    parts = re.split(r"<br\s*/?>|</div>", inner)
    places = [_text(re.sub(r"<[^>]+>", "", part)) for part in parts]
    return [p for p in places if p and not re.fullmatch(r"\(\d+\)", p)]


def parse_cards(page_html: str) -> list[dict[str, Any]]:
    """Every job card on a listing page, as plain dicts."""
    cards: list[dict[str, Any]] = []
    for chunk in _CARD_SPLIT.split(page_html or "")[1:]:
        card_id = _CARD_ID.match(chunk)
        title = _TITLE.search(chunk)
        if title:
            path, name = title.group(1), title.group(2)
        else:
            href = _TITLE_HREF.search(chunk)
            if not href:
                continue
            path, name = href.group(1), href.group(2)
        company = _COMPANY.search(chunk)
        age = _AGE.search(chunk)
        card: dict[str, Any] = {
            "id": card_id.group(1) if card_id else path.rstrip("/").rsplit("/", 1)[-1],
            "title": _text(name),
            "url": path,
            "company": _text(company.group(1)) if company else None,
            "age_text": _text(age.group(1)) if age else None,
            "summary": _text(s.group(1)) if (s := _SUMMARY.search(chunk)) else None,
            "skills": [_text(s) for s in _SKILL.findall(chunk)],
            "places": [],
        }
        for icon, attrs, value in _ATTRIBUTE.findall(chunk):
            field = _ICON_FIELDS.get(icon)
            if field and field not in card:
                card[field] = _text(value)
                if field == "location":
                    card["places"] = _tooltip_places(attrs) or [_text(value)]
        cards.append(card)
    return cards


def extract_job_posting(page_html: str) -> dict[str, Any] | None:
    """The schema.org JobPosting on a detail page, from `@graph` or top level."""
    for match in _LD_JSON.finditer(page_html or ""):
        try:
            payload = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        items = payload.get("@graph", [payload]) if isinstance(payload, dict) else payload
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def place_country(place: str) -> str | None:
    """Country of one Built In place: `"Pune, Maharashtra, IND"`, `"IND"`, `"India"`."""
    last = place.rsplit(",", 1)[-1].strip()
    if last.upper() in ISO3:
        return ISO3[last.upper()]
    if len(last) == 2:
        return None  # "IN", "KA": alpha-2 and state codes collide; not guessed
    return country_code(last) or next(
        iter(resolve_eligibility(split_location_text(place))[0]), None
    )


def places_to_codes(places: list[str]) -> list[str]:
    codes: list[str] = []
    for place in places:
        code = place_country(place)
        if code and code not in codes:
            codes.append(code)
    return codes


def required_countries(job_ld: dict[str, Any]) -> list[str]:
    """`applicantLocationRequirements` as alpha-2 codes."""
    raw = job_ld.get("applicantLocationRequirements")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    names = [str(item.get("name") or "") for item in items if isinstance(item, dict)]
    return places_to_codes([n for n in names if n])


def parse_age(value: str | None, now: datetime) -> datetime | None:
    """ "Reposted 4 Hours Ago" / "One Month Ago" / "Yesterday" -> a date, floored to the day."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "yesterday" in (value or "").lower():
        return today - timedelta(days=1)
    match = _AGE_PARTS.search(value or "")
    if not match:
        return None
    raw = match.group("n").lower()
    count = 1 if raw in ("a", "an", "one") else int(raw)
    moment = now - timedelta(days=count * _UNIT_DAYS[match.group("unit").lower()])
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def workplace_of(value: str | None) -> tuple[bool, str | None]:
    text = (value or "").lower()
    if "remote" in text:
        return True, "remote"  # "Remote or Hybrid" still offers remote
    if "hybrid" in text:
        return False, "hybrid"
    if "office" in text:
        return False, "onsite"
    return False, None


# ----------------------------------------------------------------- Adapter
class BuiltInAdapter(BaseAdapter):
    ats = "builtin"
    empty_result_is_suspicious = True

    def __init__(self, client: Any = None) -> None:
        super().__init__(client=client)
        self._pacer = RequestPacer(get_settings().builtin_request_interval_seconds)

    async def _page(self, url: str, params: dict[str, Any] | None, source: SourceConfig) -> str:
        await self._pacer.wait()
        return await self.get_text(url, params=params, company=source)

    async def _fetch_listing(
        self, query: dict[str, Any], source: SourceConfig
    ) -> list[dict[str, Any]]:
        path = str(query.get("path") or "").strip().strip("/")
        if not path or "search" in query or "?" in path:
            raise AdapterError(
                "builtin query needs a category `path`; `search` is disallowed by robots.txt",
                ats=self.ats,
                company=source.company,
            )
        url = f"{SITE}/jobs/{path}"
        params = {k: v for k, v in query.items() if k != "path"}

        seen: dict[str, dict[str, Any]] = {}
        for page in range(1, get_settings().builtin_max_pages + 1):
            cards = parse_cards(await self._page(url, {**params, "page": page}, source))
            if page == 1 and not cards:
                raise AdapterError(
                    f"no job cards on {url}; the page markup changed",
                    ats=self.ats,
                    company=source.company,
                )
            new = [card for card in cards if card["id"] not in seen]
            if not new:
                break
            for card in new:
                seen[card["id"]] = {**card, "_query": query}
        # Deep pages keep answering, so no sweep is known to be complete.
        self.fetch_truncated = True
        return list(seen.values())

    async def _fetch_detail(self, card: dict[str, Any], source: SourceConfig) -> None:
        try:
            page_html = await self._page(SITE + card["url"], None, source)
        except AdapterError as exc:
            log.warning("builtin.detail_failed", extra={"job_id": card["id"], "error": str(exc)})
            return
        job_ld = extract_job_posting(page_html)
        if not job_ld:
            log.warning("builtin.detail_no_jsonld", extra={"job_id": card["id"]})
            return
        card["_detail"] = {
            key: job_ld.get(key)
            for key in (
                "description",
                "datePosted",
                "validThrough",
                "employmentType",
                "jobLocationType",
                "applicantLocationRequirements",
            )
        }

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES
        fetched_at = datetime.now(UTC).isoformat()

        by_id: dict[str, dict[str, Any]] = {}
        for query in queries:
            for card in await self._fetch_listing(query, company):
                by_id.setdefault(card["id"], {**card, "_fetched_at": fetched_at})

        rules = get_filters().role
        budget = get_settings().builtin_detail_budget
        candidates = [
            card for card in by_id.values() if classify(card["title"], None, rules).passed
        ]
        if len(candidates) > budget:
            log.warning(
                "builtin.detail_budget_exhausted",
                extra={"budget": budget, "candidates": len(candidates)},
            )
        for card in candidates[:budget]:
            await self._fetch_detail(card, company)

        return [RawJob(native_id=card_id, payload=card) for card_id, card in by_id.items()]

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        card = raw.payload
        detail: dict[str, Any] = card.get("_detail") or {}
        if not card.get("title") or not card.get("url"):
            raise ValueError("Built In card without a title or url")

        employer = (card.get("company") or company.company).strip()
        remote, workplace = workplace_of(card.get("workplace"))
        if detail.get("jobLocationType") == "TELECOMMUTE":
            remote, workplace = True, "remote"

        places = [str(p) for p in card.get("places") or []]
        codes = required_countries(detail) or places_to_codes(places)
        if not codes:
            # The listing was filtered on this country, so Built In lists the
            # role for it even when the card names no parseable place.
            country = str((card.get("_query") or {}).get("country") or "")
            codes = [ISO3[country]] if country in ISO3 else []

        description_html = detail.get("description") or None
        fallback = "\n\n".join(
            part
            for part in (
                card.get("summary"),
                f"Top skills: {', '.join(card['skills'])}." if card.get("skills") else None,
            )
            if part
        )
        description_text = html_to_text(description_html) or fallback or None

        stamp = parse_iso_datetime(detail.get("datePosted"))
        if stamp is None:
            fetched = parse_iso_datetime(card.get("_fetched_at")) or datetime.now(UTC)
            stamp = parse_age(card.get("age_text"), fetched)

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=card["title"],
            locations=clean_locations(places),
            remote=remote,
            employment_type=employment_type(detail.get("employmentType")),
            workplace_type=workplace,
            department=None,
            apply_url=SITE + card["url"],  # Built In's page, which links onward
            description_html=description_html,
            description_text=description_text,
            posted_at=stamp,
            updated_at=None,
            location_eligibility=codes,
            timezone_restrictions=[],
            # Card pay states no currency; kept in raw_json, read as unstated.
            salary_min=None,
            salary_max=None,
            salary_currency=None,
            is_us_employer=None,
            raw_json=raw.payload,
        )
