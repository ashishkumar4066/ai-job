"""The adapter contract every ATS integration implements."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from itertools import chain
from typing import Any

import httpx

from app.config import get_settings
from app.geo import resolve_eligibility, split_location_text
from app.schemas import CompanyConfig, JobPosting, RawJob

log = logging.getLogger(__name__)


class AdapterError(RuntimeError):
    """Raised when a source cannot be fetched or parsed.

    Ingest catches this per-source so one broken board never aborts a run.
    """

    def __init__(self, message: str, *, ats: str = "", company: str = "") -> None:
        super().__init__(message)
        self.ats = ats
        self.company = company


class BaseAdapter(ABC):
    """One adapter per ATS.

    Subclasses implement `fetch` (I/O) and `normalize` (pure mapping) so the
    mapping logic stays testable against recorded fixtures with no network.
    """

    ats: str = "base"
    # Some boards answer 200 with an empty list for an unknown slug. When True,
    # ingest refuses to run the closure sweep on a zero-result fetch for a
    # source that previously had open jobs.
    empty_result_is_suspicious: bool = True

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    # -- HTTP ---------------------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            settings = get_settings()
            self._client = httpx.AsyncClient(
                timeout=settings.http_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "personal-job-aggregator/1.0", "Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """Honour a `Retry-After` header when the server sends one."""
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw.strip()))
        except ValueError:
            return None  # HTTP-date form; fall back to our own backoff

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        company: CompanyConfig | None = None,
    ) -> Any:
        """GET + JSON decode, retrying transient failures with backoff.

        429 gets its own retry budget and exponential backoff: Himalayas rate
        limits, and a rate limit is a "come back later", not the permanent
        config error that other 4xx codes signal.
        """
        settings = get_settings()
        client = await self._get_client()
        attempts = settings.http_max_retries + 1
        rate_limit_budget = settings.rate_limit_max_retries
        last_error: Exception | None = None
        attempt = 0
        rate_limited = 0

        while attempt < attempts:
            attempt += 1
            try:
                response = await client.get(url, params=params)

                if response.status_code == 429:
                    if rate_limited >= rate_limit_budget:
                        raise AdapterError(
                            f"rate limited by {url} after {rate_limited} retries",
                            ats=self.ats,
                            company=company.company if company else "",
                        )
                    delay = self._retry_after_seconds(response) or (
                        settings.rate_limit_base_delay_seconds * (2**rate_limited)
                    )
                    rate_limited += 1
                    attempt -= 1  # a rate limit must not consume the error budget
                    log.warning(
                        "adapter.rate_limited",
                        extra={
                            "ats": self.ats,
                            "url": url,
                            "retry_in_s": round(delay, 2),
                            "attempt": rate_limited,
                        },
                    )
                    await asyncio.sleep(delay)
                    continue

                if response.status_code >= 500 and attempt < attempts:
                    last_error = AdapterError(f"HTTP {response.status_code} from {url}")
                    await asyncio.sleep(0.5 * attempt)
                    continue

                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                # Other 4xx is a config problem (bad token/slug) — do not retry.
                raise AdapterError(
                    f"HTTP {exc.response.status_code} from {url}",
                    ats=self.ats,
                    company=company.company if company else "",
                ) from exc
            except (httpx.TransportError, ValueError) as exc:
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(0.5 * attempt)
                    continue

        raise AdapterError(
            f"failed to fetch {url}: {last_error}",
            ats=self.ats,
            company=company.company if company else "",
        ) from last_error

    # -- Contract -----------------------------------------------------------
    @abstractmethod
    async def fetch(self, company: CompanyConfig) -> list[RawJob]:
        """Return every currently-listed posting for `company`."""

    @abstractmethod
    def normalize(self, raw: RawJob, company: CompanyConfig) -> JobPosting:
        """Map one raw posting into the shared schema. Pure — no I/O."""

    # -- Eligibility enrichment ---------------------------------------------
    def enrich_eligibility(self, posting: JobPosting, company: CompanyConfig) -> JobPosting:
        """Fill eligibility inputs an adapter could not supply itself.

        The aggregator boards publish candidate eligibility directly, so they
        set these during `normalize` and this is a no-op for them. The ATS
        boards publish only where the *job* sits, so eligibility is inferred
        here — once, for all three — from the posting's own location strings,
        falling back to what the curated config asserts about a pre-vetted
        company.

        Only ever fills blanks: an adapter that knows better always wins.
        """
        if not posting.location_eligibility:
            codes, timezones, _ = resolve_eligibility(
                chain.from_iterable(split_location_text(loc) for loc in posting.locations)
            )
            # A pre-vetted company's declared eligibility is a fallback, not an
            # override — a concrete "Bengaluru, India" beats a config default.
            posting.location_eligibility = codes or list(company.location_eligibility)
            if timezones and not posting.timezone_restrictions:
                posting.timezone_restrictions = timezones

        if posting.is_us_employer is None:
            posting.is_us_employer = company.us_employer

        return posting

    # -- Convenience --------------------------------------------------------
    def normalize_all(self, raws: list[RawJob], company: CompanyConfig) -> list[JobPosting]:
        """Normalize a batch, dropping (and logging) individually bad rows.

        A single malformed posting must not cost us the other 500.
        """
        out: list[JobPosting] = []
        for raw in raws:
            try:
                out.append(self.enrich_eligibility(self.normalize(raw, company), company))
            except Exception:
                log.exception(
                    "adapter.normalize_failed",
                    extra={
                        "ats": self.ats,
                        "company": company.company,
                        "native_id": raw.native_id,
                    },
                )
        return out
