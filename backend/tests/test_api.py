"""API tests — filters, detail, manual ingest trigger."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import update

from app.api import router
from app.db import session_scope
from app.ingest import run_ingest
from app.ingest_state import tracker
from app.models import IngestRun
from tests.test_ingest import mock_all_boards


async def wait_for_idle(client: httpx.AsyncClient, timeout: float = 15.0) -> dict:
    """Poll /ingest/status the way the dashboard does, until the sweep lands."""
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = (await client.get("/ingest/status")).json()
        if body["state"] != "running":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"ingest still running after {timeout}s: {body}")


@pytest.fixture
async def client(db: None) -> AsyncIterator[httpx.AsyncClient]:  # noqa: ARG001
    """App without the lifespan hook, so no scheduler starts during tests."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture
async def seeded(client: httpx.AsyncClient) -> httpx.AsyncClient:
    with respx.mock:
        mock_all_boards()
        await run_ingest(notify=False)
    return client


class TestHealth:
    async def test_health_reports_counts(self, seeded: httpx.AsyncClient) -> None:
        response = await seeded.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["jobs_total"] == 9
        assert body["jobs_open"] == 9
        assert "greenhouse" in body["supported_ats"]


class TestListJobs:
    async def test_returns_open_jobs_by_default(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/jobs")).json()
        assert body["total"] == 9
        assert len(body["items"]) == 9
        assert {item["status"] for item in body["items"]} == {"open"}

    async def test_filter_by_company(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/jobs", params={"company": "Stripe"})).json()
        assert body["total"] == 3
        assert {item["company"] for item in body["items"]} == {"Stripe"}

    async def test_filter_by_company_is_case_insensitive(self, seeded: httpx.AsyncClient) -> None:
        assert (await seeded.get("/jobs", params={"company": "stripe"})).json()["total"] == 3

    async def test_filter_by_multiple_companies(self, seeded: httpx.AsyncClient) -> None:
        response = await seeded.get("/jobs", params=[("company", "Stripe"), ("company", "Linear")])
        assert response.json()["total"] == 6

    async def test_filter_by_ats(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/jobs", params={"ats": "lever"})).json()
        assert body["total"] == 3
        assert {item["ats"] for item in body["items"]} == {"lever"}

    async def test_filter_by_remote(self, seeded: httpx.AsyncClient) -> None:
        remote = (await seeded.get("/jobs", params={"remote": "true"})).json()
        onsite = (await seeded.get("/jobs", params={"remote": "false"})).json()
        assert remote["total"] + onsite["total"] == 9
        assert all(item["remote"] for item in remote["items"])

    async def test_keyword_search_spans_title_company_and_description(
        self, seeded: httpx.AsyncClient
    ) -> None:
        body = (await seeded.get("/jobs", params={"q": "Engineer"})).json()
        assert body["total"] >= 1

        # A hit may land in any of the three searched fields, so verify against
        # the detail payload rather than assuming the title matched.
        for item in body["items"]:
            detail = (await seeded.get(f"/jobs/{item['id']}")).json()
            haystack = " ".join(
                [detail["title"], detail["company"], detail["description_text"] or ""]
            ).lower()
            assert "engineer" in haystack

    async def test_keyword_search_matches_description(self, seeded: httpx.AsyncClient) -> None:
        # "Palantir" appears in the JD body of the Lever fixtures.
        body = (await seeded.get("/jobs", params={"q": "Palantir"})).json()
        assert body["total"] >= 1

    async def test_keyword_search_excludes_non_matches(self, seeded: httpx.AsyncClient) -> None:
        assert (await seeded.get("/jobs", params={"q": "zzzznotarealkeyword"})).json()["total"] == 0

    async def test_status_filter(self, seeded: httpx.AsyncClient) -> None:
        assert (await seeded.get("/jobs", params={"status": "closed"})).json()["total"] == 0
        assert (await seeded.get("/jobs", params={"status": "any"})).json()["total"] == 9

    async def test_pagination(self, seeded: httpx.AsyncClient) -> None:
        page_one = (await seeded.get("/jobs", params={"limit": 4, "offset": 0})).json()
        page_two = (await seeded.get("/jobs", params={"limit": 4, "offset": 4})).json()

        assert page_one["total"] == 9 and len(page_one["items"]) == 4
        assert len(page_two["items"]) == 4
        ids_one = {item["id"] for item in page_one["items"]}
        ids_two = {item["id"] for item in page_two["items"]}
        assert ids_one.isdisjoint(ids_two), "pagination must not repeat rows"

    async def test_sort_by_posted_at(self, seeded: httpx.AsyncClient) -> None:
        items = (await seeded.get("/jobs", params={"sort": "posted_at", "order": "desc"})).json()[
            "items"
        ]
        dates = [item["posted_at"] for item in items if item["posted_at"]]
        assert dates == sorted(dates, reverse=True)

    async def test_filters_compose(self, seeded: httpx.AsyncClient) -> None:
        body = (
            await seeded.get("/jobs", params={"ats": "ashby", "remote": "true", "q": "engineer"})
        ).json()
        assert body["total"] >= 1
        for item in body["items"]:
            assert item["ats"] == "ashby"
            assert item["remote"] is True

        # Each filter must actually narrow the result set.
        unfiltered = (await seeded.get("/jobs")).json()["total"]
        assert body["total"] < unfiltered

    async def test_rejects_bad_limit(self, seeded: httpx.AsyncClient) -> None:
        assert (await seeded.get("/jobs", params={"limit": 9999})).status_code == 422


class TestJobDetail:
    async def test_returns_full_description(self, seeded: httpx.AsyncClient) -> None:
        job_id = (await seeded.get("/jobs")).json()["items"][0]["id"]
        body = (await seeded.get(f"/jobs/{job_id}")).json()

        assert body["id"] == job_id
        assert body["description_text"]
        assert body["apply_url"].startswith("https://")
        assert body["raw_json"] is None, "raw payload is opt-in"

    async def test_include_raw(self, seeded: httpx.AsyncClient) -> None:
        job_id = (await seeded.get("/jobs")).json()["items"][0]["id"]
        body = (await seeded.get(f"/jobs/{job_id}", params={"include_raw": "true"})).json()
        assert body["raw_json"] is not None

    async def test_missing_job_is_404(self, seeded: httpx.AsyncClient) -> None:
        assert (await seeded.get("/jobs/999999")).status_code == 404


class TestIngestEndpoint:
    @respx.mock
    async def test_manual_trigger_runs_and_reports(self, client: httpx.AsyncClient) -> None:
        mock_all_boards()
        response = await client.post("/ingest/run", params={"notify": "false"})

        assert response.status_code == 200
        body = response.json()
        assert body["new"] == 9
        assert len(body["sources"]) == 3
        assert (await client.get("/jobs")).json()["total"] == 9

    @respx.mock
    async def test_second_trigger_is_idempotent(self, client: httpx.AsyncClient) -> None:
        mock_all_boards()
        await client.post("/ingest/run", params={"notify": "false"})
        second = (await client.post("/ingest/run", params={"notify": "false"})).json()

        assert second["new"] == 0
        assert (await client.get("/jobs")).json()["total"] == 9

    async def test_runs_history(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/ingest/runs")).json()
        assert len(body) == 1
        assert body[0]["new"] == 9
        assert len(body[0]["sources"]) == 3


class TestRefreshEndpoint:
    """The page-load path: sweep once per 24h window, in the background, with progress."""

    @respx.mock
    async def test_starts_a_background_sweep_and_reports_progress(
        self, client: httpx.AsyncClient
    ) -> None:
        mock_all_boards()
        started = (await client.post("/ingest/refresh")).json()

        # Returns before the sweep finishes — that is the whole point.
        assert started["state"] == "running"
        assert started["skipped"] is False

        final = await wait_for_idle(client)
        assert final["state"] == "idle"
        assert final["sources_done"] == final["sources_total"] == 3
        assert {source["state"] for source in final["sources"]} == {"done"}
        assert final["result"]["new"] == 9
        assert (await client.get("/jobs")).json()["total"] == 9

    @respx.mock
    async def test_a_second_visit_inside_the_window_does_not_hit_the_boards(
        self, client: httpx.AsyncClient
    ) -> None:
        """The default gate: no `fresh_since`, so the backend's 24h window applies."""
        route = mock_all_boards()["greenhouse"]
        await client.post("/ingest/refresh")
        await wait_for_idle(client)
        calls_after_first = route.call_count

        second = (await client.post("/ingest/refresh")).json()

        assert second["state"] == "fresh"
        assert second["skipped"] is True
        assert second["last_finished_at"] is not None
        assert route.call_count == calls_after_first

    @respx.mock
    async def test_reload_at_a_calendar_boundary_still_does_not_sweep(
        self, client: httpx.AsyncClient
    ) -> None:
        """The window is rolling: a sweep at 23:00 is not stale at 00:05.

        The old gate was the viewer's local midnight, which made every first
        visit of a new day re-sweep however recently the boards were fetched.
        """
        route = mock_all_boards()["greenhouse"]
        await client.post("/ingest/refresh")
        await wait_for_idle(client)
        calls_after_first = route.call_count

        # Five minutes past a midnight that falls just after the stored run.
        just_after_midnight = datetime.now(UTC) - timedelta(minutes=5)
        body = (
            await client.post(
                "/ingest/refresh", params={"fresh_since": just_after_midnight.isoformat()}
            )
        ).json()

        assert body["state"] == "fresh"
        assert route.call_count == calls_after_first

    @respx.mock
    async def test_sweeps_again_once_the_window_has_lapsed(
        self, client: httpx.AsyncClient
    ) -> None:
        route = mock_all_boards()["greenhouse"]
        await client.post("/ingest/refresh")
        await wait_for_idle(client)
        calls_after_first = route.call_count

        # Stand in for "24h have passed" by asking for data newer than now.
        stale_cutoff = datetime.now(UTC) + timedelta(seconds=1)
        second = (
            await client.post("/ingest/refresh", params={"fresh_since": stale_cutoff.isoformat()})
        ).json()

        assert second["state"] == "running"
        assert second["skipped"] is False
        await wait_for_idle(client)
        assert route.call_count > calls_after_first

    @respx.mock
    async def test_an_interrupted_run_does_not_count_as_fresh(
        self, client: httpx.AsyncClient
    ) -> None:
        """`finished_at IS NULL` means the sweep died mid-flight — not fresh data."""
        route = mock_all_boards()["greenhouse"]
        await client.post("/ingest/refresh")
        await wait_for_idle(client)
        calls_after_first = route.call_count

        # Clear the finish stamp, as a process killed mid-sweep would leave it.
        async with session_scope() as session:
            await session.execute(update(IngestRun).values(finished_at=None))

        body = (await client.post("/ingest/refresh")).json()
        assert body["state"] == "running"
        await wait_for_idle(client)
        assert route.call_count > calls_after_first

    @respx.mock
    async def test_a_fresh_reload_during_a_running_sweep_renders_immediately(
        self, client: httpx.AsyncClient
    ) -> None:
        """Fresh data wins over a run in flight.

        Checking `busy` first meant a page load that landed during a scheduler
        tick was held behind the whole sweep, which is what made the dashboard
        look like it re-fetched on every refresh.
        """
        mock_all_boards()
        await client.post("/ingest/refresh")
        await wait_for_idle(client)

        async with tracker.lock:  # stand in for the scheduler sweeping now
            body = (await client.post("/ingest/refresh")).json()

        assert body["state"] == "fresh"
        assert body["skipped"] is True

    @respx.mock
    async def test_force_ignores_freshness(self, client: httpx.AsyncClient) -> None:
        route = mock_all_boards()["greenhouse"]
        await client.post("/ingest/refresh")
        await wait_for_idle(client)
        calls_after_first = route.call_count

        forced = (await client.post("/ingest/refresh", params={"force": "true"})).json()
        assert forced["state"] == "running"
        result = await wait_for_idle(client)
        assert route.call_count > calls_after_first
        # A forced re-sweep is still idempotent: no duplicates, no new-job events.
        assert result["result"]["new"] == 0
        assert (await client.get("/jobs")).json()["total"] == 9

    async def test_attaches_to_a_run_already_in_progress(
        self, client: httpx.AsyncClient
    ) -> None:
        """With no stored run there is nothing fresh, so `busy` decides."""
        async with tracker.lock:  # stand in for the scheduler holding it
            body = (await client.post("/ingest/refresh")).json()

        assert body["state"] == "running"
        assert body["reason"] == "a sweep was already in progress"
        # No second sweep was queued behind the one already running.
        assert tracker.running is False

    @respx.mock
    async def test_a_failing_board_does_not_block_the_others(
        self, client: httpx.AsyncClient
    ) -> None:
        mock_all_boards(lever_status=500)

        await client.post("/ingest/refresh")
        final = await wait_for_idle(client)

        by_ats = {source["ats"]: source for source in final["sources"]}
        assert by_ats["lever"]["state"] == "failed"
        assert by_ats["lever"]["error"]
        assert by_ats["greenhouse"]["state"] == "done"
        assert final["state"] == "idle"


class TestCompaniesEndpoint:
    async def test_lists_configured_companies(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/companies")).json()
        # The unsupported-ATS entry is filtered out; the disabled one is shown.
        names = {entry["company"] for entry in body}
        assert {"Stripe", "Palantir", "Linear", "Disabled Co"} == names
        assert next(e for e in body if e["company"] == "Disabled Co")["enabled"] is False
