"""Tests for the dashboard-facing API additions (Phase 2a)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.test_api import client, seeded  # noqa: F401 - fixture re-export


class TestFacets:
    async def test_returns_options_with_counts(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/meta/facets")).json()

        companies = {entry["value"]: entry["count"] for entry in body["companies"]}
        assert companies == {"Stripe": 3, "Palantir": 3, "Linear": 3}

        ats = {entry["value"]: entry["count"] for entry in body["ats"]}
        assert ats == {"greenhouse": 3, "lever": 3, "ashby": 3}

    async def test_departments_exclude_nulls(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/meta/facets")).json()
        assert all(entry["value"] for entry in body["departments"])
        assert sum(entry["count"] for entry in body["departments"]) <= 9

    async def test_totals(self, seeded: httpx.AsyncClient) -> None:
        totals = (await seeded.get("/meta/facets")).json()["totals"]
        assert totals["open"] == 9
        assert totals["closed"] == 0
        assert totals["matching"] == 9
        assert 0 <= totals["remote"] <= 9

    async def test_new_since_counts_recent_discoveries(
        self, seeded: httpx.AsyncClient
    ) -> None:
        after = (await seeded.get("/meta/facets", params={"since": "2000-01-01T00:00:00Z"})).json()
        assert after["totals"]["new_since"] == 9

        future = (
            await seeded.get("/meta/facets", params={"since": "2099-01-01T00:00:00Z"})
        ).json()
        assert future["totals"]["new_since"] == 0

    async def test_counts_match_the_jobs_endpoint(self, seeded: httpx.AsyncClient) -> None:
        """Facet counts and list results must agree, or filters look broken."""
        body = (await seeded.get("/meta/facets")).json()
        for entry in body["companies"]:
            listed = (
                await seeded.get("/jobs", params={"company": entry["value"], "limit": 500})
            ).json()
            assert listed["total"] == entry["count"], entry["value"]


class TestSearchScope:
    async def test_title_scope_is_precise(self, seeded: httpx.AsyncClient) -> None:
        broad = (await seeded.get("/jobs", params={"q": "Palantir"})).json()
        titled = (await seeded.get("/jobs", params={"q": "Palantir", "q_scope": "title"})).json()

        # "Palantir" appears in JD boilerplate but in no job title.
        assert broad["total"] > 0
        assert titled["total"] == 0

    async def test_title_scope_still_matches_titles(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/jobs", params={"q": "Engineer", "q_scope": "title"})).json()
        assert body["total"] >= 1
        assert all("engineer" in item["title"].lower() for item in body["items"])


class TestFirstSeenAfter:
    async def test_filters_by_discovery_time(self, seeded: httpx.AsyncClient) -> None:
        everything = (
            await seeded.get("/jobs", params={"first_seen_after": "2000-01-01T00:00:00Z"})
        ).json()
        assert everything["total"] == 9

        none = (
            await seeded.get("/jobs", params={"first_seen_after": "2099-01-01T00:00:00Z"})
        ).json()
        assert none["total"] == 0

    async def test_naive_timestamp_is_treated_as_utc(self, seeded: httpx.AsyncClient) -> None:
        body = (await seeded.get("/jobs", params={"first_seen_after": "2000-01-01T00:00:00"})).json()
        assert body["total"] == 9


class TestDefaultOrdering:
    async def test_ties_on_first_seen_fall_back_to_posted_at(
        self, seeded: httpx.AsyncClient
    ) -> None:
        """A bulk first ingest gives every row the same `first_seen_at`.

        Without a `posted_at` tie-break the default view degenerates into
        "whatever was inserted last", which showed a single company's jobs.
        """
        body = (await seeded.get("/jobs")).json()
        stamps = {item["first_seen_at"] for item in body["items"]}
        assert len(stamps) == 1, "fixture setup: all rows should share a run stamp"

        posted = [item["posted_at"] for item in body["items"] if item["posted_at"]]
        assert posted == sorted(posted, reverse=True)

    async def test_default_page_is_not_single_company(
        self, seeded: httpx.AsyncClient
    ) -> None:
        body = (await seeded.get("/jobs", params={"limit": 9})).json()
        assert len({item["company"] for item in body["items"]}) > 1

    async def test_pagination_covers_every_row_exactly_once(
        self, seeded: httpx.AsyncClient
    ) -> None:
        seen: list[int] = []
        for offset in range(0, 9, 2):
            page = (await seeded.get("/jobs", params={"limit": 2, "offset": offset})).json()
            seen.extend(item["id"] for item in page["items"])
        assert len(seen) == len(set(seen)) == 9


class TestDepartmentFilter:
    async def test_exact_match_and_repeatable(self, seeded: httpx.AsyncClient) -> None:
        departments = (await seeded.get("/meta/facets")).json()["departments"]
        first = departments[0]["value"]

        body = (await seeded.get("/jobs", params={"department": first})).json()
        assert body["total"] == departments[0]["count"]
        assert all(item["department"] == first for item in body["items"])

        if len(departments) > 1:
            second = departments[1]["value"]
            both = (
                await seeded.get("/jobs", params=[("department", first), ("department", second)])
            ).json()
            assert both["total"] == departments[0]["count"] + departments[1]["count"]


class TestEligibilityGate:
    """The dashboard's default view hides rows the eligibility filter rejected.

    `/jobs` has always accepted `eligibility_pass`; `/meta/facets` did not, and
    that gap is the bug this class pins. A facet list built without the gate
    offers companies whose every posting is filtered out, so selecting one
    yields an empty table and the filter reads as broken.
    """

    async def test_jobs_and_facets_agree_under_the_gate(
        self, seeded: httpx.AsyncClient
    ) -> None:
        params = {"eligibility_pass": "true"}
        jobs = (await seeded.get("/jobs", params={**params, "limit": "200"})).json()
        facets = (await seeded.get("/meta/facets", params=params)).json()

        assert facets["totals"]["matching"] == jobs["total"]
        # Every facet option must be reachable: its count is drawn from the
        # same gated set the table is.
        by_company: dict[str, int] = {}
        for job in jobs["items"]:
            by_company[job["company"]] = by_company.get(job["company"], 0) + 1
        assert {e["value"]: e["count"] for e in facets["companies"]} == by_company

    async def test_gate_off_is_a_superset_of_gate_on(
        self, seeded: httpx.AsyncClient
    ) -> None:
        ungated = (await seeded.get("/meta/facets")).json()["totals"]["matching"]
        gated = (
            await seeded.get("/meta/facets", params={"eligibility_pass": "true"})
        ).json()["totals"]["matching"]
        assert gated <= ungated

    async def test_the_eligible_split_describes_the_board_not_the_query(
        self, seeded: httpx.AsyncClient
    ) -> None:
        """`eligible`/`ineligible` must read the same with the gate on or off.

        Deriving them from the gated base would report ineligible = 0 whenever
        the dashboard sits in its default state — a statistic that only ever
        restates the query that produced it.
        """
        ungated = (await seeded.get("/meta/facets")).json()["totals"]
        gated = (
            await seeded.get("/meta/facets", params={"eligibility_pass": "true"})
        ).json()["totals"]

        assert gated["eligible"] == ungated["eligible"]
        assert gated["ineligible"] == ungated["ineligible"]
        # ...while `matching` DOES follow the query, because it describes the
        # rows on screen.
        assert gated["matching"] == gated["eligible"]
