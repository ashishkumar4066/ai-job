"""YC jobs + Arc.dev adapter tests against recorded fixtures.

The fixtures are trimmed captures of real 2026-09-19 pages: the JSON each page
embeds, re-wrapped here the way the page ships it. CI never hits a live site.

The cases that matter most:
  * YC writes locations as ISO codes, so `"CA / Remote (CA)"` is Canada, while
    its malformed `"San Francisco, CA / Remote (US)"` is still the US.
  * A bare YC `"Remote"` is not worldwide.
  * An unknown YC role or location slug serves the default page with a 200;
    the adapter must refuse it, not store the wrong list.
  * Arc's own jobs with no country list are worldwide; its externals with no
    country list are unknown (CrowdStrike's "(Remote, DEU)" role).
  * Detail pages are read only for India/worldwide candidates.
"""

from __future__ import annotations

import copy
import html
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.adapters import get_adapter, is_aggregator
from app.adapters.arc import (
    ARC_KIND,
    CONFIDENTIAL_EMPLOYER,
    EXTERNAL_KIND,
    ArcAdapter,
    resolve_countries,
)
from app.adapters.base import AdapterError
from app.adapters.yc import (
    SITE as YC_SITE,
)
from app.adapters.yc import (
    YcAdapter,
    extract_data_page,
    parse_location,
    parse_relative_age,
    parse_salary,
    years_line,
)
from app.config import get_settings
from app.eligibility import FilterConfig, evaluate
from app.geo import WORLDWIDE
from app.schemas import JobPosting, RawJob, SourceConfig
from tests.conftest import load_fixture

# YC ids in the fixtures.
YC_MULTI = "97376"      # "US / ES / DE / GB / PL / IN / CA / Remote (US; ES; DE; GB; PL; IN; CA)"
YC_INDIA = "78821"      # "IN / Remote (IN)", INR pay, posted 2025-07-17, HQ GB
YC_CANADA = "107231"    # "CA / Remote (CA)" — Canada, not California
YC_SF = "100839"        # "San Francisco, CA / Remote (US)" — the country is dropped
YC_BARE = "78968"       # "Remote"
YC_US = "108124"        # "Remote (US)"
YC_INTERN = "110698"    # "IN / Remote (IN)", "₹25K - ₹75K INR / monthly"

# Arc keys in the fixtures.
ARC_WORLD_FT = "pkmiiyi85w"   # arcJobs, [] -> worldwide, $80-120K/yr
ARC_WORLD_HR = "pkvft6v137"   # arcJobs, [] -> worldwide, $30-45/h, in BOTH listings
ARC_US = "pkvlubg26y"         # arcJobs, ["US"]
ARC_LATAM = "pki5t0za8p"      # arcJobs, 30+ LATAM/EU codes, no IN
ARC_PY_ONLY = "p85gjg24ve"    # arcJobs, [] -> worldwide, only in the python listing
EXT_INDIA = "pl4nlujaxp"      # externalJobs, ["IN"]
EXT_EMPTY = "pl4ntso8c4"      # externalJobs, [] — CrowdStrike "(Remote, DEU)"
EXT_US = "pl4nnaomxr"         # externalJobs, ["US"]


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch):
    """Pacing is politeness toward the live sites; against mocks it only sleeps."""
    monkeypatch.setenv("YC_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("ARC_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("HTTP_MAX_RETRIES", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _passes_location(job: JobPosting) -> bool:
    verdict = evaluate(location_eligibility=job.location_eligibility, filters=FilterConfig())
    return verdict.reasons[0].startswith("location_ok")


# ======================================================================= YC
def _yc_html(page: dict, json_ld: dict | None = None) -> str:
    """The page as shipped: HTML-escaped JSON in a `data-page` attribute."""
    attr = html.escape(json.dumps(page), quote=True)
    ld = (
        f'<script type="application/ld+json">{json.dumps(json_ld)}</script>' if json_ld else ""
    )
    return (
        f"<html><head>{ld}</head><body>"
        f'<div id="WaasJobListingsPage-react-component-x" data-page="{attr}"></div>'
        "</body></html>"
    )


@pytest.fixture
def yc_source() -> SourceConfig:
    return SourceConfig(
        company="Y Combinator",
        ats="yc",
        queries=[
            {"role": "software-engineer", "location": "remote"},
            {"role": "software-engineer", "location": "india"},
        ],
    )


class _YcSite:
    """Answers listing and detail URLs from the fixtures, counting detail hits."""

    def __init__(self, remote: dict | None = None, india: dict | None = None) -> None:
        self.listings = {
            "/jobs/role/software-engineer/remote": remote or load_fixture("yc_listing_remote.json"),
            "/jobs/role/software-engineer/india": india or load_fixture("yc_listing_india.json"),
        }
        self.details = load_fixture("yc_details.json")
        self.detail_hits: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in self.listings:
            return httpx.Response(200, text=_yc_html(self.listings[path]))
        for job_id, detail in self.details.items():
            if path == detail["props"]["job"]["url"]:
                self.detail_hits.append(job_id)
                return httpx.Response(200, text=_yc_html(detail, detail["json_ld"]))
        # Unknown slugs fail OPEN live: the default list, with a 200.
        return httpx.Response(200, text=_yc_html(load_fixture("yc_listing_remote.json")))


async def _yc_fetch(source: SourceConfig, site: _YcSite) -> tuple[YcAdapter, list[RawJob]]:
    respx.get(url__startswith=YC_SITE).mock(side_effect=site)
    async with httpx.AsyncClient() as client:
        adapter = YcAdapter(client=client)
        return adapter, await adapter.fetch(source)


def _yc_postings(
    adapter: YcAdapter, raws: list[RawJob], source: SourceConfig
) -> dict[str, JobPosting]:
    return {
        raw.native_id: adapter.enrich_eligibility(adapter.normalize(raw, source), source)
        for raw in raws
    }


class TestYcParsing:
    @pytest.mark.parametrize(
        ("value", "codes", "remote"),
        [
            ("CA / Remote (CA)", ["CA"], True),
            ("San Francisco, CA / Remote (US)", ["US"], True),
            ("IN / Remote (IN)", ["IN"], True),
            ("Remote", [], True),
            ("Remote (US)", ["US"], True),
            ("Warsaw, Masovian Voivodeship, PL / Remote", ["PL"], True),
            ("Toronto, ON, CA / Remote (Toronto, ON, CA; Toronto, Ontario, CA)", ["CA"], True),
            ("San Francisco, CA, US / CA, US / OR, US", ["US"], False),
            (
                "US / ES / DE / GB / PL / IN / CA / Remote (US; ES; DE; GB; PL; IN; CA)",
                ["US", "ES", "DE", "GB", "PL", "IN", "CA"],
                True,
            ),
        ],
    )
    def test_locations_are_iso_countries_not_states(
        self, value: str, codes: list[str], remote: bool
    ) -> None:
        parsed_codes, parsed_remote, unresolved = parse_location(value)
        assert parsed_codes == codes
        assert parsed_remote is remote
        assert unresolved == []

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("$100K - $175K", (100_000.0, 175_000.0, "USD")),
            # The shared parser reads this as (1.0, None, "INR").
            ("₹1.5M - ₹3M INR", (1_500_000.0, 3_000_000.0, "INR")),
            ("$150K - $180K CAD", (150_000.0, 180_000.0, "CAD")),
            ("£50K - £120K GBP", (50_000.0, 120_000.0, "GBP")),
            ("₹25K - ₹75K INR / monthly", (25_000.0, 75_000.0, "INR")),
            ("", (None, None, None)),
        ],
    )
    def test_salary(self, value: str, expected: tuple) -> None:
        assert parse_salary(value) == expected

    def test_relative_age_floors_to_the_day(self) -> None:
        now = datetime(2026, 9, 19, 15, 30, tzinfo=UTC)
        assert parse_relative_age("18 days", now) == datetime(2026, 9, 1, tzinfo=UTC)
        assert parse_relative_age("about 1 month", now) == datetime(2026, 8, 20, tzinfo=UTC)
        assert parse_relative_age("about 1 year", now) == datetime(2025, 9, 19, tzinfo=UTC)
        assert parse_relative_age("about 5 hours", now) == datetime(2026, 9, 19, tzinfo=UTC)
        assert parse_relative_age("", now) is None

    def test_years_line_is_read_by_the_role_rule(self) -> None:
        from app.roles import years_required

        assert years_line("6+ years") == "Minimum 6+ years experience."
        assert years_required(years_line("6+ years")) == 6
        assert years_line("Any (new grads ok)") is None
        assert years_line(None) is None

    def test_extract_data_page_unescapes_the_attribute(self) -> None:
        page = load_fixture("yc_listing_remote.json")
        assert extract_data_page(_yc_html(page)) == page
        assert extract_data_page("<html></html>") is None


class TestYcAdapter:
    @respx.mock
    async def test_fetch_reads_both_listings_and_details_only_for_india_rows(
        self, yc_source: SourceConfig
    ) -> None:
        site = _YcSite()
        adapter, raws = await _yc_fetch(yc_source, site)

        assert {r.native_id for r in raws} == {
            YC_MULTI, YC_INDIA, YC_CANADA, YC_SF, YC_BARE, YC_US, YC_INTERN, "109577", "108894",
        }
        assert sorted(site.detail_hits) == sorted([YC_MULTI, YC_INDIA, YC_INTERN])
        assert adapter.fetch_truncated is False

    @respx.mock
    async def test_a_job_on_both_pages_is_one_job(self, yc_source: SourceConfig) -> None:
        remote = load_fixture("yc_listing_remote.json")
        india = copy.deepcopy(load_fixture("yc_listing_india.json"))
        india["props"]["jobPostings"].append(copy.deepcopy(remote["props"]["jobPostings"][1]))

        _, raws = await _yc_fetch(yc_source, _YcSite(india=india))

        ids = [r.native_id for r in raws]
        assert ids.count(YC_INDIA) == 1
        assert len(ids) == len(set(ids))

    @respx.mock
    async def test_normalizes_an_india_eligible_row_from_its_detail_page(
        self, yc_source: SourceConfig
    ) -> None:
        adapter, raws = await _yc_fetch(yc_source, _YcSite())
        job = _yc_postings(adapter, raws, yc_source)[YC_MULTI]

        assert job.source_key == "yc:" + job.company.lower().replace(" ", "-") + ":" + YC_MULTI
        assert "IN" in job.location_eligibility
        assert job.remote is True and job.workplace_type == "remote"
        assert (job.salary_min, job.salary_max, job.salary_currency) == (100_000, 175_000, "USD")
        # The exact JSON-LD date, not the relative "3 months".
        assert job.posted_at == datetime(2026, 6, 23, 20, 23, 34, tzinfo=UTC)
        assert job.description_html and "<" in job.description_html
        assert job.description_text.startswith("Minimum 6+ years experience.")
        # HQ country from the detail page: this one is Canadian.
        assert job.is_us_employer is False
        assert job.apply_url.startswith(YC_SITE + "/companies/")
        assert _passes_location(job)

    @respx.mock
    async def test_visa_never_reaches_the_jd_text(self, yc_source: SourceConfig) -> None:
        adapter, raws = await _yc_fetch(yc_source, _YcSite())
        for job in _yc_postings(adapter, raws, yc_source).values():
            assert job.raw_json.get("visa") is None or job.raw_json["visa"] not in (
                job.description_text or ""
            )

    @respx.mock
    async def test_old_india_row_keeps_its_real_date_and_pay(self, yc_source: SourceConfig) -> None:
        adapter, raws = await _yc_fetch(yc_source, _YcSite())
        job = _yc_postings(adapter, raws, yc_source)[YC_INDIA]

        assert job.location_eligibility == ["IN"]
        assert job.posted_at == datetime(2025, 7, 17, 9, 48, 48, tzinfo=UTC)
        assert (job.salary_min, job.salary_max, job.salary_currency) == (
            1_500_000,
            3_000_000,
            "INR",
        )
        assert job.is_us_employer is False  # HQ GB

    @respx.mock
    async def test_canada_bare_remote_and_us_rows_fail_location(
        self, yc_source: SourceConfig
    ) -> None:
        adapter, raws = await _yc_fetch(yc_source, _YcSite())
        postings = _yc_postings(adapter, raws, yc_source)

        assert postings[YC_CANADA].location_eligibility == ["CA"]
        assert postings[YC_SF].location_eligibility == ["US"]
        assert postings[YC_BARE].location_eligibility == []
        assert postings[YC_BARE].remote is True
        for job_id in (YC_CANADA, YC_SF, YC_BARE, YC_US):
            assert not _passes_location(postings[job_id]), job_id
            # No detail read, so no HQ and no JD — unknown, not guessed.
            assert postings[job_id].is_us_employer is None

    @respx.mock
    async def test_an_unknown_role_slug_is_refused_not_stored(self) -> None:
        source = SourceConfig(
            company="Y Combinator",
            ats="yc",
            queries=[{"role": "backend-engineer", "location": "remote"}],
        )
        with pytest.raises(AdapterError, match="unknown YC role"):
            await _yc_fetch(source, _YcSite())

    @respx.mock
    async def test_an_unknown_location_slug_is_refused_not_stored(self) -> None:
        source = SourceConfig(
            company="Y Combinator",
            ats="yc",
            queries=[{"role": "software-engineer", "location": "mumbai"}],
        )
        with pytest.raises(AdapterError, match="fail open"):
            await _yc_fetch(source, _YcSite())

    @respx.mock
    async def test_a_full_listing_marks_the_fetch_truncated(self, yc_source: SourceConfig) -> None:
        remote = copy.deepcopy(load_fixture("yc_listing_remote.json"))
        template = remote["props"]["jobPostings"][-1]  # "Remote (US)": no detail read
        remote["props"]["jobPostings"] = [
            {**template, "id": 900_000 + i, "url": f"/companies/x/jobs/{i}"} for i in range(40)
        ]
        adapter, _ = await _yc_fetch(yc_source, _YcSite(remote=remote))
        assert adapter.fetch_truncated is True

    @respx.mock
    async def test_a_page_without_data_page_fails_the_source(self, yc_source: SourceConfig) -> None:
        respx.get(url__startswith=YC_SITE).mock(
            return_value=httpx.Response(200, text="<html>challenge</html>")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="data-page"):
                await YcAdapter(client=client).fetch(yc_source)


# ====================================================================== Arc
def _arc_html(next_data: dict) -> str:
    return (
        '<html><body><div id="__next"></div>'
        '<script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">'
        f"{json.dumps(next_data)}</script></body></html>"
    )


@pytest.fixture
def arc_source() -> SourceConfig:
    return SourceConfig(
        company="Arc.dev",
        ats="arc",
        queries=[
            {"countries": "IN", "jobRoles": "engineering"},
            {"category": "python", "countries": "IN"},
        ],
    )


class _ArcSite:
    def __init__(self, engineering: dict | None = None) -> None:
        self.engineering = engineering or load_fixture("arc_listing_engineering.json")
        self.python = load_fixture("arc_listing_python.json")
        self.details = load_fixture("arc_details.json")
        self.detail_hits: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/remote-jobs":
            return httpx.Response(200, text=_arc_html(self.engineering))
        if path == "/remote-jobs/python":
            return httpx.Response(200, text=_arc_html(self.python))
        key = path.rsplit("-", 1)[-1]
        for fixture_key, detail in self.details.items():
            if fixture_key.split(":", 1)[1] == key:
                self.detail_hits.append(key)
                return httpx.Response(200, text=_arc_html(detail))
        return httpx.Response(404, text="not found")


async def _arc_fetch(source: SourceConfig, site: _ArcSite) -> tuple[ArcAdapter, list[RawJob]]:
    respx.get(url__startswith="https://arc.dev").mock(side_effect=site)
    async with httpx.AsyncClient() as client:
        adapter = ArcAdapter(client=client)
        return adapter, await adapter.fetch(source)


def _arc_postings(
    adapter: ArcAdapter, raws: list[RawJob], source: SourceConfig
) -> dict[str, JobPosting]:
    return {
        raw.native_id: adapter.enrich_eligibility(adapter.normalize(raw, source), source)
        for raw in raws
    }


class TestArcCountries:
    def test_an_empty_list_is_worldwide_only_for_arcs_own_jobs(self) -> None:
        assert resolve_countries(ARC_KIND, [], []) == [WORLDWIDE]
        assert resolve_countries(EXTERNAL_KIND, [], []) == []

    def test_a_detail_page_worldwide_location_counts_for_either_kind(self) -> None:
        assert resolve_countries(EXTERNAL_KIND, [], ["worldwide"]) == [WORLDWIDE]

    def test_stated_countries_win_and_unknown_codes_are_dropped(self) -> None:
        assert resolve_countries(ARC_KIND, ["in", "US", "ZZ"], ["worldwide"]) == ["IN", "US"]


class TestArcAdapter:
    @respx.mock
    async def test_fetch_dedupes_across_filters_and_reads_details_for_candidates_only(
        self, arc_source: SourceConfig
    ) -> None:
        site = _ArcSite()
        adapter, raws = await _arc_fetch(arc_source, site)

        assert {r.native_id for r in raws} == {
            ARC_WORLD_FT, ARC_WORLD_HR, ARC_US, ARC_LATAM, ARC_PY_ONLY,
            EXT_INDIA, EXT_EMPTY, EXT_US,
        }
        # Worldwide Arc jobs and India externals only; ARC_WORLD_HR once, though
        # it sits in both listings.
        assert sorted(site.detail_hits) == sorted(
            [ARC_WORLD_FT, ARC_WORLD_HR, ARC_PY_ONLY, EXT_INDIA]
        )
        assert adapter.fetch_truncated is False

    @respx.mock
    async def test_listing_params_and_category_path_are_sent(
        self, arc_source: SourceConfig
    ) -> None:
        route = respx.get(url__startswith="https://arc.dev").mock(side_effect=_ArcSite())
        async with httpx.AsyncClient() as client:
            await ArcAdapter(client=client).fetch(arc_source)
        listing_calls = [
            call.request.url
            for call in route.calls
            if "/details/" not in call.request.url.path and "/j/" not in call.request.url.path
        ]
        assert listing_calls[0].path == "/remote-jobs"
        assert dict(listing_calls[0].params) == {"countries": "IN", "jobRoles": "engineering"}
        assert listing_calls[1].path == "/remote-jobs/python"
        assert dict(listing_calls[1].params) == {"countries": "IN"}

    @respx.mock
    async def test_normalizes_a_worldwide_arc_job(self, arc_source: SourceConfig) -> None:
        adapter, raws = await _arc_fetch(arc_source, _ArcSite())
        postings = _arc_postings(adapter, raws, arc_source)

        full_time = postings[ARC_WORLD_FT]
        assert full_time.company == CONFIDENTIAL_EMPLOYER
        assert full_time.location_eligibility == [WORLDWIDE]
        assert full_time.locations == ["Worldwide"]
        assert (full_time.salary_min, full_time.salary_max, full_time.salary_currency) == (
            80_000,
            120_000,
            "USD",
        )
        assert full_time.employment_type == "full_time"
        assert full_time.remote is True and full_time.workplace_type == "remote"
        assert full_time.apply_url == (
            f"https://arc.dev/remote-jobs/details/{full_time.raw_json['urlString']}-{ARC_WORLD_FT}"
        )
        assert full_time.description_text
        assert full_time.posted_at is not None and full_time.posted_at.tzinfo is not None
        assert _passes_location(full_time)

        hourly = postings[ARC_WORLD_HR]
        # Annual fields are null here and hourly carries the pay; 0 means unset.
        assert (hourly.salary_min, hourly.salary_max) == (30, 45)
        assert hourly.employment_type == "contract"

    @respx.mock
    async def test_externals_need_a_stated_india(self, arc_source: SourceConfig) -> None:
        adapter, raws = await _arc_fetch(arc_source, _ArcSite())
        postings = _arc_postings(adapter, raws, arc_source)

        india = postings[EXT_INDIA]
        assert india.location_eligibility == ["IN"]
        assert india.company == "Jobgether"
        assert "/remote-jobs/j/" in india.apply_url
        assert india.description_text
        assert _passes_location(india)

        # CrowdStrike "(Remote, DEU)": empty, and empty is not worldwide here.
        assert postings[EXT_EMPTY].location_eligibility == []
        assert not _passes_location(postings[EXT_EMPTY])
        assert not _passes_location(postings[EXT_US])
        assert not _passes_location(postings[ARC_US])
        assert "IN" not in postings[ARC_LATAM].location_eligibility

    @respx.mock
    async def test_a_job_closed_on_its_detail_page_is_not_returned(
        self, arc_source: SourceConfig
    ) -> None:
        site = _ArcSite()
        detail = site.details[f"arc:{ARC_WORLD_FT}"]
        detail["props"]["pageProps"]["job"]["closed"] = True

        _, raws = await _arc_fetch(arc_source, site)
        assert ARC_WORLD_FT not in {r.native_id for r in raws}

    @respx.mock
    async def test_a_full_list_marks_the_fetch_truncated(self, arc_source: SourceConfig) -> None:
        engineering = copy.deepcopy(load_fixture("arc_listing_engineering.json"))
        props = engineering["props"]["pageProps"]
        template = props["externalJobs"][-1]  # ["US"]: no detail read
        props["externalJobs"] = [
            {**template, "randomKey": f"zz{i:08d}"} for i in range(30)
        ]
        adapter, _ = await _arc_fetch(arc_source, _ArcSite(engineering=engineering))
        assert adapter.fetch_truncated is True

    @respx.mock
    async def test_a_failed_detail_keeps_the_listing_row(self, arc_source: SourceConfig) -> None:
        site = _ArcSite()
        site.details = {}  # every detail 404s
        adapter, raws = await _arc_fetch(arc_source, site)
        job = _arc_postings(adapter, raws, arc_source)[ARC_WORLD_FT]
        assert job.location_eligibility == [WORLDWIDE]
        assert job.description_text is None


# =================================================================== Wiring
class TestRegistration:
    def test_both_are_aggregators_with_aliases(self) -> None:
        assert isinstance(get_adapter("ycombinator.com"), YcAdapter)
        assert isinstance(get_adapter("arc.dev"), ArcAdapter)
        assert is_aggregator("yc") and is_aggregator("arc")
