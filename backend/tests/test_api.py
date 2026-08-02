"""API tests — filters, detail, manual ingest trigger."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.api import router
from app.ingest import run_ingest
from tests.test_ingest import mock_all_boards


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


class TestCompaniesEndpoint:
    async def test_lists_configured_companies(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/companies")).json()
        # The unsupported-ATS entry is filtered out; the disabled one is shown.
        names = {entry["company"] for entry in body}
        assert {"Stripe", "Palantir", "Linear", "Disabled Co"} == names
        assert next(e for e in body if e["company"] == "Disabled Co")["enabled"] is False
