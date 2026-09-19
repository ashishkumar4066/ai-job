"""Remote OK aggregator-board adapter — a free JSON feed of remote jobs.

Endpoint (verified live 2026-09-19):
    GET https://remoteok.com/api?tag={tag}
    -> [ {"last_updated", "legal"}, {job}, {job}, ... ]

TERMS OF USE (the `legal` element of every response):

  * **Link back to the Remote OK URL, followed, and name Remote OK as the
    source**, or API access is suspended. `job["url"]` is stored as
    `apply_url`, and the dashboard's apply links carry no `nofollow`.
    `ats="remoteok"` carries the attribution the dashboard and Telegram show.
  * The logo is a registered trademark and is never used; the name is fine.

Live quirks encoded below:
  * **Element 0 is the legal notice, not a job.** Rows are recognised by
    having an `id` and a `url`, not by position.
  * **`tag` filters, and an unknown tag returns nothing** (just the notice),
    so it fails closed, and an empty sweep is treated as breakage.
  * **No pagination.** Each tag returns its newest ~100 rows, reaching back
    about two months. A job that drops out of that window has not closed, so
    every fetch is marked truncated and closure never runs here.
  * **Text is mojibake at the source.** UTF-8 bytes were decoded as Latin-1
    before encoding to JSON: `"a\\u00c2\\u00a0"` is a no-break space and
    `"BogotÃ¡"` is Bogotá. Undone per string with a Latin-1 round trip, which
    leaves already-correct text alone because it fails to round-trip.
  * **Most stated locations are a bare city.** Of 237 rows across the three
    default tags, 145 named a place the resolver cannot place ("Dehradun, ",
    "Toronto, ", and placeholders like "Posts, " or "Job, "). They fail the
    location rule and are stored flagged, not guessed.
  * **`location` is free text, and blank on 28 of those 237 rows.** Blank does not
    mean worldwide: DomainTools' blank-location role says "US-based" in its
    JD. The job page's JSON-LD claims `"Anywhere"` even for "Select USA Remote
    Locations", so it cannot settle the question either. A blank (or bare
    "Remote") location is treated as a worldwide *claim*, narrowed by the
    JD's own prose, as Wellfound's "Everywhere" is.
  * **Salary is integer USD, 0 when unstated.** One employer posted
    10,000-750,000 on three rows; a range whose top is over ten times its
    bottom is a placeholder and is read as unstated.
  * `date` is ISO-8601 with an offset. No update timestamp exists.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import AdapterError, BaseAdapter
from app.geo import WORLDWIDE, resolve_eligibility, restriction_from_prose, split_location_text
from app.normalize import (
    build_source_key,
    clean_locations,
    employment_type,
    html_to_text,
    parse_iso_datetime,
)
from app.schemas import JobPosting, RawJob, SourceConfig, slugify

log = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"

# Measured 2026-09-19: dev/engineer/backend return ~100 rows each and overlap,
# so the three tags gave 237 distinct rows.
DEFAULT_QUERIES: list[dict[str, str]] = [
    {"tag": "dev"},
    {"tag": "engineer"},
    {"tag": "backend"},
]

# Locations that state no place at all.
_UNSTATED = {"", "remote", "remoto", "remote only", "fully remote"}

# Above this ratio a salary range is a placeholder, not an offer.
_MAX_SALARY_SPREAD = 10


def repair_mojibake(value: str | None) -> str | None:
    """Undo UTF-8 text that was decoded as Latin-1 upstream.

    Text that is already correct either has no Latin-1 range characters (and
    is returned unchanged) or does not survive the round trip, so it is never
    mangled.
    """
    if not value or not any("\x80" <= ch <= "\xff" for ch in value):
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


class RemoteOkJob(BaseModel):
    """Permissive model of one posting. This API is unversioned."""

    model_config = ConfigDict(extra="allow")

    id: int | str
    url: str
    position: str
    company: str | None = None
    date: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    location: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    original: bool | None = None


def parse_salary(job: RemoteOkJob) -> tuple[float | None, float | None, str | None]:
    low = job.salary_min if job.salary_min and job.salary_min > 0 else None
    high = job.salary_max if job.salary_max and job.salary_max > 0 else None
    if low is None and high is None:
        return None, None, None
    if low is not None and high is not None and high > low * _MAX_SALARY_SPREAD:
        return None, None, None
    return low, high, "USD"


def resolve_location(
    location: str | None, description_text: str | None
) -> tuple[list[str], list[str], list[str]]:
    """Return `(codes, timezones, advisory reasons)` for one row."""
    text = (location or "").strip().strip(",").strip()
    if text.lower() not in _UNSTATED:
        codes, timezones, unresolved = resolve_eligibility(split_location_text(text))
        if codes:
            return codes, timezones, []
        return (
            [],
            timezones,
            [f"remoteok: location {text!r} names no country the resolver knows"],
        )

    restriction = restriction_from_prose(description_text, skip_pay_sentences=True)
    if restriction:
        codes, _, _ = resolve_eligibility([restriction])
        return (
            codes,
            [],
            [f"remoteok: location unstated, but the posting text restricts candidates to {restriction}"],
        )
    return (
        [WORLDWIDE],
        [],
        ["remoteok: candidate location unstated; treated as worldwide (unverified)"],
    )


class RemoteOkAdapter(BaseAdapter):
    ats = "remoteok"
    # A known tag always has rows; an empty sweep means breakage.
    empty_result_is_suspicious = True

    async def fetch(self, company: SourceConfig) -> list[RawJob]:
        queries = list(company.queries) or DEFAULT_QUERIES

        raws: list[RawJob] = []
        seen: set[str] = set()
        for params in queries:
            data = await self.get_json(API_URL, params=dict(params), company=company)
            if not isinstance(data, list):
                raise AdapterError(
                    f"unexpected Remote OK payload for {params}: {type(data).__name__}",
                    ats=self.ats,
                    company=company.company,
                )
            for job in data:
                # Element 0 is the legal notice. Rows without a Remote OK URL
                # cannot honour the link-back terms, so they are not stored.
                if not isinstance(job, dict) or job.get("id") is None or not job.get("url"):
                    continue
                native_id = str(job["id"])
                if native_id in seen:
                    continue
                seen.add(native_id)
                raws.append(RawJob(native_id=native_id, payload=job))

        # Each tag serves only its newest ~100 rows (see module docstring).
        self.fetch_truncated = True
        return raws

    def normalize(self, raw: RawJob, company: SourceConfig) -> JobPosting:
        job = RemoteOkJob.model_validate(raw.payload)

        employer = (repair_mojibake(job.company) or company.company).strip()
        description_html = repair_mojibake(job.description) or None
        description_text = html_to_text(description_html)
        location = repair_mojibake(job.location)

        codes, timezones, reasons = resolve_location(location, description_text)
        salary_min, salary_max, currency = parse_salary(job)

        return JobPosting(
            source_key=build_source_key(self.ats, slugify(employer), raw.native_id),
            ats=self.ats,
            company=employer,
            title=(repair_mojibake(job.position) or "").strip(),
            locations=clean_locations([(location or "").strip().strip(",")]),
            remote=True,  # Remote OK lists remote roles only
            employment_type=_employment_type(job.tags),
            workplace_type="remote",
            department=None,
            # Remote OK's own URL, never the employer's — required by the terms.
            apply_url=job.url,
            description_html=description_html,
            description_text=description_text,
            posted_at=parse_iso_datetime(job.date),
            updated_at=None,  # the feed exposes no update stamp
            location_eligibility=codes,
            timezone_restrictions=timezones,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            is_us_employer=None,  # no employer-HQ field
            eligibility_reasons=reasons,
            raw_json=raw.payload,
        )


def _employment_type(tags: list[Any]) -> str | None:
    """Tags mix skills with commitment ("full time", "contract")."""
    for tag in tags:
        value = employment_type(str(tag))
        if value:
            return value
    return None
