"""RemoteYeah adapter — remote engineering board with per-country listing pages.

Pages (verified live 2026-09-19):
    listing  GET https://remoteyeah.com/{path}[/page/N]   (HTML, 25 cards a page)
    detail   GET https://remoteyeah.com/jobs/{slug}        (HTML + JSON-LD JobPosting)

No API. `path` is one of RemoteYeah's own listing slugs; the useful ones are
`remote-{role}-jobs-in-india`, which list roles open to India, worldwide
roles included. robots.txt allows everything; requests are paced anyway.

Live quirks encoded below:
  * **An unknown slug fails OPEN.** `remote-bogusrole-jobs-in-india`
    redirects to `remote-jobs-in-india` (every role) with a 200. The final
    URL of every listing request is checked against the one asked for, and a
    mismatch fails the source.
  * **Pagination is a path, `/page/N`.** `?page=N` is ignored (it serves page
    1 again). Past the last page the site redirects back to the last page,
    which the same URL check turns into the end of the sweep.
  * **Cards are newest first and carry an exact `<time datetime>`**, except
    "Featured" cards pinned to the top of page 1, which carry none. A sweep
    stops at the first dated card older than `remoteyeah_max_age_days`, so
    it is always truncated and closure never runs.
  * **Location tags are RemoteYeah's own classification** ("🇮🇳 India",
    "🌍 Worldwide", "🌍 Asia") and are taken as stated. They cannot be
    second-guessed from the prose: the description RemoteYeah serves is its
    own bullet summary (Description / Requirements / Benefits), not the
    employer's JD, so restrictions stated in the original may not survive.
  * **Details are read only for candidates** (wanted title, India or
    worldwide on the card). JSON-LD adds `applicantLocationRequirements`,
    `baseSalary` with a real currency (Lifen's EUR 65-75K) and
    `monthsOfExperience`. Card pay ("$160K—$200K / year") is kept in
    `raw_json` only: the detail is the one place the currency is stated.
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
from app.geo import WORLDWIDE, resolve_eligibility, split_location_text
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

SITE = "https://remoteyeah.com"

DEFAULT_QUERIES: list[dict[str, str]] = [
    {"path": "remote-software-engineer-jobs-in-india"},
    {"path": "remote-backend-engineer-jobs-in-india"},
    {"path": "remote-full-stack-engineer-jobs-in-india"},
    {"path": "remote-artificial-intelligence-engineer-jobs-in-india"},
    {"path": "remote-machine-learning-engineer-jobs-in-india"},
    {"path": "remote-platform-engineer-jobs-in-india"},
]

_PATH = re.compile(r"^remote-[a-z0-9-]+$")
_CARD = re.compile(r'<article class="job-card\b.*?</article>', re.S)
_HREF = re.compile(r'href="https://remoteyeah\.com/jobs/([^"]+)"\s+class="job-card-title"')
_TITLE = re.compile(r'<span class="job-card-title-text">(.*?)</span>', re.S)
_COMPANY = re.compile(r'<span class="job-card-company">(.*?)</span>', re.S)
_TIME = re.compile(r'<time class="job-card-published" datetime="([^"]+)"')
_EXPERIENCE = re.compile(r'class="job-card-experience"[^>]*>\s*Exp:\W*(\d+)\s*years?', re.S)
_SALARY = re.compile(r'<div class="badge-success[^"]*">\s*<span[^>]*>(.*?)</span>', re.S)
_LOCATION = re.compile(r'class="badge-secondary job-card-tag-location"[^>]*>(.*?)</a>', re.S)
_SKILL = re.compile(r'class="badge-secondary job-card-tag-skill"[^>]*>(.*?)</a>', re.S)
_ROLE = re.compile(r'<a class="badge-secondary" href="[^"]*">(.*?)</a>', re.S)
_LD_JSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


# ----------------------------------------------------------------- Parsing
def _text(value: str) -> str:
    """Tag-stripped, entity-decoded, whitespace-collapsed text."""
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return re.sub(r"\s+", " ", value).strip()


def _place(value: str) -> str:
    """A location tag without its flag or globe emoji: "🇮🇳 India" -> "India"."""
    return re.sub(r"[^\w\s,.()'-]", " ", _text(value)).strip()


def parse_cards(page_html: str) -> list[dict[str, Any]]:
    """Every job card on a listing page, as plain dicts."""
    cards: list[dict[str, Any]] = []
    for chunk in _CARD.findall(page_html or ""):
        href = _HREF.search(chunk)
        title = _TITLE.search(chunk)
        if not href or not title:
            continue
        stamp = _TIME.search(chunk)
        years = _EXPERIENCE.search(chunk)
        salary = _SALARY.search(chunk)
        role = _ROLE.search(chunk)
        company = _COMPANY.search(chunk)
        cards.append(
            {
                "id": href.group(1),
                "url": f"{SITE}/jobs/{href.group(1)}",
                "title": _text(title.group(1)),
                "company": _text(company.group(1)) if company else None,
                "published": stamp.group(1) if stamp else None,
                "featured": "Featured" in chunk.split('class="job-card-inner"', 1)[0],
                "years": int(years.group(1)) if years else None,
                "salary_text": _text(salary.group(1)) if salary else None,
                "role": _text(role.group(1)) if role else None,
                "skills": [_text(s) for s in _SKILL.findall(chunk)],
                "places": [_place(p) for p in _LOCATION.findall(chunk)],
            }
        )
    return cards


def extract_job_posting(page_html: str) -> dict[str, Any] | None:
    for match in _LD_JSON.finditer(page_html or ""):
        try:
            payload = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("@type") == "JobPosting":
            return payload
    return None


def places_to_codes(places: list[str]) -> list[str]:
    codes, _, _ = resolve_eligibility(p for place in places for p in split_location_text(place))
    return codes


def required_places(job_ld: dict[str, Any]) -> list[str]:
    raw = job_ld.get("applicantLocationRequirements")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    return [str(i.get("name")) for i in items if isinstance(i, dict) and i.get("name")]


def parse_salary(job_ld: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    base = job_ld.get("baseSalary")
    if not isinstance(base, dict) or not isinstance(base.get("value"), dict):
        return None, None, None
    value = base["value"]

    def figure(key: str) -> float | None:
        number = value.get(key)
        return float(number) if isinstance(number, (int, float)) and number > 0 else None

    low, high = figure("minValue"), figure("maxValue")
    if low is None and high is None:
        return None, None, None
    return low, high, (str(base.get("currency")).upper() if base.get("currency") else None)


def years_of(card: dict[str, Any], job_ld: dict[str, Any]) -> int | None:
    experience = job_ld.get("experienceRequirements")
    if isinstance(experience, dict):
        months = experience.get("monthsOfExperience")
        if isinstance(months, (int, float)) and months > 0:
            return int(months) // 12
    return card.get("years")


def _is_candidate(card: dict[str, Any]) -> bool:
    codes = places_to_codes(card.get("places") or [])
    return bool({"IN", WORLDWIDE} & set(codes)) and classify(
        card["title"], None, get_filters().role
    ).passed


# ----------------------------------------------------------------- Adapter
class RemoteYeahAdapter(BaseAdapter):
    ats = "remoteyeah"
    empty_result_is_suspicious = True

    def __init__(self, client: Any = None) -> None:
        super().__init__(client=client)
        self._pacer = RequestPacer(get_settings().remoteyeah_request_interval_seconds)

    async def _page(self, url: str, source: SourceConfig) -> tuple[str, str]:
        """GET a page; returns `(final URL, body)` so redirects are visible."""
        await self._pacer.wait()
        return await self._request(
            url,
            decode=lambda response: (str(response.url), response.text),
            company=source,
            headers={"Accept": "text/html"},
        )

    async def _fetch_listing(
        self, path: str, source: SourceConfig, cutoff: datetime
    ) -> list[dict[str, Any]]:
        settings = get_settings()
        seen: dict[str, dict[str, Any]] = {}
        for page in range(1, settings.remoteyeah_max_pages + 1):
            wanted = f"{SITE}/{path}" + (f"/page/{page}" if page > 1 else "")
            final, body = await self._page(wanted, source)
            if final.split("#")[0].rstrip("/") != wanted:
                if page == 1:
                    raise AdapterError(
                        f"RemoteYeah redirected {path!r} to {final}; the slug is unknown",
                        ats=self.ats,
                        company=source.company,
                    )
                return list(seen.values())  # walked past the last page
            cards = parse_cards(body)
            if page == 1 and not cards:
                raise AdapterError(
                    f"no job cards on {wanted}; the page markup changed",
                    ats=self.ats,
                    company=source.company,
                )
            stale = False
            for card in cards:
                stamp = parse_iso_datetime(card["published"])
                if stamp is not None and stamp < cutoff:
                    stale = True  # newest first: the rest are older still
                    continue
                seen.setdefault(card["id"], card)
            if stale or not cards:
                return list(seen.values())
        log.warning("remoteyeah.page_cap", extra={"path": path, "pages": settings.remoteyeah_max_pages})
        return list(seen.values())

    async def _fetch_detail(self, card: dict[str, Any], source: SourceConfig) -> None:
        try:
            _, body = await self._page(card["url"], source)
        except AdapterError as exc:
            log.warning("remoteyeah.detail_failed", extra={"job_id": card["id"], "error": str(exc)})
            return
        job_ld = extract_job_posting(body)
        if not job_ld:
            log.warning("remoteyeah.detail_no_jsonld", extra={"job_id": card["id"]})
            return
        card["_detail"] = {
            key: job_ld.get(key)
            for key in (
                "description",
                "datePosted",
                "validThrough",
                "employmentType",
                "applicantLocationRequirements",
                "baseSalary",
                "experienceRequirements",
            )
        }

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        settings = get_settings()
        queries = list(company.queries) or DEFAULT_QUERIES
        cutoff = datetime.now(UTC) - timedelta(days=settings.remoteyeah_max_age_days)

        by_id: dict[str, dict[str, Any]] = {}
        for query in queries:
            path = str(query.get("path") or "").strip().strip("/")
            if not _PATH.match(path):
                raise AdapterError(
                    f"remoteyeah query needs a listing `path` like "
                    f"'remote-backend-engineer-jobs-in-india', got {path!r}",
                    ats=self.ats,
                    company=company.company,
                )
            for card in await self._fetch_listing(path, company, cutoff):
                by_id.setdefault(card["id"], {**card, "_path": path})

        candidates = [card for card in by_id.values() if _is_candidate(card)]
        budget = settings.remoteyeah_detail_budget
        if len(candidates) > budget:
            log.warning(
                "remoteyeah.detail_budget_exhausted",
                extra={"budget": budget, "candidates": len(candidates)},
            )
        for card in candidates[:budget]:
            await self._fetch_detail(card, company)

        # Every sweep stops at the age cutoff (see module docstring).
        self.fetch_truncated = True
        return [RawJob(native_id=card_id, payload=card) for card_id, card in by_id.items()]

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        card = raw.payload
        detail: dict[str, Any] = card.get("_detail") or {}
        if not card.get("title") or not card.get("url"):
            raise ValueError("RemoteYeah card without a title or url")

        employer = (card.get("company") or company.company).strip()
        places = required_places(detail) or list(card.get("places") or [])
        codes = places_to_codes(places)

        years = years_of(card, detail)
        body = html_to_text(detail.get("description"))
        fallback = (
            f"Top skills: {', '.join(card['skills'])}." if card.get("skills") else None
        )
        description_text = "\n\n".join(
            part
            for part in (f"Minimum {years}+ years experience." if years else None, body or fallback)
            if part
        ) or None
        salary_min, salary_max, currency = parse_salary(detail)
        kinds = detail.get("employmentType")

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=card["title"],
            locations=clean_locations(places),
            remote=True,  # RemoteYeah lists remote roles only
            employment_type=employment_type(kinds[0] if isinstance(kinds, list) and kinds else kinds),
            workplace_type="remote",
            department=card.get("role"),
            apply_url=card["url"],  # RemoteYeah's page, which links onward
            description_html=detail.get("description") or None,
            description_text=description_text,
            posted_at=parse_iso_datetime(detail.get("datePosted") or card.get("published")),
            updated_at=None,
            location_eligibility=codes,
            timezone_restrictions=[],
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            is_us_employer=None,  # no employer-HQ field
            raw_json=raw.payload,
        )
