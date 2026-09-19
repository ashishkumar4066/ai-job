"""We Work Remotely aggregator-board adapter — per-category RSS feeds.

Endpoint (verified live 2026-09-19):
    GET https://weworkremotely.com/categories/{category}.rss
    -> RSS 2.0; each <item> carries title, region, category, description
       (HTML), pubDate, guid/link, and on most rows country, state, skills,
       type and expires_at.

WWR's JSON API needs a token issued on request; the RSS feeds are public and
robots.txt allows them. Its job URL is kept as `apply_url` and
`ats="weworkremotely"` credits the board.

Live quirks encoded below:
  * **`region` is nearly meaningless.** 123 of 127 sampled rows said
    "Anywhere in the World", Reddit's "Remote - United States" roles and
    CircleCI's Canada-only ones included. It is trusted only when it narrows
    ("USA Only", "North America Only").
  * **`country` is the real restriction when present**: a flag-prefixed list,
    `"🇨🇦 Canada and 🇺🇸 United States of America"`. It was empty or absent
    on ~80% of rows, and then the "Anywhere" claim is narrowed by, in order,
    a "Headquarters:" line that says remote ("Remote - USA", "Ontario, Canada
    (Remote)"), then the JD prose. Whatever survives both is stored as
    worldwide with an "(unverified)" note, the same stance Wellfound takes.
    Measured on the four programming feeds (69 rows): 17 stated a country,
    and of the 52 "Anywhere" rows 23 were narrowed by headquarters, 8 by
    prose and 21 stayed unverified. One false positive was seen: Lemon.io's
    "startups in the US and Europe" reads as a US restriction.
  * **The title is `"Company: Role"`**; the company is split off it.
  * **The all-jobs feed (`/remote-jobs.rss`) caps each category at 10**, but
    the category feeds show no cap (43, 20, 6, 25 rows), so a category sweep
    is treated as complete and closure runs. `remote-programming-jobs` is its
    own set, not the union of the other programming categories.
  * **An unknown category is not a 404**: it answered 301 with an empty body.
    A body that does not parse as RSS fails the source.
  * `pubDate` is RFC 822. Some rows are from 2024 and still listed; the
    30-day posted window downstream handles them. No salary field exists.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

from app.adapters.base import AdapterError, BaseAdapter
from app.geo import WORLDWIDE, resolve_eligibility, restriction_from_prose, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    employment_type,
    html_to_text,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

FEED_URL = "https://weworkremotely.com/categories/{category}.rss"

DEFAULT_QUERIES: list[dict[str, str]] = [
    {"category": "remote-full-stack-programming-jobs"},
    {"category": "remote-back-end-programming-jobs"},
    {"category": "remote-front-end-programming-jobs"},
    {"category": "remote-devops-sysadmin-jobs"},
    {"category": "remote-programming-jobs"},
]

_FIELDS = (
    "title", "region", "category", "description", "pubDate", "guid", "link",
    "country", "state", "skills", "type", "expires_at",
)
# Regional-indicator pairs: the flag emoji in front of each country name.
_FLAGS = re.compile("[\U0001F1E6-\U0001F1FF]")
_HQ = re.compile(r"Headquarters:\s*</strong>\s*([^<]+)", re.I)
_US_DOTTED = re.compile(r"\bU\.S\.(?:A\.)?(?=\W|$)")
_ANYWHERE = "anywhere in the world"


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Every <item> as a plain dict of its text fields."""
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("no <channel> in the feed")
    items: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        row = {name: (item.findtext(name) or "").strip() for name in _FIELDS}
        items.append(row)
    return items


def split_title(title: str) -> tuple[str | None, str]:
    """ "Legion: Chief Architect" -> ("Legion", "Chief Architect")."""
    company, sep, role = title.partition(": ")
    if not sep or not company.strip() or not role.strip():
        return None, title.strip()
    return company.strip(), role.strip()


def headquarters(description_html: str) -> str | None:
    match = _HQ.search(description_html or "")
    return " ".join(match.group(1).split()) if match else None


def _codes(text: str) -> list[str]:
    text = _US_DOTTED.sub("US", text)
    codes, _, _ = resolve_eligibility(split_location_text(text))
    return codes


def resolve_location(item: dict[str, Any], description_text: str | None) -> tuple[list[str], list[str]]:
    """Return `(codes, advisory reasons)` for one feed item."""
    countries = _FLAGS.sub("", item.get("country") or "").strip()
    if countries:
        codes = _codes(countries)
        if codes:
            return codes, []

    region = (item.get("region") or "").strip()
    if region and region.lower() != _ANYWHERE:
        codes = _codes(re.sub(r"\bonly\b", "", region, flags=re.I))
        if codes:
            return codes, []

    # "Anywhere in the World", or nothing usable: a claim, not a fact.
    hq = headquarters(item.get("description") or "")
    if hq and "remote" in hq.lower():
        codes = _codes(hq)
        if codes and WORLDWIDE not in codes:
            return codes, [
                f"weworkremotely: listed as anywhere, but headquarters reads {hq!r}"
            ]

    restriction = restriction_from_prose(description_text, skip_pay_sentences=True)
    if restriction:
        return _codes(restriction), [
            f"weworkremotely: listed as anywhere, but the posting text restricts "
            f"candidates to {restriction}"
        ]
    return [WORLDWIDE], [
        "weworkremotely: listed as anywhere in the world; treated as worldwide (unverified)"
    ]


def is_us_employer(hq: str | None) -> bool | None:
    """From a non-remote headquarters line: "Princeton, New Jersey, United States"."""
    if not hq or "remote" in hq.lower():
        return None
    codes = _codes(hq)
    if not codes or WORLDWIDE in codes:
        return None
    return codes == ["US"] if len(codes) == 1 else None


class WeWorkRemotelyAdapter(BaseAdapter):
    ats = "weworkremotely"
    # Five programming feeds are never all empty at once.
    empty_result_is_suspicious = True

    async def _feed(self, category: str, source: SourceConfig) -> list[dict[str, Any]]:
        url = FEED_URL.format(category=category)
        text = await self._request(
            url,
            decode=lambda response: response.text,
            company=source,
            headers={"Accept": "application/rss+xml, application/xml;q=0.9"},
        )
        try:
            return parse_feed(text)
        except (ET.ParseError, ValueError) as exc:
            # An unknown category answers 301 with an empty body.
            raise AdapterError(
                f"We Work Remotely feed {category!r} is not RSS: {exc}",
                ats=self.ats,
                company=source.company,
            ) from exc

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES

        raws: list[RawJob] = []
        seen: set[str] = set()
        for query in queries:
            category = str(query.get("category") or "").strip().removesuffix(".rss")
            if not category:
                raise AdapterError(
                    "weworkremotely query needs a `category` slug",
                    ats=self.ats,
                    company=company.company,
                )
            for item in await self._feed(category, company):
                link = item.get("link") or item.get("guid")
                if not link or not item.get("title"):
                    continue
                native_id = link.rstrip("/").rsplit("/", 1)[-1]
                if native_id in seen:
                    continue
                seen.add(native_id)
                raws.append(RawJob(native_id=native_id, payload={**item, "link": link}))
        return raws

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        item = raw.payload
        employer, title = split_title(item["title"])
        employer = employer or company.company

        description_html = item.get("description") or None
        description_text = html_to_text(description_html)
        codes, reasons = resolve_location(item, description_text)
        hq = headquarters(description_html or "")

        places = [_FLAGS.sub("", item.get("country") or "").strip(), item.get("region")]
        try:
            posted = parsedate_to_datetime(item["pubDate"]) if item.get("pubDate") else None
        except (TypeError, ValueError):
            posted = None
        if posted is not None:
            posted = posted.astimezone(UTC) if posted.tzinfo else posted.replace(tzinfo=UTC)

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=title,
            locations=clean_locations(places),
            remote=True,  # WWR lists remote roles only
            employment_type=employment_type(item.get("type")),
            workplace_type="remote",
            department=item.get("category") or None,
            apply_url=item["link"],  # WWR's page, which links onward
            description_html=description_html,
            description_text=description_text,
            posted_at=posted,
            updated_at=None,
            location_eligibility=codes,
            timezone_restrictions=[],
            salary_min=None,  # the feed has no pay field
            salary_max=None,
            salary_currency=None,
            is_us_employer=is_us_employer(hq),
            eligibility_reasons=reasons,
            raw_json=item,
        )
