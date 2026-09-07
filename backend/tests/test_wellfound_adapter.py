"""Wellfound adapter tests against recorded fixtures.

Wellfound has no public API, so the adapter goes through Firecrawl and reads
the Next.js `__NEXT_DATA__` blob (listing pages) plus schema.org JSON-LD
(detail pages). Both fixtures here are trimmed captures of real 2026-09-06
responses. CI never hits Firecrawl or Wellfound.

The case that matters most is `TestEverywhereClaim`: Wellfound renders an
unstated candidate location as "Everywhere", and one verified live listing
claimed exactly that while its own prose said "fully remotely within the
United States". Reading the structured field alone would push US-only roles
through an India-only eligibility filter.
"""

from __future__ import annotations

import json

import httpx
import time

import pytest
import respx

from app.adapters import get_adapter, is_aggregator
from app.adapters.base import AdapterError
from app.config import get_settings
from app.adapters.wellfound import (
    FIRECRAWL_SCRAPE_URL,
    WellfoundAdapter,
    extract_json_ld,
    extract_next_data,
    feed_url,
)
from app.schemas import RawJob, SourceConfig
from tests.conftest import load_fixture

# Job ids present in the listing fixture.
US_ONLY = "4322508"      # acceptedRemoteLocationNames == ["United States"]
INDIA = "4566107"        # == ["India"], no compensation string
UNSTATED = "4592896"     # == [] -> renders as "Everywhere"
WIDE_EQUITY = "4670353"  # 129 locations, compensation carries an equity tail


@pytest.fixture(autouse=True)
def _firecrawl_key(monkeypatch: pytest.MonkeyPatch):
    """`get_settings` is lru_cached, so an env override needs an explicit clear."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setenv("HTTP_MAX_RETRIES", "0")
    # Pacing exists to respect a live per-minute quota. Against mocks there is
    # no quota, so leaving it on would just make CI sleep ~4s per scrape.
    monkeypatch.setenv("FIRECRAWL_REQUESTS_PER_MINUTE", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def source() -> SourceConfig:
    return SourceConfig(
        company="Wellfound",
        ats="wellfound",
        queries=[{"role": "software-engineer", "remote": True}],
    )


def _listing_html(fixture: str = "wellfound_role_search.json") -> str:
    """Wrap the recorded Apollo payload the way the real page ships it.

    The `crossorigin` attribute is deliberate — it is present live, and an
    extractor that assumes `type="application/json">` closes the tag silently
    finds nothing.
    """
    payload = json.dumps(load_fixture(fixture))
    return (
        '<html><body><div id="__next"></div>'
        f'<script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">'
        f"{payload}</script></body></html>"
    )


def _detail_html() -> str:
    ld = json.dumps(load_fixture("wellfound_job_ld.json"))
    return f'<html><head><script type="application/ld+json">{ld}</script></head></html>'


def _fc(rawHtml: str = "", markdown: str = "") -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "data": {"rawHtml": rawHtml, "markdown": markdown}}
    )


def _router(
    *,
    detail: httpx.Response | None = None,
    listing_html: str | None = None,
):
    """Answer by the URL Firecrawl was asked to scrape, as the real API does.

    A flat response list breaks the moment stage 2 fires, because the number of
    detail fetches depends on the fixture's eligibility mix.
    """
    page1 = _fc(listing_html if listing_html is not None else _listing_html())
    empty = _fc(_listing_html("wellfound_role_search_page2.json"))
    detail_response = detail if detail is not None else _fc(_detail_html())

    def handler(request: httpx.Request) -> httpx.Response:
        target = json.loads(request.content)["url"]
        if "/jobs/" in target:
            return detail_response
        return empty if "page=" in target else page1

    return handler


async def _fetch(source: SourceConfig, handler) -> list[RawJob]:
    respx.post(FIRECRAWL_SCRAPE_URL).mock(side_effect=handler)
    async with httpx.AsyncClient() as client:
        return await WellfoundAdapter(client=client).fetch(source)


def _by_id(raws: list[RawJob], job_id: str) -> RawJob:
    return next(r for r in raws if r.native_id == job_id)


# ------------------------------------------------------------------ URL forms
class TestFeedUrl:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ({"role": "software-engineer", "remote": True}, "https://wellfound.com/role/r/software-engineer"),
            ({"role": "software-engineer"}, "https://wellfound.com/role/software-engineer"),
            ({"role": "software-engineer", "location": "india"}, "https://wellfound.com/role/l/software-engineer/india"),
            ({"location": "india"}, "https://wellfound.com/location/india"),
        ],
    )
    def test_builds_only_robots_allowed_paths(self, query: dict, expected: str) -> None:
        assert feed_url(query) == expected

    def test_an_empty_query_is_a_config_error(self) -> None:
        with pytest.raises(AdapterError):
            feed_url({})


# ------------------------------------------------------------------ Extraction
class TestExtraction:
    def test_next_data_survives_the_crossorigin_attribute(self) -> None:
        assert extract_next_data(_listing_html()) is not None

    def test_missing_blob_returns_none_rather_than_raising(self) -> None:
        assert extract_next_data("<html><body>Security Check</body></html>") is None

    def test_json_ld_picks_the_job_posting(self) -> None:
        assert extract_json_ld(_detail_html())["@type"] == "JobPosting"


# --------------------------------------------------------------------- Fetch
class TestPacing:
    """Firecrawl caps requests per minute and sends no `Retry-After`, so the
    adapter has to space its own calls rather than discover the limit."""

    async def test_calls_are_spaced_to_the_configured_rate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIRECRAWL_REQUESTS_PER_MINUTE", "600")  # 0.1s apart
        get_settings.cache_clear()

        adapter = WellfoundAdapter()
        start = time.monotonic()
        await adapter._pace()
        await adapter._pace()
        await adapter._pace()
        elapsed = time.monotonic() - start

        # First call is free; the next two each wait one interval.
        assert elapsed >= 0.2
        get_settings.cache_clear()

    async def test_pacing_can_be_switched_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FIRECRAWL_REQUESTS_PER_MINUTE", "0")
        get_settings.cache_clear()

        adapter = WellfoundAdapter()
        start = time.monotonic()
        for _ in range(5):
            await adapter._pace()

        assert time.monotonic() - start < 0.1
        get_settings.cache_clear()


class TestFetch:
    @respx.mock
    async def test_pages_until_a_page_yields_no_listings(self, source: SourceConfig) -> None:
        raws = await _fetch(source, _router())
        assert {r.native_id for r in raws} == {US_ONLY, INDIA, UNSTATED, WIDE_EQUITY}

    @respx.mock
    async def test_the_same_job_in_two_feeds_is_one_job(self, source: SourceConfig) -> None:
        two_feeds = SourceConfig(
            company="Wellfound",
            ats="wellfound",
            queries=[{"role": "software-engineer", "remote": True}, {"location": "india"}],
        )
        raws = await _fetch(two_feeds, _router())
        assert len(raws) == len({r.native_id for r in raws}) == 4

    @respx.mock
    async def test_a_challenged_first_page_raises(self, source: SourceConfig) -> None:
        respx.post(FIRECRAWL_SCRAPE_URL).mock(
            return_value=_fc("<html><body>Security Check</body></html>")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="no __NEXT_DATA__"):
                await WellfoundAdapter(client=client).fetch(source)

    @respx.mock
    async def test_a_missing_key_is_a_clear_error(
        self, source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.setenv("FIRECRAWL_API_KEY", "")
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="FIRECRAWL_API_KEY"):
                await WellfoundAdapter(client=client).fetch(source)


# --------------------------------------------------------------- Cost control
class TestDetailBudget:
    def test_a_decided_job_needs_no_detail_fetch(self) -> None:
        adapter = WellfoundAdapter()
        # US-only with a USD figure: location already fails, pay cannot save it.
        assert adapter._needs_detail(
            {"acceptedRemoteLocationNames": ["United States"], "compensation": "$150k – $185k"}
        ) is False

    def test_an_india_job_without_readable_pay_is_worth_a_lookup(self) -> None:
        assert WellfoundAdapter()._needs_detail(
            {"acceptedRemoteLocationNames": ["India"], "compensation": ""}
        ) is True

    def test_an_unstated_location_is_always_worth_a_lookup(self) -> None:
        assert WellfoundAdapter()._needs_detail(
            {"acceptedRemoteLocationNames": [], "compensation": "$170k – $190k"}
        ) is True

    @respx.mock
    async def test_verification_can_be_switched_off(
        self, source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WELLFOUND_VERIFY_DETAILS", "false")
        route = respx.post(FIRECRAWL_SCRAPE_URL).mock(side_effect=_router())
        async with httpx.AsyncClient() as client:
            await WellfoundAdapter(client=client).fetch(source)
        assert route.call_count == 2  # two listing pages, zero detail pages


# ------------------------------------------------------ The "Everywhere" claim
class TestEverywhereClaim:
    """An unstated candidate location renders as "Everywhere" — verify it."""

    @respx.mock
    async def test_prose_restriction_overrides_the_worldwide_claim(
        self, source: SourceConfig
    ) -> None:
        monkey_markdown = (
            "# Senior Software Engineer\n\n- Remote (Everywhere)\n\n"
            "Remote Work Policy\n\nThis role may be performed fully remotely "
            "within the United States. Please note that our US headquarters "
            "are located in NYC.\n"
        )
        raws = await _fetch(source, _router(detail=_fc(_detail_html(), monkey_markdown)))
        posting = WellfoundAdapter().normalize(_by_id(raws, UNSTATED), source)

        assert posting.location_eligibility == ["US"]
        assert "worldwide" not in posting.location_eligibility
        assert any("restricts candidates to United States" in r for r in posting.eligibility_reasons)

    @respx.mock
    async def test_silence_in_the_prose_leaves_it_worldwide_but_flagged(
        self, source: SourceConfig
    ) -> None:
        markdown = "# Senior Software Engineer\n\n- Remote (Everywhere)\n\nWe hire globally.\n"
        raws = await _fetch(source, _router(detail=_fc(_detail_html(), markdown)))
        posting = WellfoundAdapter().normalize(_by_id(raws, UNSTATED), source)

        assert posting.location_eligibility == ["worldwide"]
        assert posting.eligibility_reasons  # never silently "just worldwide"

    @respx.mock
    async def test_a_failed_detail_fetch_keeps_the_job(self, source: SourceConfig) -> None:
        raws = await _fetch(source, _router(detail=httpx.Response(500)))
        assert len(raws) == 4  # degrades to stage-1 data rather than dropping


# ----------------------------------------------------------------- Normalize
class TestNormalize:
    @respx.mock
    @respx.mock
    async def test_country_names_become_iso_codes(self, source: SourceConfig) -> None:
        raws = await _fetch(source, _router())
        adapter = WellfoundAdapter()

        assert adapter.normalize(_by_id(raws, US_ONLY), source).location_eligibility == ["US"]
        assert "IN" in adapter.normalize(_by_id(raws, INDIA), source).location_eligibility

    @respx.mock
    async def test_identity_and_apply_url_use_the_native_job_id(
        self, source: SourceConfig
    ) -> None:
        raws = await _fetch(source, _router())
        posting = WellfoundAdapter().normalize(_by_id(raws, US_ONLY), source)

        assert posting.source_key.startswith("wellfound:")
        assert posting.source_key.endswith(f":{US_ONLY}")
        assert posting.apply_url.startswith(f"https://wellfound.com/jobs/{US_ONLY}-")

    @respx.mock
    async def test_the_employer_not_the_board_is_the_company(
        self, source: SourceConfig
    ) -> None:
        raws = await _fetch(source, _router())
        posting = WellfoundAdapter().normalize(_by_id(raws, US_ONLY), source)
        assert posting.company not in {"Wellfound", ""}

    def test_equity_after_the_bullet_is_not_read_as_pay(self) -> None:
        """'$80k – $90k • 5.0% – 10.0%' must not parse as 5–10."""
        raw = RawJob(
            native_id=WIDE_EQUITY,
            payload={
                "id": WIDE_EQUITY,
                "title": "Software Engineer",
                "slug": "software-engineer",
                "compensation": "$80k – $90k • 5.0% – 10.0%",
                "acceptedRemoteLocationNames": ["India"],
            },
        )
        posting = WellfoundAdapter().normalize(raw, SourceConfig(company="Wellfound", ats="wellfound"))
        assert (posting.salary_min, posting.salary_max) == (80000.0, 90000.0)
        assert posting.salary_currency == "USD"

    def test_structured_json_ld_pay_beats_the_display_string(self) -> None:
        raw = RawJob(
            native_id=US_ONLY,
            payload={
                "id": US_ONLY,
                "title": "Senior Software Engineer",
                "slug": "senior-software-engineer",
                "compensation": "$1k – $2k",  # deliberately wrong
                "acceptedRemoteLocationNames": ["India"],
                "_json_ld": load_fixture("wellfound_job_ld.json"),
            },
        )
        posting = WellfoundAdapter().normalize(raw, SourceConfig(company="Wellfound", ats="wellfound"))
        assert (posting.salary_min, posting.salary_max, posting.salary_currency) == (
            168000.0,
            185000.0,
            "USD",
        )

    def test_is_us_employer_comes_from_the_hiring_org_address(self) -> None:
        raw = RawJob(
            native_id=US_ONLY,
            payload={
                "id": US_ONLY,
                "title": "Senior Software Engineer",
                "acceptedRemoteLocationNames": ["India"],
                "_json_ld": load_fixture("wellfound_job_ld.json"),
            },
        )
        posting = WellfoundAdapter().normalize(raw, SourceConfig(company="Wellfound", ats="wellfound"))
        assert posting.is_us_employer is True

    def test_is_us_employer_stays_unknown_without_a_detail_page(self) -> None:
        raw = RawJob(
            native_id=INDIA,
            payload={"id": INDIA, "title": "Software Engineer", "acceptedRemoteLocationNames": ["India"]},
        )
        posting = WellfoundAdapter().normalize(raw, SourceConfig(company="Wellfound", ats="wellfound"))
        # None, not False — the pay rule treats the two differently.
        assert posting.is_us_employer is None

    def test_json_ld_date_posted_wins_over_the_epoch_field(self) -> None:
        raw = RawJob(
            native_id=US_ONLY,
            payload={
                "id": US_ONLY,
                "title": "Senior Software Engineer",
                "liveStartAt": 1,
                "_json_ld": load_fixture("wellfound_job_ld.json"),
            },
        )
        posting = WellfoundAdapter().normalize(raw, SourceConfig(company="Wellfound", ats="wellfound"))
        assert posting.posted_at is not None
        assert posting.posted_at.year == 2026

    def test_live_start_at_is_read_as_epoch_seconds(self) -> None:
        raw = RawJob(
            native_id=US_ONLY,
            payload={"id": US_ONLY, "title": "Senior Software Engineer", "liveStartAt": 1788307200},
        )
        posting = WellfoundAdapter().normalize(raw, SourceConfig(company="Wellfound", ats="wellfound"))
        assert posting.posted_at is not None and posting.posted_at.year == 2026


class TestCompoundLocationNames:
    """Wellfound ships compound names; they must be split before resolution.

    Regression: resolving "Mumbai, Maharashtra" as one token yields nothing, so
    an India-eligible job that names a city instead of the country was being
    dropped from `location_eligibility` entirely — silently failing the one
    filter this project exists to apply.
    """

    @pytest.mark.parametrize(
        ("names", "expected"),
        [
            (["Mumbai, Maharashtra"], "IN"),
            (["Bengaluru, Karnataka"], "IN"),
            (["California, United States"], "US"),
        ],
    )
    def test_a_city_inside_a_compound_name_still_resolves(
        self, names: list[str], expected: str
    ) -> None:
        raw = RawJob(
            native_id="1",
            payload={"id": "1", "title": "Engineer", "acceptedRemoteLocationNames": names},
        )
        posting = WellfoundAdapter().normalize(
            raw, SourceConfig(company="Wellfound", ats="wellfound")
        )
        assert expected in posting.location_eligibility

    def test_an_india_city_survives_the_detail_screen(self) -> None:
        # _needs_detail must see IN here, or the job never reaches stage 2.
        assert WellfoundAdapter()._needs_detail(
            {"acceptedRemoteLocationNames": ["Mumbai, Maharashtra"], "compensation": ""}
        ) is True


# ------------------------------------------------------------------ Registry
class TestRegistry:
    def test_resolves_through_the_registry(self) -> None:
        assert isinstance(get_adapter("wellfound"), WellfoundAdapter)

    def test_angellist_aliases_resolve(self) -> None:
        assert isinstance(get_adapter("angellist"), WellfoundAdapter)

    def test_counts_as_an_aggregator_board(self) -> None:
        # Its `company` column holds the employer, not the board name.
        assert is_aggregator("wellfound") is True

    def test_needs_no_board_token(self) -> None:
        SourceConfig(company="Wellfound", ats="wellfound", queries=[{"location": "india"}])
