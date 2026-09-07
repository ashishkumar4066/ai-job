"""Ingest-level eligibility tests — the Phase 1 acceptance criteria.

These run the whole pipeline against the **shipped** `config/filters.yaml`
(IN + worldwide, USD) with both aggregator boards mocked from fixtures, and
assert the two behaviours the spec is explicit about:

  * nothing is silently dropped — failing jobs are stored and flagged, and
  * only `eligibility_pass = true` jobs generate alerts.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.adapters.himalayas import API_URL as HIMALAYAS_URL
from app.adapters.remotive import API_URL as REMOTIVE_URL
from app.config import BACKEND_ROOT, get_settings
from app.eligibility import get_filters
from app.ingest import run_ingest
from app.models import JobPosting
from app.schemas import SourceConfig
from tests.conftest import load_fixture

HIMALAYAS_SOURCE = SourceConfig(
    company="Himalayas", ats="himalayas", queries=[{"worldwide": "true"}]
)
REMOTIVE_SOURCE = SourceConfig(company="Remotive", ats="remotive")


@pytest.fixture
def shipped_filters(db: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    """Swap the permissive test filter config for the one that ships."""
    monkeypatch.setenv("FILTERS_FILE", str(BACKEND_ROOT / "config" / "filters.yaml"))
    get_settings.cache_clear()
    get_filters.cache_clear()


def mock_aggregators() -> None:
    """Serve both recorded aggregator fixtures. No live endpoint is touched."""
    respx.get(HIMALAYAS_URL).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=load_fixture("himalayas_search.json")
            if request.url.params.get("page") == "1"
            else load_fixture("himalayas_search_page2.json"),
        )
    )
    respx.get(REMOTIVE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("remotive_software_dev.json"))
    )


class CountingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify_new_jobs(self, jobs: list[JobPosting]) -> int:
        self.messages.extend(f"{job.company}|{job.title}" for job in jobs)
        return len(jobs)


async def all_rows(session_factory: Any) -> list[JobPosting]:
    async with session_factory() as session:
        return list((await session.execute(select(JobPosting))).scalars().all())


class TestNothingIsDropped:
    @respx.mock
    async def test_ineligible_jobs_are_stored_and_flagged_with_reasons(
        self, shipped_filters: None, session_factory: Any
    ) -> None:
        mock_aggregators()
        result = await run_ingest(
            companies=[HIMALAYAS_SOURCE, REMOTIVE_SOURCE], notify=False
        )

        rows = await all_rows(session_factory)
        # 5 Himalayas + 7 Remotive fixture jobs, all persisted.
        assert len(rows) == 12
        assert result.fetched == 12
        assert result.new == 12

        failed = [r for r in rows if not r.eligibility_pass]
        assert failed, "the fixtures include jobs that must fail"
        assert all(r.eligibility_reasons for r in failed), "every failure states why"
        assert result.eligible == sum(1 for r in rows if r.eligibility_pass)

    @respx.mock
    async def test_us_only_job_fails_on_location_not_pay(
        self, shipped_filters: None, session_factory: Any
    ) -> None:
        """Acceptance: a US-candidates-only remote job is flagged, with a location reason."""
        mock_aggregators()
        await run_ingest(companies=[HIMALAYAS_SOURCE], notify=False)

        rows = await all_rows(session_factory)
        # The Samsara posting: locationRestrictions ["United States"], USD pay.
        us_job = next(r for r in rows if r.company == "Samsara")

        assert us_job.eligibility_pass is False
        assert us_job.location_eligibility == ["US"]
        assert any(r.startswith("location_blocked") for r in us_job.eligibility_reasons)
        # Its pay was fine — only the location disqualified it.
        assert any(r.startswith("pay_ok") for r in us_job.eligibility_reasons)

    @respx.mock
    async def test_worldwide_usd_job_passes(
        self, shipped_filters: None, session_factory: Any
    ) -> None:
        """Acceptance: a worldwide-eligible USD job passes the filter."""
        mock_aggregators()
        await run_ingest(companies=[HIMALAYAS_SOURCE], notify=False)

        rows = await all_rows(session_factory)
        # The Aline posting: no location restrictions, USD 100k.
        passing = next(r for r in rows if r.company == "Aline")

        assert passing.eligibility_pass is True
        assert passing.location_eligibility == ["worldwide"]
        assert passing.salary_currency == "USD"

    @respx.mock
    async def test_india_job_without_a_salary_passes_and_is_flagged(
        self, shipped_filters: None, session_factory: Any
    ) -> None:
        """An unstated salary is not a disqualification, only a fact to surface.

        Roughly 84% of real postings state no pay at all, so blocking on it
        would discard most of the board. The row passes carrying an explicit
        `pay_unstated` reason, which is what the dashboard renders as
        "not stated".
        """
        mock_aggregators()
        await run_ingest(companies=[HIMALAYAS_SOURCE], notify=False)

        rows = await all_rows(session_factory)
        india_job = next(r for r in rows if r.company == "Alkira, Inc.")

        assert india_job.location_eligibility == ["IN"]
        assert india_job.eligibility_pass is True
        assert any(r.startswith("pay_unstated") for r in india_job.eligibility_reasons)
        assert india_job.salary_min is None and india_job.salary_max is None


class TestAlertGate:
    @respx.mock
    async def test_only_eligible_jobs_generate_alerts(
        self, shipped_filters: None, session_factory: Any
    ) -> None:
        """Acceptance: only `eligibility_pass = true` jobs generate alerts."""
        mock_aggregators()
        notifier = CountingNotifier()
        result = await run_ingest(
            companies=[HIMALAYAS_SOURCE, REMOTIVE_SOURCE], notifier=notifier
        )

        rows = await all_rows(session_factory)
        eligible = {f"{r.company}|{r.title}" for r in rows if r.eligibility_pass}
        ineligible = {f"{r.company}|{r.title}" for r in rows if not r.eligibility_pass}

        assert eligible, "the fixtures must include passing jobs"
        assert ineligible, "and failing ones"
        assert set(notifier.messages) == eligible
        assert not (set(notifier.messages) & ineligible)
        assert result.notified == len(eligible)
        # Every job was still recorded as new, alerted or not.
        assert result.new == len(rows)

    @respx.mock
    async def test_flipping_the_config_re_decides_existing_rows(
        self, db: None, session_factory: Any, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Editing filters.yaml must re-evaluate jobs whose content never changed."""
        strict = tmp_path / "strict.yaml"
        # A country no fixture job is eligible for, so nothing clears location.
        strict.write_text("allowed_locations: [JP]\n", encoding="utf-8")
        monkeypatch.setenv("FILTERS_FILE", str(strict))
        get_settings.cache_clear()
        get_filters.cache_clear()

        mock_aggregators()
        first = await run_ingest(companies=[HIMALAYAS_SOURCE], notify=False)
        assert first.eligible == 0, "no fixture job is eligible for JP"

        loose = tmp_path / "loose.yaml"
        loose.write_text("allowed_locations: [IN, worldwide]\n", encoding="utf-8")
        monkeypatch.setenv("FILTERS_FILE", str(loose))
        get_settings.cache_clear()
        get_filters.cache_clear()

        second = await run_ingest(companies=[HIMALAYAS_SOURCE], notify=False)
        assert second.new == 0, "no job changed, so nothing is newly discovered"
        assert second.eligible > 0, "but IN/worldwide jobs are now eligible"

        rows = await all_rows(session_factory)
        assert next(r for r in rows if r.company == "Aline").eligibility_pass is True
        # The US-only job stays blocked under either config.
        assert next(r for r in rows if r.company == "Samsara").eligibility_pass is False


class TestRemotiveAttribution:
    @respx.mock
    async def test_remotive_url_and_source_survive_ingestion(
        self, shipped_filters: None, session_factory: Any
    ) -> None:
        """Acceptance: Remotive jobs retain Remotive's canonical URL and attribution."""
        mock_aggregators()
        await run_ingest(companies=[REMOTIVE_SOURCE], notify=False)

        rows = [r for r in await all_rows(session_factory) if r.ats == "remotive"]
        assert rows

        fixture_urls = {j["url"] for j in load_fixture("remotive_software_dev.json")["jobs"]}
        for row in rows:
            assert row.apply_url in fixture_urls, "the stored link must be Remotive's own"
            assert row.apply_url.startswith("https://remotive.com/")
            assert row.ats == "remotive", "attribution travels on the source field"

    @respx.mock
    async def test_alert_text_names_remotive_as_the_source(
        self, shipped_filters: None, session_factory: Any
    ) -> None:
        from app.notify.telegram import _format_job

        mock_aggregators()
        await run_ingest(companies=[REMOTIVE_SOURCE], notify=False)

        rows = [r for r in await all_rows(session_factory) if r.eligibility_pass]
        # Exactly one fixture row is both a wanted role and India-eligible
        # (Kestrel Labs, worldwide). The rest are either not engineering
        # roles or US/CA-restricted, so this also pins rule 4 against the
        # shipped config end-to-end.
        assert [r.company for r in rows] == ["Kestrel Labs"]

        message = _format_job(rows[0])
        assert "Source: Remotive" in message
        assert rows[0].apply_url in message


class TestThrottle:
    @respx.mock
    async def test_a_throttled_source_is_not_contacted_again(
        self, shipped_filters: None, session_factory: Any
    ) -> None:
        """Remotive's terms cap request volume; the scheduler runs hourly."""
        mock_aggregators()
        capped = SourceConfig(
            company="Remotive", ats="remotive", min_fetch_interval_minutes=360
        )

        first = await run_ingest(companies=[capped], notify=False)
        assert first.fetched == 7

        second = await run_ingest(companies=[capped], notify=False)
        assert second.sources[0].throttled is True
        assert second.fetched == 0
        assert second.closed == 0, "a skipped fetch must never close anything"
        # The rows from the first run are untouched.
        assert len(await all_rows(session_factory)) == 7
