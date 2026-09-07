"""Ingest pipeline tests — these are the Phase 1 acceptance criteria."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import respx
from sqlalchemy import func, select

from app.adapters.ashby import API_URL as ASHBY_URL
from app.adapters.greenhouse import API_URL as GH_URL
from app.adapters.lever import API_URL as LEVER_URL
from app.ingest import run_ingest
from app.models import IngestRun, JobPosting
from tests.conftest import load_fixture

GH_ENDPOINT = GH_URL.format(token="stripe")
LEVER_ENDPOINT = LEVER_URL.format(slug="palantir")
ASHBY_ENDPOINT = ASHBY_URL.format(slug="linear")


class CountingNotifier:
    """Records exactly what would have been sent, one entry per message."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.calls = 0

    async def notify_new_jobs(self, jobs: list[JobPosting]) -> int:
        self.calls += 1
        self.messages.extend(f"{job.company}|{job.title}" for job in jobs)
        return len(jobs)


def mock_all_boards(
    greenhouse: Any = None,
    lever: Any = None,
    ashby: Any = None,
    *,
    greenhouse_status: int = 200,
    lever_status: int = 200,
    ashby_status: int = 200,
) -> dict[str, respx.Route]:
    """Serve the three recorded fixtures. No live endpoint is ever touched.

    Returns the routes so a caller can assert on call counts — the way to tell
    a skipped sweep from a sweep that simply found nothing new.
    """
    return {
        "greenhouse": respx.get(GH_ENDPOINT).mock(
            return_value=httpx.Response(
                greenhouse_status,
                json=greenhouse
                if greenhouse is not None
                else load_fixture("greenhouse_stripe.json"),
            )
        ),
        "lever": respx.get(LEVER_ENDPOINT).mock(
            return_value=httpx.Response(
                lever_status,
                json=lever if lever is not None else load_fixture("lever_palantir.json"),
            )
        ),
        "ashby": respx.get(ASHBY_ENDPOINT).mock(
            return_value=httpx.Response(
                ashby_status,
                json=ashby if ashby is not None else load_fixture("ashby_linear.json"),
            )
        ),
    }


async def count_jobs(session_factory: Any, **filters: Any) -> int:
    async with session_factory() as session:
        stmt = select(func.count()).select_from(JobPosting)
        for column, value in filters.items():
            stmt = stmt.where(getattr(JobPosting, column) == value)
        return await session.scalar(stmt) or 0


# ===========================================================================
# Acceptance criterion 1: re-running produces zero new events, zero duplicates
# ===========================================================================
class TestIdempotency:
    @respx.mock
    async def test_second_run_emits_no_new_jobs_and_no_duplicates(self, session_factory) -> None:
        mock_all_boards()

        notifier = CountingNotifier()
        first = await run_ingest(notifier=notifier)

        assert first.new == 9  # 3 greenhouse + 3 lever + 3 ashby
        assert first.fetched == 9
        assert first.closed == 0
        rows_after_first = await count_jobs(session_factory)
        assert rows_after_first == 9

        # All 9 are stored; only the eligible ones alert. Two of Linear's roles
        # are Europe-only, which the test filter does not allow, so they are
        # kept with `eligibility_pass = false` rather than dropped.
        assert first.eligible == 6
        assert await count_jobs(session_factory, eligibility_pass=False) == 3
        assert len(notifier.messages) == first.eligible

        notifier_two = CountingNotifier()
        second = await run_ingest(notifier=notifier_two)

        assert second.new == 0, "a re-run must not report new jobs"
        assert second.closed == 0, "nothing disappeared, so nothing may close"
        assert second.updated == 0, "unchanged content must not be rewritten"
        assert await count_jobs(session_factory) == rows_after_first, "duplicate rows created"
        assert notifier_two.messages == [], "a re-run must not alert"

    @respx.mock
    async def test_first_seen_at_is_preserved_across_runs(self, session_factory) -> None:
        mock_all_boards()
        await run_ingest(notify=False)

        async with session_factory() as session:
            before = {
                row.source_key: row.first_seen_at
                for row in (await session.execute(select(JobPosting))).scalars()
            }

        await run_ingest(notify=False)

        async with session_factory() as session:
            after = {
                row.source_key: (row.first_seen_at, row.last_seen_at)
                for row in (await session.execute(select(JobPosting))).scalars()
            }

        for key, first_seen in before.items():
            assert after[key][0] == first_seen, "first_seen_at must never move"
            assert after[key][1] > first_seen, "last_seen_at must be refreshed"

    @respx.mock
    async def test_duplicate_ids_within_one_feed_are_collapsed(self, session_factory) -> None:
        payload = load_fixture("greenhouse_stripe.json")
        payload["jobs"].append(copy.deepcopy(payload["jobs"][0]))  # same id twice
        mock_all_boards(greenhouse=payload)

        result = await run_ingest(notify=False)

        assert await count_jobs(session_factory, ats="greenhouse") == 3
        assert result.new == 9

    @respx.mock
    async def test_changed_content_updates_in_place(self, session_factory) -> None:
        mock_all_boards()
        await run_ingest(notify=False)

        payload = load_fixture("greenhouse_stripe.json")
        payload["jobs"][0]["title"] = "Retitled Role"
        mock_all_boards(greenhouse=payload)

        result = await run_ingest(notify=False)

        assert result.new == 0
        assert result.updated == 1
        assert await count_jobs(session_factory) == 9
        async with session_factory() as session:
            row = await session.scalar(
                select(JobPosting).where(JobPosting.source_key == "greenhouse:stripe:7954688")
            )
        assert row.title == "Retitled Role"


# ===========================================================================
# Acceptance criterion 2: a vanished job is closed, never deleted
# ===========================================================================
class TestClosureByDisappearance:
    @respx.mock
    async def test_removed_job_flips_to_closed_and_is_retained(self, session_factory) -> None:
        mock_all_boards()
        await run_ingest(notify=False)

        payload = load_fixture("greenhouse_stripe.json")
        removed = payload["jobs"].pop(0)
        removed_key = f"greenhouse:stripe:{removed['id']}"
        mock_all_boards(greenhouse=payload)

        result = await run_ingest(notify=False)

        assert result.closed == 1
        assert result.new == 0
        assert await count_jobs(session_factory) == 9, "closure must not delete the row"

        async with session_factory() as session:
            row = await session.scalar(
                select(JobPosting).where(JobPosting.source_key == removed_key)
            )
        assert row is not None
        assert row.status == "closed"
        assert row.closed_at is not None

    @respx.mock
    async def test_closure_is_scoped_to_its_own_source(self, session_factory) -> None:
        mock_all_boards()
        await run_ingest(notify=False)

        payload = load_fixture("greenhouse_stripe.json")
        payload["jobs"].pop(0)
        mock_all_boards(greenhouse=payload)
        await run_ingest(notify=False)

        # Other boards were untouched, so none of their jobs may close.
        assert await count_jobs(session_factory, ats="lever", status="open") == 3
        assert await count_jobs(session_factory, ats="ashby", status="open") == 3

    @respx.mock
    async def test_reappearing_job_reopens_without_duplicating(self, session_factory) -> None:
        mock_all_boards()
        await run_ingest(notify=False)

        reduced = load_fixture("greenhouse_stripe.json")
        removed = reduced["jobs"].pop(0)
        mock_all_boards(greenhouse=reduced)
        await run_ingest(notify=False)

        mock_all_boards()  # the job is listed again
        result = await run_ingest(notify=False)

        assert result.new == 0, "a reopened job is not a new job"
        assert await count_jobs(session_factory) == 9
        async with session_factory() as session:
            row = await session.scalar(
                select(JobPosting).where(
                    JobPosting.source_key == f"greenhouse:stripe:{removed['id']}"
                )
            )
        assert row.status == "open"
        assert row.closed_at is None

    @respx.mock
    async def test_empty_fetch_does_not_mass_close_an_ashby_board(self, session_factory) -> None:
        """A typo'd Ashby slug answers 200 + `{"jobs": []}` — must not close the board."""
        mock_all_boards()
        await run_ingest(notify=False)
        assert await count_jobs(session_factory, ats="ashby", status="open") == 3

        mock_all_boards(ashby={"jobs": [], "apiVersion": "1"})
        result = await run_ingest(notify=False)

        assert await count_jobs(session_factory, ats="ashby", status="open") == 3
        ashby_result = next(s for s in result.sources if s.ats == "ashby")
        assert ashby_result.skipped_closure_sweep is True
        assert result.closed == 0

    @respx.mock
    async def test_empty_lever_board_does_close(self, session_factory) -> None:
        """Lever 404s on a bad slug, so an empty array is a real signal."""
        mock_all_boards()
        await run_ingest(notify=False)

        mock_all_boards(lever=[])
        result = await run_ingest(notify=False)

        assert result.closed == 3
        assert await count_jobs(session_factory, ats="lever", status="open") == 0
        assert await count_jobs(session_factory, ats="lever") == 3


# ===========================================================================
# Acceptance criterion 3: one failing adapter must not stop the others
# ===========================================================================
class TestFailureIsolation:
    @respx.mock
    async def test_one_source_erroring_leaves_the_rest_persisted(self, session_factory) -> None:
        mock_all_boards(lever_status=500)

        result = await run_ingest(notify=False)

        assert result.errored == 1
        assert await count_jobs(session_factory, ats="greenhouse") == 3
        assert await count_jobs(session_factory, ats="ashby") == 3
        assert await count_jobs(session_factory, ats="lever") == 0

        failed = next(s for s in result.sources if s.ats == "lever")
        assert failed.ok is False
        assert failed.error
        assert all(s.ok for s in result.sources if s.ats != "lever")

    @respx.mock
    async def test_failed_source_does_not_close_its_existing_jobs(self, session_factory) -> None:
        """Closure requires a *successful* fetch — an error is not evidence."""
        mock_all_boards()
        await run_ingest(notify=False)
        assert await count_jobs(session_factory, ats="lever", status="open") == 3

        mock_all_boards(lever_status=500)
        await run_ingest(notify=False)

        assert await count_jobs(session_factory, ats="lever", status="open") == 3

    @respx.mock
    async def test_malformed_payload_is_isolated(self, session_factory) -> None:
        mock_all_boards(greenhouse={"unexpected": "shape"})

        result = await run_ingest(notify=False)

        assert result.errored == 1
        assert await count_jobs(session_factory) == 6

    @respx.mock
    async def test_notifier_failure_does_not_fail_the_run(self, session_factory) -> None:
        class ExplodingNotifier:
            async def notify_new_jobs(self, jobs: list[JobPosting]) -> int:
                raise RuntimeError("telegram is down")

        mock_all_boards()
        result = await run_ingest(notifier=ExplodingNotifier())

        assert result.new == 9
        assert result.notified == 0
        assert await count_jobs(session_factory) == 9


# ===========================================================================
# Acceptance criterion 4: a genuinely new job alerts exactly once
# ===========================================================================
class TestNewJobNotification:
    @respx.mock
    async def test_new_job_alerts_exactly_once_and_never_again(self, session_factory) -> None:
        mock_all_boards()
        await run_ingest(notify=False)  # establish the baseline silently

        payload = load_fixture("greenhouse_stripe.json")
        brand_new = copy.deepcopy(payload["jobs"][0])
        brand_new["id"] = 99999999
        brand_new["title"] = "Brand New Engineer"
        payload["jobs"].append(brand_new)
        mock_all_boards(greenhouse=payload)

        notifier = CountingNotifier()
        result = await run_ingest(notifier=notifier)

        assert result.new == 1
        assert notifier.messages == ["Stripe|Brand New Engineer"], "expected exactly one alert"
        assert result.notified == 1

        # A third run with the same board must stay silent.
        repeat = CountingNotifier()
        result_two = await run_ingest(notifier=repeat)
        assert result_two.new == 0
        assert repeat.messages == []

    @respx.mock
    async def test_new_job_is_visible_in_the_jobs_query(self, session_factory) -> None:
        mock_all_boards()
        await run_ingest(notify=False)

        payload = load_fixture("greenhouse_stripe.json")
        brand_new = copy.deepcopy(payload["jobs"][0])
        brand_new["id"] = 12345678
        brand_new["title"] = "Freshly Posted Role"
        payload["jobs"].append(brand_new)
        mock_all_boards(greenhouse=payload)
        await run_ingest(notify=False)

        async with session_factory() as session:
            row = await session.scalar(
                select(JobPosting).where(JobPosting.source_key == "greenhouse:stripe:12345678")
            )
        assert row is not None
        assert row.status == "open"
        assert row.title == "Freshly Posted Role"

    @respx.mock
    async def test_reopened_job_does_not_realert(self, session_factory) -> None:
        mock_all_boards()
        await run_ingest(notify=False)

        reduced = load_fixture("greenhouse_stripe.json")
        reduced["jobs"].pop(0)
        mock_all_boards(greenhouse=reduced)
        await run_ingest(notify=False)

        mock_all_boards()
        notifier = CountingNotifier()
        await run_ingest(notifier=notifier)

        assert notifier.messages == []


# ===========================================================================
# Run bookkeeping
# ===========================================================================
class TestRunAccounting:
    @respx.mock
    async def test_run_row_records_per_source_counts(self, session_factory) -> None:
        mock_all_boards(lever_status=500)
        result = await run_ingest(notify=False)

        async with session_factory() as session:
            run = await session.get(IngestRun, result.run_id)

        assert run is not None
        assert run.finished_at is not None
        assert run.new == 6
        assert run.errored == 1
        assert len(run.sources) == 3
        assert {s["ats"] for s in run.sources} == {"greenhouse", "lever", "ashby"}

    @respx.mock
    async def test_run_with_no_configured_sources(self, session_factory) -> None:
        result = await run_ingest(companies=[], notify=False)
        assert result.fetched == 0
        assert result.sources == []
        assert await count_jobs(session_factory) == 0
