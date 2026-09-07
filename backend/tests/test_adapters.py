"""Adapter tests against recorded fixtures. CI never hits a live endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.adapters import get_adapter, is_supported_ats, supported_ats
from app.adapters.ashby import API_URL as ASHBY_URL
from app.adapters.ashby import AshbyAdapter
from app.adapters.base import AdapterError
from app.adapters.greenhouse import API_URL as GH_URL
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.lever import API_URL as LEVER_URL
from app.adapters.lever import LeverAdapter
from app.adapters.playwright_adapter import PlaywrightAdapter
from app.schemas import CompanyConfig, RawJob
from tests.conftest import load_fixture


# ---------------------------------------------------------------- Greenhouse
class TestGreenhouseAdapter:
    @respx.mock
    async def test_fetch_returns_every_posting(self, greenhouse_company: CompanyConfig) -> None:
        respx.get(GH_URL.format(token="stripe")).mock(
            return_value=httpx.Response(200, json=load_fixture("greenhouse_stripe.json"))
        )
        async with httpx.AsyncClient() as client:
            raws = await GreenhouseAdapter(client=client).fetch(greenhouse_company)

        assert len(raws) == 3
        assert all(isinstance(r, RawJob) for r in raws)
        assert raws[0].native_id == "7954688"

    def test_normalize_unescapes_double_encoded_description(
        self, greenhouse_company: CompanyConfig
    ) -> None:
        payload = load_fixture("greenhouse_stripe.json")["jobs"][0]
        job = GreenhouseAdapter().normalize(RawJob(native_id="7954688", payload=payload), greenhouse_company)

        # The raw payload is entity-escaped; the stored HTML must not be.
        assert "&lt;" in payload["content"]
        assert job.description_html.startswith("<h2>")
        assert "&lt;" not in job.description_html
        assert "Who we are" in job.description_text

    def test_normalize_maps_core_fields(self, greenhouse_company: CompanyConfig) -> None:
        payload = load_fixture("greenhouse_stripe.json")["jobs"][0]
        job = GreenhouseAdapter().normalize(RawJob(native_id="7954688", payload=payload), greenhouse_company)

        assert job.source_key == "greenhouse:stripe:7954688"
        assert job.ats == "greenhouse"
        assert job.company == "Stripe"
        assert job.title == "Account Executive, AI Sales (Grower)"
        assert job.apply_url.startswith("https://")
        assert job.department == "1654 Account Executives (AI)"
        assert job.locations[0] == "San Francisco, CA"
        assert job.remote is False
        # first_published 2026-06-02T08:58:57-04:00 -> UTC
        assert job.posted_at == datetime(2026, 6, 2, 12, 58, 57, tzinfo=UTC)
        assert job.raw_json == payload

    def test_normalize_flags_remote_from_location_string(
        self, greenhouse_company: CompanyConfig
    ) -> None:
        # Live payload location: "US-Remote, US-San Francisco, ..."
        payload = load_fixture("greenhouse_stripe.json")["jobs"][2]
        job = GreenhouseAdapter().normalize(RawJob(native_id=str(payload["id"]), payload=payload), greenhouse_company)
        assert job.remote is True

    def test_normalize_tolerates_missing_optional_fields(
        self, greenhouse_company: CompanyConfig
    ) -> None:
        # These APIs are unversioned: only id + title are load-bearing.
        raw = RawJob(native_id="1", payload={"id": 1, "title": "Minimal Role"})
        job = GreenhouseAdapter().normalize(raw, greenhouse_company)
        assert job.locations == []
        assert job.department is None
        assert job.description_html is None
        assert job.apply_url == "https://boards.greenhouse.io/stripe/jobs/1"

    @respx.mock
    async def test_fetch_raises_on_unexpected_payload(
        self, greenhouse_company: CompanyConfig
    ) -> None:
        respx.get(GH_URL.format(token="stripe")).mock(
            return_value=httpx.Response(200, json=["not", "an", "envelope"])
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError):
                await GreenhouseAdapter(client=client).fetch(greenhouse_company)

    @respx.mock
    async def test_fetch_raises_on_404(self, greenhouse_company: CompanyConfig) -> None:
        respx.get(GH_URL.format(token="stripe")).mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="404"):
                await GreenhouseAdapter(client=client).fetch(greenhouse_company)


# --------------------------------------------------------------------- Lever
class TestLeverAdapter:
    @respx.mock
    async def test_fetch_handles_bare_array(self, lever_company: CompanyConfig) -> None:
        respx.get(LEVER_URL.format(slug="palantir")).mock(
            return_value=httpx.Response(200, json=load_fixture("lever_palantir.json"))
        )
        async with httpx.AsyncClient() as client:
            raws = await LeverAdapter(client=client).fetch(lever_company)
        assert len(raws) == 3

    def test_normalize_maps_core_fields(self, lever_company: CompanyConfig) -> None:
        payload = load_fixture("lever_palantir.json")[0]
        job = LeverAdapter().normalize(RawJob(native_id=payload["id"], payload=payload), lever_company)

        assert job.source_key == f"lever:palantir:{payload['id']}"
        assert job.title == "Administrative Business Partner"
        assert job.department == "Administrative"
        assert job.locations == ["London, United Kingdom"]
        assert job.apply_url == payload["hostedUrl"]
        # Lever exposes no update timestamp at all.
        assert job.updated_at is None

    def test_normalize_parses_epoch_millis_created_at(self, lever_company: CompanyConfig) -> None:
        payload = load_fixture("lever_palantir.json")[0]
        job = LeverAdapter().normalize(RawJob(native_id=payload["id"], payload=payload), lever_company)
        assert job.posted_at == datetime(2024, 3, 25, 21, 50, 16, 463000, tzinfo=UTC)

    def test_normalize_composes_full_description(self, lever_company: CompanyConfig) -> None:
        # The JD is split across description / lists / additional.
        payload = load_fixture("lever_palantir.json")[0]
        job = LeverAdapter().normalize(RawJob(native_id=payload["id"], payload=payload), lever_company)

        assert len(job.description_html) > len(payload["description"])
        assert "<ul>" in job.description_html
        assert len(job.description_text) > 0

    def test_normalize_honours_workplace_type(self, lever_company: CompanyConfig) -> None:
        by_type = {
            j["workplaceType"]: LeverAdapter().normalize(
                RawJob(native_id=j["id"], payload=j), lever_company
            )
            for j in load_fixture("lever_palantir.json")
        }
        assert by_type["remote"].remote is True
        assert by_type["hybrid"].remote is False
        assert by_type["onsite"].remote is False

    @respx.mock
    async def test_fetch_rejects_envelope_shape(self, lever_company: CompanyConfig) -> None:
        respx.get(LEVER_URL.format(slug="palantir")).mock(
            return_value=httpx.Response(200, json={"jobs": []})
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="expected list"):
                await LeverAdapter(client=client).fetch(lever_company)

    def test_empty_result_is_trustworthy(self) -> None:
        # Lever 404s on an unknown slug, so [] genuinely means "no jobs".
        assert LeverAdapter.empty_result_is_suspicious is False


# --------------------------------------------------------------------- Ashby
class TestAshbyAdapter:
    @respx.mock
    async def test_fetch_reads_envelope(self, ashby_company: CompanyConfig) -> None:
        respx.get(ASHBY_URL.format(slug="linear")).mock(
            return_value=httpx.Response(200, json=load_fixture("ashby_linear.json"))
        )
        async with httpx.AsyncClient() as client:
            raws = await AshbyAdapter(client=client).fetch(ashby_company)
        assert len(raws) == 3

    @respx.mock
    async def test_fetch_skips_unlisted_postings(self, ashby_company: CompanyConfig) -> None:
        payload = load_fixture("ashby_linear.json")
        payload["jobs"][0]["isListed"] = False
        respx.get(ASHBY_URL.format(slug="linear")).mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with httpx.AsyncClient() as client:
            raws = await AshbyAdapter(client=client).fetch(ashby_company)
        assert len(raws) == 2

    def test_normalize_maps_core_fields(self, ashby_company: CompanyConfig) -> None:
        payload = load_fixture("ashby_linear.json")["jobs"][0]
        job = AshbyAdapter().normalize(RawJob(native_id=payload["id"], payload=payload), ashby_company)

        assert job.source_key == f"ashby:linear:{payload['id']}"
        assert job.title == "Senior / Staff Fullstack Engineer"
        assert job.department == "Product"
        assert job.remote is True
        assert job.apply_url == payload["jobUrl"]
        assert job.posted_at == datetime(2021, 4, 27, 20, 13, 45, 158000, tzinfo=UTC)

    def test_normalize_flattens_secondary_locations(self) -> None:
        # secondaryLocations is a list of objects, not strings.
        company = CompanyConfig(company="OpenAI", ats="ashby", token_or_slug="openai")
        payload = load_fixture("ashby_openai.json")["jobs"][0]
        job = AshbyAdapter().normalize(RawJob(native_id=payload["id"], payload=payload), company)

        assert job.locations == ["San Francisco", "New York City", "Seattle"]

    def test_hybrid_workplace_type_overrides_is_remote(self) -> None:
        company = CompanyConfig(company="OpenAI", ats="ashby", token_or_slug="openai")
        payload = load_fixture("ashby_openai.json")["jobs"][2]
        assert payload["isRemote"] is True and payload["workplaceType"] == "Hybrid"

        job = AshbyAdapter().normalize(RawJob(native_id=payload["id"], payload=payload), company)
        assert job.remote is False

    @respx.mock
    async def test_unknown_slug_returns_empty_not_404(self, ashby_company: CompanyConfig) -> None:
        """Documents the quirk the closure-sweep guard exists for."""
        respx.get(ASHBY_URL.format(slug="linear")).mock(
            return_value=httpx.Response(200, json={"jobs": [], "apiVersion": "1"})
        )
        async with httpx.AsyncClient() as client:
            raws = await AshbyAdapter(client=client).fetch(ashby_company)

        assert raws == []
        assert AshbyAdapter.empty_result_is_suspicious is True


# ------------------------------------------------------------------ Registry
class TestRegistry:
    def test_supported_ats(self) -> None:
        assert set(supported_ats()) == {
            "greenhouse",
            "lever",
            "ashby",
            "himalayas",
            "remotive",
            "wellfound",
            "playwright",
        }

    def test_aliases_resolve(self) -> None:
        assert isinstance(get_adapter("GH"), GreenhouseAdapter)
        assert isinstance(get_adapter("ashbyhq"), AshbyAdapter)
        assert is_supported_ats("Lever.co") is True

    def test_unknown_ats_raises(self) -> None:
        with pytest.raises(AdapterError, match="no adapter"):
            get_adapter("workday")

    async def test_playwright_stub_fails_loudly(self, greenhouse_company: CompanyConfig) -> None:
        # It must never quietly return [], which would close the whole board.
        with pytest.raises(AdapterError, match="stub"):
            await PlaywrightAdapter().fetch(greenhouse_company)


# ---------------------------------------------------------------- HTTP layer
class TestHttpBehaviour:
    @respx.mock
    async def test_retries_on_5xx_then_succeeds(
        self, greenhouse_company: CompanyConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTP_MAX_RETRIES", "2")
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            route = respx.get(GH_URL.format(token="stripe"))
            route.side_effect = [
                httpx.Response(503),
                httpx.Response(200, json=load_fixture("greenhouse_stripe.json")),
            ]
            async with httpx.AsyncClient() as client:
                raws = await GreenhouseAdapter(client=client).fetch(greenhouse_company)
            assert len(raws) == 3
            assert route.call_count == 2
        finally:
            get_settings.cache_clear()

    @respx.mock
    async def test_does_not_retry_4xx(self, greenhouse_company: CompanyConfig) -> None:
        route = respx.get(GH_URL.format(token="stripe")).mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError):
                await GreenhouseAdapter(client=client).fetch(greenhouse_company)
        assert route.call_count == 1

    def test_normalize_all_skips_individually_bad_rows(
        self, greenhouse_company: CompanyConfig
    ) -> None:
        raws = [
            RawJob(native_id="1", payload={"id": 1, "title": "Good"}),
            RawJob(native_id="2", payload={"id": 2}),  # missing required title
            RawJob(native_id="3", payload={"id": 3, "title": "Also good"}),
        ]
        jobs = GreenhouseAdapter().normalize_all(raws, greenhouse_company)
        assert [j.title for j in jobs] == ["Good", "Also good"]
