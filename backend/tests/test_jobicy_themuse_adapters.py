"""Jobicy + The Muse adapter tests against recorded fixtures.

Both fixtures are trimmed captures of real 2026-09-17 responses. CI never hits
a live endpoint.

The cases that matter most:
  * Jobicy's "Anywhere" stays worldwide even when the JD mentions "US based
    candidates" in a pay sentence (a measured false positive of the Wellfound
    prose check, which is why that check is not applied here).
  * The Muse's "Flexible / Remote" is NOT worldwide; eligibility comes only
    from concrete city locations.
  * A Muse fetch stopped by the page cap is marked truncated, and ingest then
    refuses to close jobs it merely did not reach.
"""

from __future__ import annotations

import copy
import logging

import httpx
import pytest
import respx

from app.adapters import get_adapter, is_aggregator, register_adapter
from app.adapters.base import AdapterError
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.jobicy import API_URL as JOBICY_URL
from app.adapters.jobicy import JobicyAdapter
from app.adapters.themuse import API_URL as MUSE_URL
from app.adapters.themuse import MAX_PAGE, TheMuseAdapter
from app.config import get_settings
from app.eligibility import FilterConfig, evaluate
from app.ingest import run_ingest
from app.schemas import RawJob, SourceConfig
from tests.conftest import load_fixture
from tests.test_ingest import count_jobs, mock_all_boards


@pytest.fixture
def jobicy_source() -> SourceConfig:
    return SourceConfig(
        company="Jobicy",
        ats="jobicy",
        queries=[{"count": 200, "industry": "engineering", "geo": "apac"}],
    )


@pytest.fixture
def muse_source() -> SourceConfig:
    return SourceConfig(
        company="The Muse",
        ats="themuse",
        queries=[
            {
                "category": "Software Engineering",
                "level": ["Mid Level", "Senior Level"],
                "location": ["Bangalore, India", "Hyderabad, India"],
            }
        ],
    )


def _jobicy_job(job_id: int) -> dict:
    return next(j for j in load_fixture("jobicy_engineering_apac.json")["jobs"] if j["id"] == job_id)


def _muse_job(job_id: int) -> dict:
    for name in ("themuse_jobs_page0.json", "themuse_jobs_page1.json"):
        for job in load_fixture(name)["results"]:
            if job["id"] == job_id:
                return job
    raise KeyError(job_id)


def _normalize(adapter, source: SourceConfig, payload: dict):
    raw = RawJob(native_id=str(payload["id"]), payload=payload)
    return adapter.normalize_all([raw], source)[0]


def _location_passes(codes: list[str]) -> bool:
    return evaluate(location_eligibility=codes, filters=FilterConfig()).reasons[0].startswith(
        "location_ok"
    )


# -------------------------------------------------------------------- Jobicy
class TestJobicyAdapter:
    @respx.mock
    async def test_fetch_sends_the_query_and_returns_every_posting(
        self, jobicy_source: SourceConfig
    ) -> None:
        route = respx.get(JOBICY_URL).mock(
            return_value=httpx.Response(200, json=load_fixture("jobicy_engineering_apac.json"))
        )

        async with httpx.AsyncClient() as client:
            raws = await JobicyAdapter(client=client).fetch(jobicy_source)

        assert len(raws) == 5
        assert route.call_count == 1, "the terms allow one poll an hour; one query, one call"
        params = route.calls[0].request.url.params
        assert (params["count"], params["industry"], params["geo"]) == ("200", "engineering", "apac")

    @respx.mock
    async def test_rows_without_a_jobicy_url_are_not_stored(
        self, jobicy_source: SourceConfig
    ) -> None:
        payload = load_fixture("jobicy_engineering_apac.json")
        payload["jobs"][0]["url"] = ""
        respx.get(JOBICY_URL).mock(return_value=httpx.Response(200, json=payload))

        async with httpx.AsyncClient() as client:
            raws = await JobicyAdapter(client=client).fetch(jobicy_source)

        assert len(raws) == 4

    @respx.mock
    async def test_an_unknown_geo_slug_fails_loudly(self, jobicy_source: SourceConfig) -> None:
        # Verified live: `geo=india` answers HTTP 200 with an error, not jobs.
        respx.get(JOBICY_URL).mock(
            return_value=httpx.Response(
                200, json={"success": False, "error": "Invalid 'geo' value."}
            )
        )

        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="Invalid 'geo' value"):
                await JobicyAdapter(client=client).fetch(jobicy_source)

    @respx.mock
    async def test_queries_are_merged_and_deduped(self) -> None:
        source = SourceConfig(
            company="Jobicy",
            ats="jobicy",
            queries=[{"geo": "anywhere"}, {"geo": "apac"}],
        )
        respx.get(JOBICY_URL).mock(
            return_value=httpx.Response(200, json=load_fixture("jobicy_engineering_apac.json"))
        )

        async with httpx.AsyncClient() as client:
            raws = await JobicyAdapter(client=client).fetch(source)

        assert len(raws) == 5

    def test_anywhere_is_worldwide_with_structured_pay(self, jobicy_source: SourceConfig) -> None:
        job = _normalize(JobicyAdapter(), jobicy_source, _jobicy_job(150846))

        assert job.location_eligibility == ["worldwide"]
        assert (job.salary_min, job.salary_max, job.salary_currency) == (150000, 250000, "USD")
        assert job.source_key == "jobicy:sticker-mule:150846"
        assert job.company == "Sticker Mule"
        assert job.remote is True and job.workplace_type == "remote"
        assert job.employment_type == "full_time"
        assert job.posted_at is not None and job.posted_at.tzinfo is not None
        assert job.updated_at is None

    def test_apply_url_is_jobicys_own_url(self, jobicy_source: SourceConfig) -> None:
        job = _normalize(JobicyAdapter(), jobicy_source, _jobicy_job(150846))

        assert job.apply_url.startswith("https://jobicy.com/jobs/")
        assert job.ats == "jobicy"

    def test_a_us_pay_sentence_does_not_narrow_anywhere(
        self, jobicy_source: SourceConfig
    ) -> None:
        """Canonical hires globally; "compensation for US based candidates" is pay."""
        payload = _jobicy_job(149527)
        assert "US based candidates" in payload["jobDescription"]

        job = _normalize(JobicyAdapter(), jobicy_source, payload)

        assert job.location_eligibility == ["worldwide"]

    def test_apac_includes_india(self, jobicy_source: SourceConfig) -> None:
        job = _normalize(JobicyAdapter(), jobicy_source, _jobicy_job(153014))

        assert "IN" in job.location_eligibility
        assert _location_passes(job.location_eligibility)

    def test_double_spaced_geo_list_resolves_each_region(
        self, jobicy_source: SourceConfig
    ) -> None:
        job = _normalize(JobicyAdapter(), jobicy_source, _jobicy_job(145995))  # "APAC,  China"

        assert {"IN", "CN"} <= set(job.location_eligibility)

    def test_a_single_non_indian_country_fails_the_location_rule(
        self, jobicy_source: SourceConfig
    ) -> None:
        job = _normalize(JobicyAdapter(), jobicy_source, _jobicy_job(145996))  # "Japan"

        assert job.location_eligibility == ["JP"]
        assert not _location_passes(job.location_eligibility)


# ------------------------------------------------------------------ The Muse
def _muse_pages(request: httpx.Request) -> httpx.Response:
    page = request.url.params["page"]
    return httpx.Response(200, json=load_fixture(f"themuse_jobs_page{page}.json"))


class TestTheMuseAdapter:
    @respx.mock
    async def test_fetch_pages_to_page_count_and_dedupes_repeats(
        self, muse_source: SourceConfig
    ) -> None:
        route = respx.get(MUSE_URL).mock(side_effect=_muse_pages)

        async with httpx.AsyncClient() as client:
            adapter = TheMuseAdapter(client=client)
            raws = await adapter.fetch(muse_source)

        # Page 1 repeats a page-0 id: pages are not stable, ids are.
        assert len(raws) == 4
        assert route.call_count == 2
        assert [c.request.url.params["page"] for c in route.calls] == ["0", "1"]
        assert adapter.fetch_truncated is False

    @respx.mock
    async def test_list_params_are_sent_as_repeated_keys(
        self, muse_source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty env var beats a real key in the developer's `.env` file.
        monkeypatch.setenv("THEMUSE_API_KEY", "")
        get_settings.cache_clear()
        route = respx.get(MUSE_URL).mock(side_effect=_muse_pages)
        try:
            async with httpx.AsyncClient() as client:
                await TheMuseAdapter(client=client).fetch(muse_source)
        finally:
            get_settings.cache_clear()

        params = route.calls[0].request.url.params
        assert params.get_list("location") == ["Bangalore, India", "Hyderabad, India"]
        assert params.get_list("level") == ["Mid Level", "Senior Level"]
        assert "api_key" not in params

    @respx.mock
    async def test_api_key_is_sent_when_configured(
        self, muse_source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("THEMUSE_API_KEY", "k123")
        get_settings.cache_clear()
        route = respx.get(MUSE_URL).mock(side_effect=_muse_pages)
        try:
            async with httpx.AsyncClient() as client:
                await TheMuseAdapter(client=client).fetch(muse_source)
        finally:
            get_settings.cache_clear()

        assert route.calls[0].request.url.params["api_key"] == "k123"

    @respx.mock
    async def test_the_page_cap_stops_paging_and_marks_the_fetch_truncated(
        self, muse_source: SourceConfig
    ) -> None:
        # Verified live: `page=100` answers 400 even when page_count is 5,080.
        page = load_fixture("themuse_jobs_page0.json")

        def endless(request: httpx.Request) -> httpx.Response:
            body = copy.deepcopy(page)
            body["page_count"] = 500
            for i, job in enumerate(body["results"]):
                job["id"] = int(request.url.params["page"]) * 10 + i
            return httpx.Response(200, json=body)

        route = respx.get(MUSE_URL).mock(side_effect=endless)

        async with httpx.AsyncClient() as client:
            adapter = TheMuseAdapter(client=client)
            await adapter.fetch(muse_source)

        assert route.call_count == MAX_PAGE + 1
        assert adapter.fetch_truncated is True

    @respx.mock
    async def test_a_failing_deep_page_keeps_results_but_marks_truncated(
        self, muse_source: SourceConfig, db: None  # noqa: ARG002 - retries off
    ) -> None:
        respx.get(MUSE_URL).mock(
            side_effect=lambda request: (
                _muse_pages(request)
                if request.url.params["page"] == "0"
                else httpx.Response(503, text="unavailable")
            )
        )

        async with httpx.AsyncClient() as client:
            adapter = TheMuseAdapter(client=client)
            raws = await adapter.fetch(muse_source)

        assert len(raws) == 3
        assert adapter.fetch_truncated is True

    @respx.mock
    async def test_a_failure_on_the_first_page_still_raises(
        self, muse_source: SourceConfig, db: None  # noqa: ARG002 - retries off
    ) -> None:
        respx.get(MUSE_URL).mock(return_value=httpx.Response(503, text="unavailable"))

        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError):
                await TheMuseAdapter(client=client).fetch(muse_source)

    @respx.mock
    async def test_a_location_no_row_carries_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Verified live: `Bengaluru, India` silently returns the remote set.
        source = SourceConfig(
            company="The Muse",
            ats="themuse",
            queries=[{"category": "Software Engineering", "location": ["Bengaluru, India"]}],
        )
        respx.get(MUSE_URL).mock(side_effect=_muse_pages)

        with caplog.at_level(logging.WARNING, logger="app.adapters.themuse"):
            async with httpx.AsyncClient() as client:
                await TheMuseAdapter(client=client).fetch(source)

        flagged = [r for r in caplog.records if r.getMessage() == "themuse.location_not_matched"]
        assert [r.location for r in flagged] == ["Bengaluru, India"]

    def test_remote_only_is_not_read_as_worldwide(self, muse_source: SourceConfig) -> None:
        job = _normalize(TheMuseAdapter(), muse_source, _muse_job(22029789))

        assert job.locations == ["Flexible / Remote"]
        assert job.location_eligibility == [], "not worldwide, and nothing invented by enrichment"
        assert job.remote is True and job.workplace_type == "remote"
        assert not _location_passes(job.location_eligibility)

    def test_remote_with_a_us_office_resolves_to_the_office(
        self, muse_source: SourceConfig
    ) -> None:
        job = _normalize(TheMuseAdapter(), muse_source, _muse_job(21820572))

        assert job.location_eligibility == ["US"]
        assert job.remote is True

    def test_an_india_office_role_is_eligible_but_onsite(self, muse_source: SourceConfig) -> None:
        job = _normalize(TheMuseAdapter(), muse_source, _muse_job(22159863))

        assert job.location_eligibility == ["IN"]
        assert _location_passes(job.location_eligibility)
        assert job.remote is False
        assert job.workplace_type == "onsite", "no remote marker on this board means an office role"

    def test_maps_identity_urls_and_dates(self, muse_source: SourceConfig) -> None:
        job = _normalize(TheMuseAdapter(), muse_source, _muse_job(22053936))

        assert job.source_key == "themuse:kyndryl:22053936"
        assert job.company == "Kyndryl"
        assert job.apply_url.startswith("https://www.themuse.com/jobs/")
        assert job.ats == "themuse"
        assert "IN" in job.location_eligibility
        assert job.department == "Software Engineering"
        assert job.posted_at is not None and job.posted_at.tzinfo is not None
        assert job.salary_min is None and job.employment_type is None


# ----------------------------------------------------------------- Registry
class TestRegistry:
    def test_both_resolve_as_aggregators(self) -> None:
        assert isinstance(get_adapter("jobicy"), JobicyAdapter)
        assert isinstance(get_adapter("themuse"), TheMuseAdapter)
        assert isinstance(get_adapter("themuse.com"), TheMuseAdapter)
        assert is_aggregator("jobicy") and is_aggregator("themuse")


# ------------------------------------------------------------------- Ingest
class TestTruncatedFetchClosure:
    @respx.mock
    async def test_a_truncated_fetch_does_not_close_unreached_jobs(
        self, session_factory
    ) -> None:
        mock_all_boards()
        await run_ingest(notify=False)
        open_before = await count_jobs(session_factory, ats="greenhouse", status="open")

        class TruncatingGreenhouse(GreenhouseAdapter):
            async def fetch(self, company):
                raws = await super().fetch(company)
                self.fetch_truncated = True
                return raws

        payload = load_fixture("greenhouse_stripe.json")
        payload["jobs"].pop(0)
        mock_all_boards(greenhouse=payload)
        register_adapter(TruncatingGreenhouse)
        try:
            result = await run_ingest(notify=False)
        finally:
            register_adapter(GreenhouseAdapter)

        assert await count_jobs(session_factory, ats="greenhouse", status="open") == open_before
        greenhouse = next(s for s in result.sources if s.ats == "greenhouse")
        assert greenhouse.skipped_closure_sweep is True
        assert result.closed == 0
