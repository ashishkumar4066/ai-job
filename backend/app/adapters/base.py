"""The adapter contract every ATS integration implements."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.config import get_settings
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

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        company: CompanyConfig | None = None,
    ) -> Any:
        """GET + JSON decode, retrying transient failures with backoff."""
        settings = get_settings()
        client = await self._get_client()
        attempts = settings.http_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await client.get(url, params=params)
                if response.status_code >= 500 and attempt < attempts:
                    last_error = AdapterError(f"HTTP {response.status_code} from {url}")
                    await asyncio.sleep(0.5 * attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                # 4xx is a config problem (bad token/slug) — do not retry.
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

    # -- Convenience --------------------------------------------------------
    def normalize_all(self, raws: list[RawJob], company: CompanyConfig) -> list[JobPosting]:
        """Normalize a batch, dropping (and logging) individually bad rows.

        A single malformed posting must not cost us the other 500.
        """
        out: list[JobPosting] = []
        for raw in raws:
            try:
                out.append(self.normalize(raw, company))
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
