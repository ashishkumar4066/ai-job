"""Remote OK + We Work Remotely + RemoteYeah adapter tests against recorded fixtures.

All fixtures are trimmed captures of real 2026-09-19 responses. CI never hits
a live endpoint.

The cases that matter most:
  * A blank or "Anywhere" location is a worldwide *claim*: kept when the
    posting says nothing more, narrowed when its headquarters line or prose
    names a country (DomainTools' "US-based", Reddit's "Remote - United
    States" headquarters).
  * A pay sentence that names a country ("salary ranges for all US-based
    postings") does not narrow anything.
  * RemoteYeah's unknown slug redirects to a broader page with a 200, and the
    adapter refuses it instead of sweeping the wrong list.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.adapters import get_adapter, is_aggregator
from app.adapters.base import AdapterError
from app.adapters.remoteok import API_URL as REMOTEOK_URL
from app.adapters.remoteok import RemoteOkAdapter, repair_mojibake
from app.adapters.remoteyeah import RemoteYeahAdapter, parse_cards
from app.adapters.weworkremotely import FEED_URL, WeWorkRemotelyAdapter, parse_feed, split_title
from app.config import get_settings
from app.eligibility import FilterConfig, evaluate, get_filters
from app.geo import WORLDWIDE, restriction_from_prose
from app.ingest import _apply_eligibility as apply_eligibility
from app.schemas import JobPosting, RawJob, SourceConfig
from tests.conftest import FIXTURE_DIR, load_fixture


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """No pacing, no retries, a wide age window, and the test filter file."""
    monkeypatch.setenv("REMOTEYEAH_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("HTTP_MAX_RETRIES", "0")
    # The fixtures are dated 2026-09; keep them inside the window whenever CI runs.
    monkeypatch.setenv("REMOTEYEAH_MAX_AGE_DAYS", "36500")
    monkeypatch.setenv("FILTERS_FILE", str(FIXTURE_DIR / "filters_test.yaml"))
    get_settings.cache_clear()
    get_filters.cache_clear()
    yield
    get_settings.cache_clear()
    get_filters.cache_clear()


@pytest.fixture
def role_rule_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test filter file disables the role rule; stage 2 needs it on."""
    monkeypatch.setattr("app.adapters.remoteyeah.get_filters", lambda: FilterConfig())


def _normalize(adapter, source: SourceConfig, raws: list[RawJob]) -> dict[str, JobPosting]:
    return {raw.native_id: posting for raw, posting in zip(raws, adapter.normalize_all(raws, source))}


def _passes_location(job: JobPosting) -> bool:
    verdict = evaluate(location_eligibility=job.location_eligibility, filters=FilterConfig())
    return verdict.reasons[0].startswith("location_ok")


def test_all_three_are_registered_aggregators() -> None:
    for ats in ("remoteok", "weworkremotely", "remoteyeah"):
        assert is_aggregator(ats)
    assert get_adapter("remoteok.com").ats == "remoteok"
    assert get_adapter("wwr").ats == "weworkremotely"
    assert get_adapter("remoteyeah.com").ats == "remoteyeah"


# ----------------------------------------------------------- shared prose check
class TestRestrictionFromProse:
    def test_pay_sentences_are_skipped_when_asked(self) -> None:
        text = (
            "We share base salary ranges for all US-based job postings.\n"
            "We hire engineers across Europe and Asia."
        )
        assert restriction_from_prose(text) == "United States"  # Wellfound's reading
        assert restriction_from_prose(text, skip_pay_sentences=True) is None

    def test_a_real_restriction_still_narrows(self) -> None:
        text = "This role is remote within the United States.\nCompensation: $150k."
        assert restriction_from_prose(text, skip_pay_sentences=True) == "United States"


# ================================================================= Remote OK
REMOTEOK_SHOPIFY = "1137405"   # blank location, mojibake, $80k-$250k
REMOTEOK_DOMAIN = "1137396"    # blank location, JD says "US-based"
REMOTEOK_US = "1136950"        # "Remote - US"
REMOTEOK_INDIA = "1136216"     # "Chennai, Chennai, Tamil Nadu, India"
REMOTEOK_JUNK_PAY = "1137139"  # 10,000-750,000 placeholder range
REMOTEOK_BOGOTA = "1135643"    # "BogotÃ¡, ..." mojibake location


@pytest.fixture
def remoteok_source() -> SourceConfig:
    return SourceConfig(company="Remote OK", ats="remoteok", queries=[{"tag": "dev"}])


async def _remoteok_raws(source: SourceConfig, payload=None) -> tuple[RemoteOkAdapter, list[RawJob]]:
    route = respx.get(REMOTEOK_URL).mock(
        return_value=httpx.Response(200, json=payload or load_fixture("remoteok_dev.json"))
    )
    async with httpx.AsyncClient() as client:
        adapter = RemoteOkAdapter(client=client)
        raws = await adapter.fetch(source)
    assert route.called
    return adapter, raws


class TestRemoteOk:
    @respx.mock
    async def test_fetch_skips_the_legal_notice_and_marks_truncated(
        self, remoteok_source: SourceConfig
    ) -> None:
        adapter, raws = await _remoteok_raws(remoteok_source)

        assert len(raws) == 6, "element 0 is the legal notice, not a job"
        assert adapter.fetch_truncated, "each tag serves only its newest ~100 rows"
        assert respx.calls[0].request.url.params["tag"] == "dev"

    @respx.mock
    async def test_an_unknown_tag_returns_nothing(self, remoteok_source: SourceConfig) -> None:
        notice = load_fixture("remoteok_dev.json")[:1]
        _, raws = await _remoteok_raws(remoteok_source, payload=notice)
        assert raws == []

    @respx.mock
    async def test_a_non_list_payload_fails_loudly(self, remoteok_source: SourceConfig) -> None:
        respx.get(REMOTEOK_URL).mock(return_value=httpx.Response(200, json={"error": "x"}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="unexpected Remote OK payload"):
                await RemoteOkAdapter(client=client).fetch(remoteok_source)

    @respx.mock
    async def test_blank_location_is_an_unverified_worldwide_claim(
        self, remoteok_source: SourceConfig
    ) -> None:
        adapter, raws = await _remoteok_raws(remoteok_source)
        job = _normalize(adapter, remoteok_source, raws)[REMOTEOK_SHOPIFY]

        assert job.location_eligibility == [WORLDWIDE]
        assert any("unverified" in r for r in job.eligibility_reasons)
        assert (job.salary_min, job.salary_max, job.salary_currency) == (80000, 250000, "USD")
        assert job.apply_url.startswith("https://remoteOK.com/remote-jobs/"), "terms: link back"
        assert job.company == "Sanctuary Computer"
        assert "Â" not in (job.description_html or ""), "mojibake was repaired"

    @respx.mock
    async def test_prose_narrows_a_blank_location(self, remoteok_source: SourceConfig) -> None:
        adapter, raws = await _remoteok_raws(remoteok_source)
        job = _normalize(adapter, remoteok_source, raws)[REMOTEOK_DOMAIN]

        assert job.location_eligibility == ["US"]
        assert not _passes_location(job)
        assert any("restricts candidates to United States" in r for r in job.eligibility_reasons)

    @respx.mock
    async def test_stated_locations_resolve(self, remoteok_source: SourceConfig) -> None:
        adapter, raws = await _remoteok_raws(remoteok_source)
        jobs = _normalize(adapter, remoteok_source, raws)

        assert jobs[REMOTEOK_US].location_eligibility == ["US"]
        assert jobs[REMOTEOK_INDIA].location_eligibility == ["IN"]
        assert _passes_location(jobs[REMOTEOK_INDIA])
        assert jobs[REMOTEOK_BOGOTA].location_eligibility == ["CO"]
        assert jobs[REMOTEOK_BOGOTA].locations == ["Bogotá, Bogotá, Distrito Capital, Colombia"]

    @respx.mock
    async def test_a_placeholder_pay_range_reads_as_unstated(
        self, remoteok_source: SourceConfig
    ) -> None:
        adapter, raws = await _remoteok_raws(remoteok_source)
        job = _normalize(adapter, remoteok_source, raws)[REMOTEOK_JUNK_PAY]
        assert (job.salary_min, job.salary_max, job.salary_currency) == (None, None, None)

    def test_mojibake_repair_leaves_correct_text_alone(self) -> None:
        assert repair_mojibake("BogotÃ¡") == "Bogotá"
        assert repair_mojibake("Bogotá") == "Bogotá"  # a lone é does not round-trip
        assert repair_mojibake("東京") == "東京"
        assert repair_mojibake(None) is None

    @respx.mock
    async def test_adapter_notes_survive_the_eligibility_stage(
        self, remoteok_source: SourceConfig
    ) -> None:
        adapter, raws = await _remoteok_raws(remoteok_source)
        job = _normalize(adapter, remoteok_source, raws)[REMOTEOK_SHOPIFY]

        apply_eligibility([job], FilterConfig())

        assert job.eligibility_reasons[0].startswith("location_ok")
        assert job.eligibility_reasons[-1].startswith("remoteok: candidate location unstated")


# ========================================================== We Work Remotely
WWR_FEED = FEED_URL.format(category="remote-programming-jobs")


@pytest.fixture
def wwr_source() -> SourceConfig:
    return SourceConfig(
        company="We Work Remotely",
        ats="weworkremotely",
        queries=[{"category": "remote-programming-jobs"}],
    )


def _wwr_feed() -> str:
    return (FIXTURE_DIR / "weworkremotely_programming.rss").read_text(encoding="utf-8")


async def _wwr_raws(source: SourceConfig) -> tuple[WeWorkRemotelyAdapter, list[RawJob]]:
    respx.get(WWR_FEED).mock(return_value=httpx.Response(200, text=_wwr_feed()))
    async with httpx.AsyncClient() as client:
        adapter = WeWorkRemotelyAdapter(client=client)
        return adapter, await adapter.fetch(source)


class TestWeWorkRemotely:
    def test_parses_items(self) -> None:
        items = parse_feed(_wwr_feed())
        assert len(items) == 7
        assert all(item["title"] and item["link"] for item in items)

    def test_splits_company_off_the_title(self) -> None:
        assert split_title("Legion: Chief Architect") == ("Legion", "Chief Architect")
        assert split_title("No company here") == (None, "No company here")

    @respx.mock
    async def test_fetch_is_complete_so_closure_can_run(self, wwr_source: SourceConfig) -> None:
        adapter, raws = await _wwr_raws(wwr_source)
        assert len(raws) == 7
        assert not adapter.fetch_truncated

    @respx.mock
    async def test_a_body_that_is_not_rss_fails_the_source(self, wwr_source: SourceConfig) -> None:
        # Verified live: an unknown category answers 301 with an empty body.
        respx.get(WWR_FEED).mock(return_value=httpx.Response(200, text=""))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AdapterError, match="is not RSS"):
                await WeWorkRemotelyAdapter(client=client).fetch(wwr_source)

    @respx.mock
    async def test_location_rules(self, wwr_source: SourceConfig) -> None:
        adapter, raws = await _wwr_raws(wwr_source)
        jobs = {job.company + ": " + job.title: job for job in adapter.normalize_all(raws, wwr_source)}

        def find(prefix: str) -> JobPosting:
            return next(job for key, job in jobs.items() if key.startswith(prefix))

        # A stated country list wins over the "Anywhere" region.
        twikey = find("Twikey")
        assert "IN" not in twikey.location_eligibility and "DE" in twikey.location_eligibility
        # "Headquarters: Remote - United States" narrows the claim; its pay
        # sentence ("US-based job postings") is not what did it.
        reddit = find("Reddit")
        assert reddit.location_eligibility == ["US"]
        assert any("headquarters" in r for r in reddit.eligibility_reasons)
        # "remote within the United States" in the prose.
        assert find("Expel").location_eligibility == ["US"]
        assert find("CircleCI").location_eligibility == ["CA"]
        assert find("Toptal").location_eligibility == ["CA", "MX", "US"]  # "North America Only"
        # Nothing narrows Toggl's claim, so it stands, flagged unverified.
        toggl = find("Toggl")
        assert toggl.location_eligibility == [WORLDWIDE]
        assert _passes_location(toggl)
        assert any("unverified" in r for r in toggl.eligibility_reasons)

    @respx.mock
    async def test_normalized_fields(self, wwr_source: SourceConfig) -> None:
        adapter, raws = await _wwr_raws(wwr_source)
        job = next(j for j in adapter.normalize_all(raws, wwr_source) if j.company == "Toggl")

        assert job.title == "Senior Full Stack"
        assert job.apply_url.startswith("https://weworkremotely.com/remote-jobs/")
        assert job.source_key == f"weworkremotely:toggl:{job.apply_url.rsplit('/', 1)[-1]}"
        assert job.posted_at is not None and job.posted_at.tzinfo is not None
        assert job.remote and job.workplace_type == "remote"
        assert job.employment_type == "full_time"


# ================================================================ RemoteYeah
RY_LISTING = "https://remoteyeah.com/remote-backend-engineer-jobs-in-india"
RY_PRAGMATIKE = "remote-backend-engineer-go-endpoint-security-pragmatike"  # India, 5y
RY_CLERA = "remote-senior-backend-engineer-clera-3"                        # Worldwide, USD
RY_FEATURED = "remote-developer-advocate-indonesian-serpapi"              # no date


@pytest.fixture
def ry_source() -> SourceConfig:
    return SourceConfig(
        company="RemoteYeah",
        ats="remoteyeah",
        queries=[{"path": "remote-backend-engineer-jobs-in-india"}],
    )


class _RemoteYeahSite:
    """Serves the listing as page 1; page 2 redirects back, as past-end pages do."""

    def __init__(self, listing: str | None = None) -> None:
        self.listing = listing or (FIXTURE_DIR / "remoteyeah_listing.html").read_text(
            encoding="utf-8"
        )
        self.details = load_fixture("remoteyeah_details.json")
        self.detail_hits: list[str] = []
        self.listing_hits: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.strip("/")
        if path.startswith("jobs/"):
            slug = path.split("/", 1)[1]
            self.detail_hits.append(slug)
            ld = self.details.get(slug)
            body = (
                f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld else ""
            )
            return httpx.Response(200, text=f"<html>{body}</html>")
        self.listing_hits.append(path)
        if path.endswith("/page/2"):
            return httpx.Response(302, headers={"Location": RY_LISTING})
        if path == "remote-bogusrole-jobs-in-india":
            return httpx.Response(302, headers={"Location": "https://remoteyeah.com/remote-jobs-in-india"})
        return httpx.Response(200, text=self.listing)


async def _ry_fetch(source: SourceConfig, site: _RemoteYeahSite):
    respx.get(url__startswith="https://remoteyeah.com/").mock(side_effect=site)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        adapter = RemoteYeahAdapter(client=client)
        return adapter, await adapter.fetch(source)


class TestRemoteYeah:
    def test_parses_cards(self) -> None:
        html = (FIXTURE_DIR / "remoteyeah_listing.html").read_text(encoding="utf-8")
        cards = {c["id"]: c for c in parse_cards(html)}

        assert len(cards) == 10
        pragmatike = cards[RY_PRAGMATIKE]
        assert pragmatike["places"] == ["India"]
        assert pragmatike["years"] == 5
        assert pragmatike["published"] == "2026-09-18T10:01:37+00:00"
        assert cards[RY_CLERA]["places"] == ["Worldwide"]
        assert cards[RY_FEATURED]["featured"] and cards[RY_FEATURED]["published"] is None

    @respx.mock
    async def test_sweep_stops_when_the_next_page_redirects_back(
        self, ry_source: SourceConfig, role_rule_on: None
    ) -> None:
        site = _RemoteYeahSite()
        adapter, raws = await _ry_fetch(ry_source, site)

        assert len(raws) == 10
        assert site.listing_hits == [
            "remote-backend-engineer-jobs-in-india",
            "remote-backend-engineer-jobs-in-india/page/2",
            "remote-backend-engineer-jobs-in-india",  # the redirect target
        ]
        assert adapter.fetch_truncated
        # Candidates only: wanted title and India/worldwide on the card.
        assert RY_PRAGMATIKE in site.detail_hits
        assert RY_FEATURED not in site.detail_hits, "Developer Advocate is not a wanted role"

    @respx.mock
    async def test_an_unknown_slug_fails_instead_of_sweeping_everything(self) -> None:
        source = SourceConfig(
            company="RemoteYeah",
            ats="remoteyeah",
            queries=[{"path": "remote-bogusrole-jobs-in-india"}],
        )
        with pytest.raises(AdapterError, match="slug is unknown"):
            await _ry_fetch(source, _RemoteYeahSite())

    async def test_a_malformed_path_is_refused(self) -> None:
        source = SourceConfig(
            company="RemoteYeah", ats="remoteyeah", queries=[{"path": "jobs?page=2"}]
        )
        with pytest.raises(AdapterError, match="listing `path`"):
            await RemoteYeahAdapter().fetch(source)

    @respx.mock
    async def test_stale_cards_end_the_sweep(
        self, ry_source: SourceConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        days = (datetime.now(UTC) - datetime(2010, 1, 1, tzinfo=UTC)).days
        monkeypatch.setenv("REMOTEYEAH_MAX_AGE_DAYS", str(days))
        get_settings.cache_clear()
        listing = (FIXTURE_DIR / "remoteyeah_listing.html").read_text(encoding="utf-8")
        # Re-date the last card to 2001: it falls outside the window.
        head, sep, tail = listing.rpartition('datetime="')
        listing = head + sep + "2001-01-01T00:00:00+00:00" + tail[len("2026-09-16T20:01:40+00:00"):]

        site = _RemoteYeahSite(listing)
        _, raws = await _ry_fetch(ry_source, site)

        assert len(raws) == 9
        assert site.listing_hits == ["remote-backend-engineer-jobs-in-india"], "no page 2"

    @respx.mock
    async def test_normalized(self, ry_source: SourceConfig, role_rule_on: None) -> None:
        adapter, raws = await _ry_fetch(ry_source, _RemoteYeahSite())
        jobs = _normalize(adapter, ry_source, raws)

        india = jobs[RY_PRAGMATIKE]
        assert india.location_eligibility == ["IN"]
        assert _passes_location(india)
        assert india.description_text.startswith("Minimum 5+ years experience.")
        assert india.employment_type == "full_time"
        assert india.apply_url == f"https://remoteyeah.com/jobs/{RY_PRAGMATIKE}"
        assert india.posted_at == datetime(2026, 9, 18, 10, 1, 37, tzinfo=UTC)

        clera = jobs[RY_CLERA]
        assert clera.location_eligibility == [WORLDWIDE]
        assert clera.salary_currency == "USD" and clera.salary_min == 160000

        # Not a candidate, so no detail: card data only, and pay unstated.
        featured = jobs[RY_FEATURED]
        assert featured.salary_min is None
        assert featured.location_eligibility == [WORLDWIDE]

    def test_detail_locations_override_the_card(self, ry_source: SourceConfig) -> None:
        details = load_fixture("remoteyeah_details.json")
        card = {
            "id": "remote-senior-platform-engineer-lifen",
            "url": "https://remoteyeah.com/jobs/remote-senior-platform-engineer-lifen",
            "title": "Senior Platform Engineer",
            "company": "Lifen",
            "places": ["Worldwide"],
            "_detail": copy.deepcopy(details["remote-senior-platform-engineer-lifen"]),
        }
        job = RemoteYeahAdapter().normalize(RawJob(native_id=card["id"], payload=card), ry_source)

        assert "FR" in job.location_eligibility and "IN" not in job.location_eligibility
        assert (job.salary_min, job.salary_max, job.salary_currency) == (65000, 75000, "EUR")
