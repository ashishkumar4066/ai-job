"""Cutshort, Hirist, Built In and YC company-mode tests against recorded fixtures.

Fixtures are trimmed captures of the live 2026-09-19 responses. CI never hits a
live site.

The cases that matter most:
  * Cutshort USD rows hold INR in `min`/`max`; the displayed figures win.
  * A sweep that stops on age or page cap is truncated, so closure never runs.
  * Hirist and Built In read the JD only for candidates: remote rows (Hirist)
    whose title already passes the role rule.
  * Hirist pay is in lakhs; Built In countries are alpha-3.
  * Built In refuses robots-disallowed `search` queries and stops when a page
    adds no new ids.
  * YC company mode reads its Algolia key off the live page, never a stored one.
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
from app.adapters.base import AdapterError
from app.adapters.builtin import (
    BuiltInAdapter,
    extract_job_posting,
    parse_age,
    parse_cards,
    places_to_codes,
)
from app.adapters.cutshort import CutshortAdapter
from app.adapters.hirist import HiristAdapter, clean_title
from app.adapters.yc import SITE as YC_SITE
from app.adapters.yc import YcAdapter, extract_algolia_opts
from app.config import get_settings
from app.eligibility import FilterConfig, evaluate, get_filters
from app.schemas import JobPosting, RawJob, SourceConfig
from tests.conftest import FIXTURE_DIR, load_fixture

# Cutshort ids.
CS_INR = "6aad02996640f8cb0b1723e3"     # .NET Fullstack Developer, ₹20L-25L, 6+ yrs
CS_USD = "6aa98f38888acbd0818121d3"     # "$2K - $3.5K / yr", min/max hold INR
CS_HIDDEN = "6aaa9be1f4d6e5c6f3fba813"  # hideSalary: true

# Hirist ids.
HI_CANDIDATE = "1670236"   # WFH, "Wingify - Lead Backend Engineer - Java"
HI_WFH_NOT_ROLE = "1670230"  # WFH, "Lead Software Development Engineer" (no family)
HI_REMOTE_LOC = "1672027"  # "Remote" location, "Senior Rust Developer"
HI_PAID = "1668532"        # on-site, ₹30L-35L stated
HI_SIEMENS = "1671164"     # "Siemens - Golang Developer - Backend Services"

# Built In ids.
BI_STAFF = "11090490"      # candidate; JSON-LD says TELECOMMUTE, IND
BI_SWE = "9757208"         # candidate; card salary "4M-4M Annually"
BI_PRINCIPAL = "10883904"  # leadership title: never fetched
BI_MULTI = "10752704"      # card place "Tamil Nadu, IND"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """No pacing, no retries, a wide age window, and the test filter file."""
    for name in (
        "CUTSHORT_REQUEST_INTERVAL_SECONDS",
        "HIRIST_REQUEST_INTERVAL_SECONDS",
        "BUILTIN_REQUEST_INTERVAL_SECONDS",
        "YC_REQUEST_INTERVAL_SECONDS",
    ):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("HTTP_MAX_RETRIES", "0")
    # The fixtures are dated 2026-09; keep them inside the window whenever CI runs.
    monkeypatch.setenv("CUTSHORT_MAX_AGE_DAYS", "36500")
    monkeypatch.setenv("HIRIST_MAX_AGE_DAYS", "36500")
    monkeypatch.setenv("FILTERS_FILE", str(FIXTURE_DIR / "filters_test.yaml"))
    get_settings.cache_clear()
    get_filters.cache_clear()
    yield
    get_settings.cache_clear()
    get_filters.cache_clear()


@pytest.fixture
def role_rule_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test filter file disables the role rule; stage 2 needs it on."""
    for module in ("app.adapters.hirist", "app.adapters.builtin"):
        monkeypatch.setattr(f"{module}.get_filters", lambda: FilterConfig())


def _postings(adapter, raws: list[RawJob], source: SourceConfig) -> dict[str, JobPosting]:
    return {
        raw.native_id: adapter.enrich_eligibility(adapter.normalize(raw, source), source)
        for raw in raws
    }


def _days_since_2010() -> int:
    """An age window reaching back to 2010 whenever the suite runs: the 2026
    fixture rows fall inside it, rows re-dated to 2001 fall outside."""
    return (datetime.now(UTC) - datetime(2010, 1, 1, tzinfo=UTC)).days


def _passes_location(job: JobPosting) -> bool:
    verdict = evaluate(location_eligibility=job.location_eligibility, filters=FilterConfig())
    return verdict.reasons[0].startswith("location_ok")


def test_all_three_are_aggregators() -> None:
    for ats in ("cutshort", "hirist", "builtin"):
        assert is_aggregator(ats)
    assert get_adapter("cutshort.io").ats == "cutshort"
    assert get_adapter("hirist.tech").ats == "hirist"
    assert get_adapter("builtin.com").ats == "builtin"


# ================================================================= Cutshort
CUTSHORT_URL = "https://cutshort.io/backend-api/webpage/jobs/remote-jobs"


@pytest.fixture
def cutshort_source() -> SourceConfig:
    return SourceConfig(company="Cutshort", ats="cutshort", queries=[{"hub": "remote-jobs"}])


async def _cutshort_fetch(source: SourceConfig, pages: dict[int, dict]):
    hits: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        hits.append(page)
        return httpx.Response(200, json=pages.get(page, load_fixture("cutshort_past_end.json")))

    respx.get(CUTSHORT_URL).mock(side_effect=answer)
    async with httpx.AsyncClient() as client:
        adapter = CutshortAdapter(client=client)
        return adapter, await adapter.fetch(source), hits


class TestCutshort:
    @respx.mock
    async def test_walks_to_the_end_marker(self, cutshort_source: SourceConfig) -> None:
        adapter, raws, hits = await _cutshort_fetch(
            cutshort_source, {1: load_fixture("cutshort_remote_page1.json")}
        )
        assert hits == [1, 2]
        assert len(raws) == 8
        # Walking off the end is a complete sweep: closure may run.
        assert adapter.fetch_truncated is False

    @respx.mock
    async def test_first_stale_row_ends_the_sweep(
        self, cutshort_source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CUTSHORT_MAX_AGE_DAYS", str(_days_since_2010()))
        get_settings.cache_clear()
        page = load_fixture("cutshort_remote_page1.json")
        jobs = page["data"]["pageData"]["jobs"]
        jobs[-1]["jobFactSummary"]["postedDate"] = "2001-01-01T00:00:00.000Z"
        adapter, raws, hits = await _cutshort_fetch(
            cutshort_source, {1: page, 2: load_fixture("cutshort_remote_page1.json")}
        )
        assert hits == [1]  # page 2 is never requested
        assert len(raws) == 7
        assert adapter.fetch_truncated is True

    @respx.mock
    async def test_page_cap_truncates(
        self, cutshort_source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CUTSHORT_MAX_PAGES", "1")
        get_settings.cache_clear()
        page = load_fixture("cutshort_remote_page1.json")
        adapter, _, hits = await _cutshort_fetch(cutshort_source, {1: page, 2: page})
        assert hits == [1]
        assert adapter.fetch_truncated is True

    @respx.mock
    async def test_error_on_the_first_page_fails_the_source(
        self, cutshort_source: SourceConfig
    ) -> None:
        # An unknown hub answers with the same end-of-list error on page 1.
        with pytest.raises(AdapterError, match="jobs_not_found"):
            await _cutshort_fetch(cutshort_source, {})

    def _normalized(self, source: SourceConfig) -> dict[str, JobPosting]:
        jobs = load_fixture("cutshort_remote_page1.json")["data"]["pageData"]["jobs"]
        raws = [RawJob(native_id=job["_id"], payload=job) for job in jobs]
        return _postings(CutshortAdapter(), raws, source)

    def test_usd_pay_uses_the_displayed_figures(self, cutshort_source: SourceConfig) -> None:
        job = self._normalized(cutshort_source)[CS_USD]
        # Not 95,602 / 334,608: those are the INR conversion.
        assert (job.salary_min, job.salary_max, job.salary_currency) == (2000.0, 3500.0, "USD")

    def test_inr_pay_and_hidden_pay(self, cutshort_source: SourceConfig) -> None:
        postings = self._normalized(cutshort_source)
        assert (postings[CS_INR].salary_min, postings[CS_INR].salary_max) == (2_000_000.0, 2_500_000.0)
        assert postings[CS_INR].salary_currency == "INR"
        hidden = postings[CS_HIDDEN]
        assert (hidden.salary_min, hidden.salary_max, hidden.salary_currency) == (None, None, None)

    def test_remote_row_with_no_place_is_india_eligible(
        self, cutshort_source: SourceConfig
    ) -> None:
        job = self._normalized(cutshort_source)[CS_INR]
        assert job.remote is True
        assert job.workplace_type == "remote"
        assert job.location_eligibility == ["IN"]
        assert _passes_location(job)

    def test_fields(self, cutshort_source: SourceConfig) -> None:
        job = self._normalized(cutshort_source)[CS_INR]
        assert job.title == ".NET Fullstack Developer (Azure)"
        assert job.source_key.startswith("cutshort:")
        assert job.apply_url.startswith("https://cutshort.io/job/")
        assert job.employment_type == "full_time"
        assert job.posted_at is not None and job.posted_at.tzinfo is not None
        # Only the band's minimum reaches the role rule.
        assert job.description_text.startswith("Minimum 6+ years experience.")


# =================================================================== Hirist
HIRIST_API = "https://gladiator.hirist.tech/job"


@pytest.fixture
def hirist_source() -> SourceConfig:
    return SourceConfig(company="Hirist", ats="hirist", queries=[{"categoryId": 1}])


class _HiristSite:
    def __init__(self, pages: dict[int, dict] | None = None) -> None:
        self.pages = pages or {0: load_fixture("hirist_category1_page0.json")}
        self.details = load_fixture("hirist_details.json")
        self.detail_hits: list[str] = []
        self.versions: set[str | None] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.versions.add(request.headers.get("version"))
        if request.url.path == "/job/detail":
            job_id = request.url.params["jobcode"]
            self.detail_hits.append(job_id)
            if job_id in self.details:
                return httpx.Response(200, json=self.details[job_id])
            return httpx.Response(404, json={"error": {"name": "JOB_NOT_FOUND"}})
        page = int(request.url.params["page"])
        return httpx.Response(200, json=self.pages.get(page, {"data": [], "hasMore": False}))


async def _hirist_fetch(source: SourceConfig, site: _HiristSite):
    respx.get(url__startswith=HIRIST_API).mock(side_effect=site)
    async with httpx.AsyncClient() as client:
        adapter = HiristAdapter(client=client)
        return adapter, await adapter.fetch(source)


class TestHirist:
    @respx.mock
    async def test_details_only_for_remote_rows_with_a_wanted_title(
        self, hirist_source: SourceConfig, role_rule_on: None
    ) -> None:
        site = _HiristSite()
        adapter, raws = await _hirist_fetch(hirist_source, site)
        assert len(raws) == 9
        # Four remote rows; only one has a title the role rule passes.
        assert site.detail_hits == [HI_CANDIDATE]
        assert HI_WFH_NOT_ROLE not in site.detail_hits  # remote, but no wanted family
        assert site.versions == {"2"}  # the API needs `version: 2`
        assert adapter.fetch_truncated is False  # hasMore was false

    @respx.mock
    async def test_detail_budget(
        self, hirist_source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Role rule off (test filters), so every remote row is a candidate.
        monkeypatch.setenv("HIRIST_DETAIL_BUDGET", "2")
        get_settings.cache_clear()
        site = _HiristSite()
        await _hirist_fetch(hirist_source, site)
        assert len(site.detail_hits) == 2
        assert HI_PAID not in site.detail_hits  # on-site rows are never candidates

    @respx.mock
    async def test_stale_page_stops_the_query(
        self, hirist_source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HIRIST_MAX_AGE_DAYS", str(_days_since_2010()))
        get_settings.cache_clear()

        live = copy.deepcopy(load_fixture("hirist_category1_page0.json"))
        live["hasMore"] = True
        stale = copy.deepcopy(live)
        for row in stale["data"]:
            row["createdTimeMs"] = 978307200000  # 2001-01-01
        site = _HiristSite({0: live, 1: stale, 2: live})

        adapter, raws = await _hirist_fetch(hirist_source, site)
        assert {raw.native_id for raw in raws} == {str(r["id"]) for r in live["data"]}
        assert adapter.fetch_truncated is True

    def _normalized(self, source: SourceConfig) -> dict[str, JobPosting]:
        page = load_fixture("hirist_category1_page0.json")
        details = load_fixture("hirist_details.json")
        raws = []
        for row in page["data"]:
            payload = dict(row)
            if str(row["id"]) in details:
                payload["_detail"] = {"introText": details[str(row["id"])]["data"]["introText"]}
            raws.append(RawJob(native_id=str(row["id"]), payload=payload))
        return _postings(HiristAdapter(), raws, source)

    def test_pay_is_in_lakhs(self, hirist_source: SourceConfig) -> None:
        postings = self._normalized(hirist_source)
        paid = postings[HI_PAID]
        assert (paid.salary_min, paid.salary_max, paid.salary_currency) == (3_000_000, 3_500_000, "INR")
        assert postings[HI_CANDIDATE].salary_max is None  # hideSal: 1

    def test_remote_and_india_locations(self, hirist_source: SourceConfig) -> None:
        postings = self._normalized(hirist_source)
        wfh = postings[HI_CANDIDATE]  # "Anywhere in India/Multiple Locations", "Delhi NCR", ...
        assert wfh.remote is True and wfh.workplace_type == "remote"
        assert wfh.location_eligibility == ["IN"]
        assert postings[HI_REMOTE_LOC].remote is True  # a "Remote" location, wfh flag or not
        onsite = postings[HI_PAID]
        assert onsite.remote is False and onsite.workplace_type == "onsite"
        assert _passes_location(onsite)

    def test_title_prefix_and_description(self, hirist_source: SourceConfig) -> None:
        postings = self._normalized(hirist_source)
        assert postings[HI_SIEMENS].title == "Golang Developer - Backend Services"
        assert postings[HI_CANDIDATE].title == "Lead Backend Engineer - Java"
        candidate = postings[HI_CANDIDATE]
        assert candidate.description_html  # the JD from stage 2
        assert candidate.description_text.startswith("Minimum 6+ years experience.")
        assert "Skills:" in candidate.description_text
        # A row whose JD was not read still carries its years and skills.
        assert postings[HI_PAID].description_html is None
        assert postings[HI_PAID].description_text.startswith("Minimum 7+ years experience.")

    @pytest.mark.parametrize(
        ("title", "company", "expected"),
        [
            ("Siemens - Golang Developer", "Siemens", "Golang Developer"),
            ("Wingify - Lead Backend Engineer", "Wingify Software", "Lead Backend Engineer"),
            ("Senior Rust Developer", "Nexaxis", "Senior Rust Developer"),
            # A hyphen inside the role is not an employer prefix.
            ("Backend Engineer - Java", "Acme", "Backend Engineer - Java"),
        ],
    )
    def test_clean_title(self, title: str, company: str, expected: str) -> None:
        assert clean_title(title, company) == expected


# ================================================================= Built In
BUILTIN_LISTING = "https://builtin.com/jobs/remote/dev-engineering"


@pytest.fixture
def builtin_source() -> SourceConfig:
    return SourceConfig(
        company="Built In",
        ats="builtin",
        queries=[{"path": "remote/dev-engineering", "country": "IND"}],
    )


class _BuiltInSite:
    def __init__(self, pages: dict[str, str] | None = None) -> None:
        listing = load_fixture("builtin_listing.json")
        # Page 3 repeats page 2, as deep pages do live.
        self.pages = pages or {"1": listing["1"], "2": listing["2"], "3": listing["2"]}
        self.details = load_fixture("builtin_details.json")
        self.listing_hits: list[str] = []
        self.detail_hits: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/job/"):
            self.detail_hits.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, text=self.details.get(path, "<html></html>"))
        page = request.url.params.get("page", "1")
        self.listing_hits.append(page)
        assert request.url.params.get("country") == "IND"
        return httpx.Response(200, text=self.pages.get(page, "<html></html>"))


async def _builtin_fetch(source: SourceConfig, site: _BuiltInSite):
    respx.get(url__startswith="https://builtin.com/").mock(side_effect=site)
    async with httpx.AsyncClient() as client:
        adapter = BuiltInAdapter(client=client)
        return adapter, await adapter.fetch(source)


class TestBuiltIn:
    def test_parses_cards(self) -> None:
        cards = {c["id"]: c for c in parse_cards(load_fixture("builtin_listing.json")["1"])}
        assert set(cards) == {"10883904", "11137942", "11124849", BI_STAFF}
        staff = cards[BI_STAFF]
        assert staff["title"] == "Staff Software Engineer, Device Management - India"
        assert staff["company"] == "JumpCloud"
        assert staff["workplace"] == "In-Office or Remote"
        assert staff["places"] == ["Bangalore, Bengaluru, Karnataka, IND"]
        assert staff["age_text"]

    @respx.mock
    async def test_stops_when_a_page_adds_nothing_and_reads_candidates_only(
        self, builtin_source: SourceConfig, role_rule_on: None
    ) -> None:
        site = _BuiltInSite()
        adapter, raws = await _builtin_fetch(builtin_source, site)
        assert site.listing_hits == ["1", "2", "3"]
        assert len(raws) == 8
        assert sorted(site.detail_hits) == sorted([BI_STAFF, BI_SWE])
        assert BI_PRINCIPAL not in site.detail_hits
        # No sweep of Built In is known to be complete.
        assert adapter.fetch_truncated is True

    @respx.mock
    async def test_empty_first_page_fails_loudly(self, builtin_source: SourceConfig) -> None:
        with pytest.raises(AdapterError, match="markup changed"):
            await _builtin_fetch(builtin_source, _BuiltInSite({"1": "<html></html>"}))

    async def test_refuses_a_search_query(self) -> None:
        source = SourceConfig(
            company="Built In", ats="builtin", queries=[{"path": "jobs", "search": "python"}]
        )
        with pytest.raises(AdapterError, match="robots"):
            await BuiltInAdapter().fetch(source)

    @respx.mock
    async def test_normalized(self, builtin_source: SourceConfig, role_rule_on: None) -> None:
        adapter, raws = await _builtin_fetch(builtin_source, _BuiltInSite())
        postings = _postings(adapter, raws, builtin_source)

        staff = postings[BI_STAFF]  # read in stage 2
        assert staff.remote is True and staff.workplace_type == "remote"  # TELECOMMUTE
        assert staff.location_eligibility == ["IN"]
        assert staff.posted_at == datetime(2026, 9, 9, tzinfo=UTC)
        assert staff.description_html and len(staff.description_text) > 1000
        assert staff.apply_url == "https://builtin.com/job/staff-software-engineer-device-management-india/11090490"
        assert _passes_location(staff)

        swe = postings[BI_SWE]
        assert swe.raw_json["salary_text"] == "4M-4M Annually"
        assert swe.salary_max is None  # no currency stated, so not guessed

        principal = postings[BI_PRINCIPAL]  # card only
        assert principal.description_html is None
        assert principal.description_text  # the card summary
        assert principal.posted_at is not None  # from "Reposted N Hours Ago"
        assert postings[BI_MULTI].location_eligibility == ["IN"]  # "Tamil Nadu, IND"

    def test_extracts_escaped_graph_json_ld(self) -> None:
        page = next(iter(load_fixture("builtin_details.json").values()))
        assert 'application/ld&#x2B;json' in page
        job = extract_job_posting(page)
        assert job is not None and job["@type"] == "JobPosting"

    @pytest.mark.parametrize(
        ("places", "codes"),
        [
            (["Pune, Mahārāshtra, IND"], ["IN"]),
            (["IND"], ["IN"]),
            (["India"], ["IN"]),
            (["Zürich, CHE", "Miami, FL, USA", "Gurugram, Haryana, IND"], ["CH", "US", "IN"]),
            # A bare alpha-2 is ambiguous (IN is also Indiana); not guessed.
            (["IN"], []),
        ],
    )
    def test_alpha3_places(self, places: list[str], codes: list[str]) -> None:
        assert places_to_codes(places) == codes

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Reposted 4 Hours Ago", datetime(2026, 9, 19, tzinfo=UTC)),
            ("8 Days Ago", datetime(2026, 9, 11, tzinfo=UTC)),
            ("Reposted One Month Ago", datetime(2026, 8, 20, tzinfo=UTC)),
            ("Reposted An Hour Ago", datetime(2026, 9, 19, tzinfo=UTC)),
            ("Yesterday", datetime(2026, 9, 18, tzinfo=UTC)),
            ("", None),
        ],
    )
    def test_relative_age(self, text: str, expected: datetime | None) -> None:
        assert parse_age(text, datetime(2026, 9, 19, 15, 30, tzinfo=UTC)) == expected


# ======================================================== YC company mode
ALGOLIA_HOST = "https://45bwzj1sgc-dsn.algolia.net"  # httpx lowercases hosts


def _yc_html(page: dict) -> str:
    attr = html.escape(json.dumps(page), quote=True)
    return f'<html><body><div data-page="{attr}"></div></body></html>'


class _YcCompanySite:
    def __init__(self) -> None:
        self.companies = load_fixture("yc_company_jobs.json")
        self.company_hits: list[str] = []
        self.algolia_bodies: list[dict] = []
        self.algolia_keys: set[str | None] = set()

    def site(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/companies":
            return httpx.Response(200, text=load_fixture("yc_companies_page.json")["html"])
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "companies" and parts[2] == "jobs":
            self.company_hits.append(parts[1])
            return httpx.Response(200, text=_yc_html(self.companies[parts[1]]))
        return httpx.Response(404)  # detail pages: stage 2 degrades to listing data

    def algolia(self, request: httpx.Request) -> httpx.Response:
        self.algolia_keys.add(request.headers.get("x-algolia-api-key"))
        self.algolia_bodies.append(json.loads(request.content))
        return httpx.Response(200, json=load_fixture("yc_algolia_companies.json") | {"nbPages": 1})


async def _yc_companies_fetch(site: _YcCompanySite, regions: list[str] | None = None):
    respx.get(url__startswith=YC_SITE).mock(side_effect=site.site)
    respx.post(url__startswith=ALGOLIA_HOST).mock(side_effect=site.algolia)
    query: dict = {"mode": "companies"}
    if regions:
        query["regions"] = regions
    source = SourceConfig(company="Y Combinator", ats="yc", queries=[query])
    async with httpx.AsyncClient() as client:
        adapter = YcAdapter(client=client)
        return adapter, await adapter.fetch(source), source


class TestYcCompanyMode:
    def test_reads_the_key_off_the_page(self) -> None:
        opts = extract_algolia_opts(load_fixture("yc_companies_page.json")["html"])
        assert opts == {"app": "45BWZJ1SGC", "key": "FIXTURE_SCOPED_KEY"}
        assert extract_algolia_opts("<html></html>") is None

    @respx.mock
    async def test_lists_hiring_companies_then_their_jobs(self) -> None:
        site = _YcCompanySite()
        adapter, raws, source = await _yc_companies_fetch(site, ["India", "Fully Remote"])
        assert site.algolia_keys == {"FIXTURE_SCOPED_KEY"}
        params = site.algolia_bodies[0]["params"]
        assert '"isHiring:true"' in params
        assert '"regions:India", "regions:Fully Remote"' in params
        assert site.company_hits == ["groww", "meesho", "deel"]
        assert len(raws) == 2 + 0 + 5
        assert adapter.fetch_truncated is False

        postings = _postings(adapter, raws, source)
        groww = postings["15730"]
        assert groww.location_eligibility == ["IN"]  # "Bengaluru, Karnataka"
        assert groww.is_us_employer is False  # from the company page, no detail read
        assert postings["57710"].is_us_employer is True  # Deel

    @respx.mock
    async def test_company_budget_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YC_COMPANY_BUDGET", "1")
        get_settings.cache_clear()
        site = _YcCompanySite()
        adapter, _, _ = await _yc_companies_fetch(site)
        assert site.company_hits == ["groww"]
        assert adapter.fetch_truncated is True

    @respx.mock
    async def test_missing_key_fails_the_source(self) -> None:
        respx.get(url__startswith=YC_SITE).mock(return_value=httpx.Response(200, text="<html></html>"))
        source = SourceConfig(company="Y Combinator", ats="yc", queries=[{"mode": "companies"}])
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="AlgoliaOpts"):
                await YcAdapter(client=client).fetch(source)
