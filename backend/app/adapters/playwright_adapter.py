"""Playwright fallback adapter — interface + stub only (Phase 1 scope).

Per the architecture principles, browser automation is a *last resort* for
companies with no public JSON board API. The shape is fixed here so wiring a
real implementation later is a drop-in, but calling it now fails loudly rather
than silently returning zero jobs (which the closure sweep would read as "every
job at this company closed").
"""

from __future__ import annotations

from app.adapters.base import AdapterError, BaseAdapter
from app.schemas import CompanyConfig, JobPosting, RawJob


class PlaywrightAdapter(BaseAdapter):
    ats = "playwright"
    empty_result_is_suspicious = True

    async def fetch(self, company: CompanyConfig) -> list[RawJob]:
        raise AdapterError(
            "PlaywrightAdapter is a Phase 1 stub: no browser scraping implemented yet. "
            f"Remove or disable '{company.company}' in companies.yaml, or use an ATS "
            "adapter (greenhouse, lever, ashby).",
            ats=self.ats,
            company=company.company,
        )

    def normalize(self, raw: RawJob, company: CompanyConfig) -> JobPosting:
        raise AdapterError(
            "PlaywrightAdapter.normalize is not implemented (Phase 1 stub).",
            ats=self.ats,
            company=company.company,
        )
