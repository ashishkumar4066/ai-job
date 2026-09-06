"""Himalayas + Remotive adapter tests against recorded fixtures.

Acceptance criterion: both aggregator adapters return normalized jobs from a
recorded fixture, and the shared interface handles them with no downstream
special-casing. CI never hits a live endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.adapters import get_adapter, is_aggregator
from app.adapters.base import AdapterError
from app.adapters.himalayas import API_URL as HIMALAYAS_URL
from app.adapters.himalayas import HimalayasAdapter
from app.adapters.remotive import API_URL as REMOTIVE_URL
from app.adapters.remotive import RemotiveAdapter
from app.config import get_settings
from app.schemas import RawJob, SourceConfig
from tests.conftest import load_fixture


@pytest.fixture
def himalayas_source() -> SourceConfig:
    return SourceConfig(
        company="Himalayas",
        ats="himalayas",
        queries=[{"worldwide": "true", "q": "software engineer"}],
    )


@pytest.fixture
def remotive_source() -> SourceConfig:
    return SourceConfig(company="Remotive", ats="remotive")


def _himalayas_job(index: int) -> dict:
    return load_fixture("himalayas_search.json")["jobs"][index]


def _remotive_job(index: int) -> dict:
    return load_fixture("remotive_software_dev.json")["jobs"][index]


# ----------------------------------------------------------------- Himalayas
class TestHimalayasAdapter:
    @respx.mock
    async def test_fetch_pages_until_an_empty_page(self, himalayas_source: SourceConfig) -> None:
        # `totalCount` is unreliable, so paging stops only on an empty page.
        pages = [
            httpx.Response(200, json=load_fixture("himalayas_search.json")),
            httpx.Response(200, json=load_fixture("himalayas_search_page2.json")),
        ]
        route = respx.get(HIMALAYAS_URL).mock(side_effect=pages)

        async with httpx.AsyncClient() as client:
            raws = await HimalayasAdapter(client=client).fetch(himalayas_source)

        assert len(raws) == 5
        assert route.call_count == 2
        assert route.calls[0].request.url.params["page"] == "1"
        assert route.calls[1].request.url.params["page"] == "2"

    @respx.mock
    async def test_a_failing_deep_page_keeps_earlier_results(
        self, himalayas_source: SourceConfig
    ) -> None:
        # Observed live: deep pages start answering 503 under load. The 503 is
        # retried first, so the responder has to keep answering.
        respx.get(HIMALAYAS_URL).mock(
            side_effect=lambda request: (
                httpx.Response(200, json=load_fixture("himalayas_search.json"))
                if request.url.params["page"] == "1"
                else httpx.Response(503, text="upstream unavailable")
            )
        )
        async with httpx.AsyncClient() as client:
            raws = await HimalayasAdapter(client=client).fetch(himalayas_source)

        assert len(raws) == 5, "page 1's jobs must survive a failure on page 2"

    @respx.mock
    async def test_a_failure_on_the_first_page_still_raises(
        self, himalayas_source: SourceConfig
    ) -> None:
        respx.get(HIMALAYAS_URL).mock(return_value=httpx.Response(503, text="down"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError):
                await HimalayasAdapter(client=client).fetch(himalayas_source)

    @respx.mock
    async def test_rate_limits_are_retried_with_backoff(
        self, himalayas_source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_MAX_RETRIES", "2")
        monkeypatch.setenv("RATE_LIMIT_BASE_DELAY_SECONDS", "0.01")
        get_settings.cache_clear()

        route = respx.get(HIMALAYAS_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json=load_fixture("himalayas_search.json")),
                httpx.Response(200, json=load_fixture("himalayas_search_page2.json")),
            ]
        )
        async with httpx.AsyncClient() as client:
            raws = await HimalayasAdapter(client=client).fetch(himalayas_source)

        assert len(raws) == 5
        assert route.call_count == 3, "the 429 was retried, not surfaced as an error"
        get_settings.cache_clear()

    @respx.mock
    async def test_fetch_merges_queries_and_dedupes(self) -> None:
        """A job answering two queries in one sweep is still one job."""
        source = SourceConfig(
            company="Himalayas",
            ats="himalayas",
            queries=[{"worldwide": "true"}, {"country": "India"}],
        )
        respx.get(HIMALAYAS_URL).mock(
            side_effect=lambda request: httpx.Response(
                200,
                json=load_fixture("himalayas_search.json")
                if request.url.params["page"] == "1"
                else load_fixture("himalayas_search_page2.json"),
            )
        )

        async with httpx.AsyncClient() as client:
            raws = await HimalayasAdapter(client=client).fetch(source)

        assert len(raws) == 5, "the same 5 jobs came back for both queries"

    def test_native_id_comes_from_the_guid_slug(self, himalayas_source: SourceConfig) -> None:
        # There is no `id` field on this API; identity is the URL slug.
        payload = _himalayas_job(0)
        assert "id" not in payload

        adapter = HimalayasAdapter()
        raws_id = payload["guid"].rstrip("/").rsplit("/", 1)[-1]
        job = adapter.normalize(RawJob(native_id=raws_id, payload=payload), himalayas_source)

        assert job.source_key == f"himalayas:aline:{raws_id}"
        assert job.ats == "himalayas"
        # The employer, not the board, is the company.
        assert job.company == "Aline"

    def test_normalize_maps_structured_eligibility_and_salary(
        self, himalayas_source: SourceConfig
    ) -> None:
        job = HimalayasAdapter().normalize(
            RawJob(native_id="x", payload=_himalayas_job(0)), himalayas_source
        )

        # Empty `locationRestrictions` is this board's "worldwide".
        assert _himalayas_job(0)["locationRestrictions"] == []
        assert job.location_eligibility == ["worldwide"]
        assert job.locations == ["Worldwide"]
        assert job.remote is True
        assert job.salary_min == 100000
        assert job.salary_currency == "USD"
        assert job.is_us_employer is None, "the feed carries no employer HQ"
        assert job.posted_at is not None and job.posted_at.tzinfo == UTC

    def test_normalize_maps_country_names_to_iso_codes(
        self, himalayas_source: SourceConfig
    ) -> None:
        us_job = HimalayasAdapter().normalize(
            RawJob(native_id="x", payload=_himalayas_job(1)), himalayas_source
        )
        india_job = HimalayasAdapter().normalize(
            RawJob(native_id="y", payload=_himalayas_job(2)), himalayas_source
        )

        assert us_job.location_eligibility == ["US"]
        assert india_job.location_eligibility == ["IN"]

    def test_numeric_utc_offsets_become_readable_strings(
        self, himalayas_source: SourceConfig
    ) -> None:
        # India's +5:30 is why these are floats, not ints.
        india_job = HimalayasAdapter().normalize(
            RawJob(native_id="y", payload=_himalayas_job(2)), himalayas_source
        )
        assert india_job.timezone_restrictions == ["UTC+05:30"]

    def test_non_usd_currency_is_preserved_verbatim(
        self, himalayas_source: SourceConfig
    ) -> None:
        job = HimalayasAdapter().normalize(
            RawJob(native_id="z", payload=_himalayas_job(3)), himalayas_source
        )
        assert job.salary_currency == "PLN"
        assert job.location_eligibility == ["PL"]


# ------------------------------------------------------------------ Remotive
class TestRemotiveAdapter:
    @respx.mock
    async def test_fetch_returns_every_posting(self, remotive_source: SourceConfig) -> None:
        route = respx.get(REMOTIVE_URL).mock(
            return_value=httpx.Response(200, json=load_fixture("remotive_software_dev.json"))
        )
        async with httpx.AsyncClient() as client:
            raws = await RemotiveAdapter(client=client).fetch(remotive_source)

        assert len(raws) == 6
        # The documented category param is sent even though the live API
        # currently ignores it.
        assert route.calls[0].request.url.params["category"] == "software-dev"

    def test_apply_url_is_remotives_own_url(self, remotive_source: SourceConfig) -> None:
        """Required by Remotive's terms: link back to Remotive, not the employer."""
        payload = _remotive_job(0)
        job = RemotiveAdapter().normalize(
            RawJob(native_id=str(payload["id"]), payload=payload), remotive_source
        )

        assert job.apply_url == payload["url"]
        assert job.apply_url.startswith("https://remotive.com/")
        # `ats` carries the attribution the dashboard and alerts display.
        assert job.ats == "remotive"

    def test_worldwide_free_text_resolves_to_the_sentinel(
        self, remotive_source: SourceConfig
    ) -> None:
        payload = _remotive_job(0)
        assert payload["candidate_required_location"] == "Worldwide"

        job = RemotiveAdapter().normalize(
            RawJob(native_id="1", payload=payload), remotive_source
        )
        assert job.location_eligibility == ["worldwide"]
        assert job.salary_min == 150000
        assert job.salary_max == 230000
        assert job.salary_currency == "USD"

    def test_us_only_posting_resolves_to_us_and_keeps_timezone_hint(
        self, remotive_source: SourceConfig
    ) -> None:
        payload = _remotive_job(1)
        assert payload["candidate_required_location"] == "USA, Canada, USA timezones"

        job = RemotiveAdapter().normalize(
            RawJob(native_id="2", payload=payload), remotive_source
        )
        assert job.location_eligibility == ["US", "CA"]
        assert job.timezone_restrictions == ["USA timezones"]

    def test_region_tokens_expand_to_member_countries(
        self, remotive_source: SourceConfig
    ) -> None:
        payload = _remotive_job(2)
        assert payload["candidate_required_location"] == (
            "Americas, Europe, Asia, Africa, Oceania"
        )

        job = RemotiveAdapter().normalize(
            RawJob(native_id="3", payload=payload), remotive_source
        )
        # "Asia" contains India, which is the membership the filter keys off.
        assert "IN" in job.location_eligibility
        assert "US" in job.location_eligibility

    def test_parenthetical_timezone_is_split_off_the_country(
        self, remotive_source: SourceConfig
    ) -> None:
        payload = _remotive_job(3)
        assert payload["candidate_required_location"] == "USA, CST (UTC-6)"

        job = RemotiveAdapter().normalize(
            RawJob(native_id="4", payload=payload), remotive_source
        )
        assert job.location_eligibility == ["US"]
        assert job.timezone_restrictions == ["CST", "UTC-6"]

    def test_naive_publication_date_is_read_as_utc(
        self, remotive_source: SourceConfig
    ) -> None:
        job = RemotiveAdapter().normalize(
            RawJob(native_id="1", payload=_remotive_job(0)), remotive_source
        )
        assert job.posted_at is not None
        assert job.posted_at.tzinfo == UTC
        assert job.updated_at is None, "Remotive exposes no update stamp"


# ------------------------------------------------------- The shared interface
class TestSharedInterface:
    """Both families must be indistinguishable to everything downstream."""

    def test_both_resolve_through_the_registry(self) -> None:
        assert isinstance(get_adapter("himalayas"), HimalayasAdapter)
        assert isinstance(get_adapter("remotive"), RemotiveAdapter)
        assert is_aggregator("himalayas") and is_aggregator("remotive")
        assert not is_aggregator("greenhouse")

    def test_aggregator_configs_need_no_board_token(self) -> None:
        assert SourceConfig(company="Remotive", ats="remotive").token_or_slug == ""

    @pytest.mark.parametrize(
        ("adapter", "source", "payload"),
        [
            (
                HimalayasAdapter(),
                SourceConfig(company="Himalayas", ats="himalayas"),
                _himalayas_job(0),
            ),
            (
                RemotiveAdapter(),
                SourceConfig(company="Remotive", ats="remotive"),
                _remotive_job(0),
            ),
        ],
    )
    def test_normalize_produces_the_shared_schema(self, adapter, source, payload) -> None:
        job = adapter.normalize(RawJob(native_id="n1", payload=payload), source)

        # Exactly the contract `ingest` relies on, identical for both families.
        assert job.source_key.startswith(f"{adapter.ats}:")
        assert job.title and job.company and job.apply_url
        assert isinstance(job.location_eligibility, list)
        assert isinstance(job.timezone_restrictions, list)
        assert isinstance(job.raw_json, dict)
        assert job.eligibility_pass is False, "the filter stage sets this, not adapters"

    def test_normalize_all_leaves_aggregator_eligibility_untouched(self) -> None:
        """Base-class enrichment fills blanks only; it must not override a feed."""
        source = SourceConfig(
            company="Himalayas", ats="himalayas", us_employer=True,
            location_eligibility=["worldwide"],
        )
        jobs = HimalayasAdapter().normalize_all(
            [RawJob(native_id="n", payload=_himalayas_job(1))], source
        )

        assert jobs[0].location_eligibility == ["US"], "the feed's answer wins"
        assert jobs[0].is_us_employer is True, "but a blank is still filled from config"
