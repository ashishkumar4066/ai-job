"""Application tracking and the Dashboard's numbers.

Two behaviours carry most of the weight here.

`test_marking_twice_does_not_reset_an_application_in_progress` — the Apply
button calls `POST /applications` every time it is pressed, and re-opening a
form to check a question must not drag "interviewing" back to "applied" or
restamp a three-week-old application as today's.

`test_silence_is_derived_not_stored` — "no reply in 30 days" is recomputed on
every read, so every *stored* status stays something the user asserted. The
Dashboard offers to mark those rows ghosted; nothing marks them automatically.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app import applications
from app.applications import (
    ApplicationError,
    dashboard,
    days_silent,
    is_silent,
    list_applications,
    mark_applied,
    set_status,
    unmark,
)
from app.main import app
from app.models import JobPosting, utcnow


def _job(n: int, **over: object) -> JobPosting:
    now = utcnow()
    base: dict[str, object] = {
        "source_key": f"test:acme:{n}",
        "source_id": "test:acme",
        "ats": "lever",
        "company": f"Company {n}",
        "title": "Senior Backend Engineer",
        "locations": ["Remote"],
        "remote": True,
        "apply_url": f"https://jobs.lever.co/acme/{n}",
        "description_text": "Python, FastAPI, 5+ years. " * 8,
        "posted_at": now - timedelta(days=3),
        "first_seen_at": now - timedelta(days=3),
        "last_seen_at": now,
        "status": "open",
        "content_hash": f"h{n}",
        "eligibility_pass": True,
    }
    base.update(over)
    return JobPosting(**base)  # type: ignore[arg-type]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------
# Marking
# --------------------------------------------------------------------------


async def test_marking_records_an_application(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        application, created = await mark_applied(session, 1, source="apply_click")
    assert created is True
    assert application.status == "applied"
    assert application.source == "apply_click"
    assert application.history == [
        {"status": "applied", "at": application.applied_at.isoformat()}
    ]


async def test_marking_twice_does_not_reset_an_application_in_progress(
    session_factory,
) -> None:
    """The Apply button fires on every press; it must be idempotent."""
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        first, _ = await mark_applied(session, 1)
        await set_status(session, 1, status="interviewing")
        original_date = first.applied_at

        again, created = await mark_applied(session, 1, source="apply_click")
    assert created is False
    assert again.status == "interviewing"
    assert again.applied_at == original_date


async def test_applying_to_a_missing_job_is_refused(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(ApplicationError, match="not found"):
            await mark_applied(session, 999)


async def test_an_unknown_source_is_refused(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        with pytest.raises(ApplicationError, match="source"):
            await mark_applied(session, 1, source="telepathy")


async def test_one_application_per_posting(session_factory) -> None:
    """Applying twice to one posting is a mistake, not two applications."""
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        await mark_applied(session, 1)
        rows = await list_applications(session)
    assert len(rows) == 1


# --------------------------------------------------------------------------
# Moving and undoing
# --------------------------------------------------------------------------


async def test_each_move_is_recorded_with_its_date(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        await set_status(session, 1, status="screening")
        application = await set_status(session, 1, status="interviewing")
    assert [entry["status"] for entry in application.history] == [
        "applied",
        "screening",
        "interviewing",
    ]


async def test_setting_the_same_status_does_not_restart_the_silence_clock(
    session_factory,
) -> None:
    """Otherwise a stray save would hide an application that has gone quiet."""
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        stored = await applications.get_application(session, 1)
        assert stored is not None
        stored.status_changed_at = utcnow() - timedelta(days=45)
        await session.commit()

        same = await set_status(session, 1, status="applied")
    assert days_silent(same) >= 45
    assert len(same.history or []) == 1


async def test_notes_can_be_edited_without_moving_the_status(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        application = await set_status(session, 1, notes="  recruiter said Q2  ")
    assert application.status == "applied"
    assert application.notes == "recruiter said Q2"


async def test_an_unknown_status_is_refused_by_name(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        with pytest.raises(ApplicationError, match="interviewing"):
            await set_status(session, 1, status="vibing")


async def test_moving_a_job_never_applied_to_is_refused(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        with pytest.raises(ApplicationError, match="no application"):
            await set_status(session, 1, status="offer")


async def test_undo_removes_it_from_the_counts_entirely(session_factory) -> None:
    """A misclicked Apply must leave no trace, not a 'withdrawn' row."""
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        assert await unmark(session, 1) is True
        assert await applications.get_application(session, 1) is None
        assert (await dashboard(session)).total_applications == 0


async def test_undoing_nothing_is_not_an_error(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        assert await unmark(session, 1) is False


# --------------------------------------------------------------------------
# Silence
# --------------------------------------------------------------------------


async def test_silence_is_derived_not_stored(session_factory) -> None:
    """Nothing writes `ghosted` on a timer; the status stays the user's."""
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        stored = await applications.get_application(session, 1)
        assert stored is not None
        stored.status_changed_at = utcnow() - timedelta(days=40)
        await session.commit()

        assert is_silent(stored) is True
        assert stored.status == "applied"  # NOT rewritten to ghosted
        assert (await dashboard(session)).silent == 1


async def test_a_closed_application_is_never_silent(session_factory) -> None:
    """A rejection from a year ago is not awaiting anything."""
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        await set_status(session, 1, status="rejected")
        stored = await applications.get_application(session, 1)
        assert stored is not None
        stored.status_changed_at = utcnow() - timedelta(days=300)
        await session.commit()
    assert is_silent(stored) is False


async def test_silence_counts_from_the_last_move_not_the_application_date(
    session_factory,
) -> None:
    """A reply then silence restarts the clock; otherwise an active interview
    process would be reported as stale on day 31."""
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        stored = await applications.get_application(session, 1)
        assert stored is not None
        stored.applied_at = utcnow() - timedelta(days=90)
        await session.commit()
        moved = await set_status(session, 1, status="interviewing")
    assert days_silent(moved) == 0
    assert is_silent(moved) is False


async def test_silent_only_filters_the_list(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([_job(1), _job(2)])
        await session.commit()
        await mark_applied(session, 1)
        await mark_applied(session, 2)
        stale = await applications.get_application(session, 1)
        assert stale is not None
        stale.status_changed_at = utcnow() - timedelta(days=60)
        await session.commit()

        rows = await list_applications(session, silent_only=True)
    assert [job.id for _a, job in rows] == [1]


# --------------------------------------------------------------------------
# The Dashboard
# --------------------------------------------------------------------------


async def test_every_status_appears_even_at_zero(session_factory) -> None:
    """A chart with missing keys renders gaps rather than zeroes."""
    async with session_factory() as session:
        data = await dashboard(session)
    assert list(data.by_status) == list(applications.STATUSES)
    assert set(data.by_status.values()) == {0}


async def test_awaiting_is_applied_with_no_response(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([_job(n) for n in range(1, 5)])
        await session.commit()
        for n in range(1, 5):
            await mark_applied(session, n)
        await set_status(session, 3, status="interviewing")
        await set_status(session, 4, status="rejected")
        data = await dashboard(session)

    assert data.awaiting == 2
    assert data.total_applications == 4
    # Active excludes the closed ones, so it is what is still in play.
    assert data.active == 3


async def test_response_rate_counts_a_rejection_after_an_interview(
    session_factory,
) -> None:
    """Read off the history, not the current status: being rejected at the final
    round is still a response, and the rate measures whether anyone replied."""
    async with session_factory() as session:
        session.add_all([_job(1), _job(2)])
        await session.commit()
        await mark_applied(session, 1)
        await mark_applied(session, 2)
        await set_status(session, 1, status="interviewing")
        await set_status(session, 1, status="rejected")
        data = await dashboard(session)
    assert data.response_rate == 0.5


async def test_response_rate_is_none_with_no_applications(session_factory) -> None:
    """0.0 would read as "nobody replies", which is a different claim."""
    async with session_factory() as session:
        assert (await dashboard(session)).response_rate is None


async def test_recent_windows_exclude_older_applications(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([_job(1), _job(2)])
        await session.commit()
        await mark_applied(session, 1)
        await mark_applied(session, 2)
        old = await applications.get_application(session, 2)
        assert old is not None
        old.applied_at = utcnow() - timedelta(days=50)
        await session.commit()
        data = await dashboard(session)
    assert data.applied_last_7d == 1
    assert data.applied_last_30d == 1


async def test_pipeline_health_reports_the_board(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                _job(1),
                _job(2, eligibility_pass=False),
                _job(3, status="closed"),
                _job(4, posted_at=utcnow() - timedelta(days=90)),
            ]
        )
        await session.commit()
        data = await dashboard(session)
    assert data.jobs_open == 3
    assert data.jobs_eligible == 2
    # The 90-day-old posting is eligible but outside the 30-day window.
    assert data.jobs_fresh == 1


async def test_the_llm_budget_names_its_provider_and_cap(session_factory) -> None:
    async with session_factory() as session:
        data = await dashboard(session)
    assert data.provider
    assert data.model
    # Every supported provider caps tokens per day; the panel needs the divisor.
    assert data.tokens_per_day is None or data.tokens_per_day > 0


async def test_activity_covers_a_full_quarter_of_weeks(session_factory) -> None:
    """Missing weeks would make the chart lie about a quiet stretch."""
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
        await mark_applied(session, 1)
        data = await dashboard(session)
    assert len(data.activity) == applications.ACTIVITY_WEEKS
    assert [point.week for point in data.activity] == sorted(p.week for p in data.activity)
    assert sum(point.applications for point in data.activity) == 1
    assert sum(point.jobs_found for point in data.activity) == 1


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


async def test_the_apply_click_flow_over_http(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([_job(1), _job(2)])
        await session.commit()

    async with _client() as client:
        marked = await client.post("/applications", json={"job_id": 1})
        assert marked.status_code == 200, marked.text
        assert marked.json()["status"] == "applied"
        assert marked.json()["source"] == "apply_click"
        assert marked.json()["company"] == "Company 1"

        # Idempotent: the button fires again on a second visit to the form.
        again = await client.post("/applications", json={"job_id": 1})
        assert again.json()["applied_at"] == marked.json()["applied_at"]

        moved = await client.patch("/applications/1", json={"status": "screening"})
        assert moved.status_code == 200, moved.text
        assert moved.json()["status"] == "screening"

        listed = await client.get("/applications")
        assert listed.json()["total"] == 1

        board = await client.get("/dashboard")
        assert board.status_code == 200, board.text
        assert board.json()["by_status"]["screening"] == 1
        assert board.json()["silent_after_days"] == applications.SILENT_AFTER_DAYS

        removed = await client.delete("/applications/1")
        assert removed.json() == {"removed": True}
        assert (await client.get("/dashboard")).json()["total_applications"] == 0


async def test_a_job_row_carries_its_application_status(session_factory) -> None:
    """The row needs it to render the button's state without a second request."""
    async with session_factory() as session:
        session.add_all([_job(1), _job(2)])
        await session.commit()

    async with _client() as client:
        await client.post("/applications", json={"job_id": 1})
        rows = (await client.get("/jobs")).json()["items"]
        by_id = {row["id"]: row["application_status"] for row in rows}
        assert by_id[1] == "applied"
        assert by_id[2] is None

        detail = await client.get("/jobs/1")
        assert detail.json()["application_status"] == "applied"


async def test_patching_an_unknown_status_is_a_422(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
    async with _client() as client:
        await client.post("/applications", json={"job_id": 1})
        bad = await client.patch("/applications/1", json={"status": "vibing"})
    assert bad.status_code == 422


async def test_patching_an_untracked_job_is_a_404(session_factory) -> None:
    async with session_factory() as session:
        session.add(_job(1))
        await session.commit()
    async with _client() as client:
        missing = await client.patch("/applications/1", json={"status": "offer"})
    assert missing.status_code == 404


async def test_applying_to_a_missing_job_over_http_is_a_404(session_factory) -> None:
    async with _client() as client:
        missing = await client.post("/applications", json={"job_id": 4242})
    assert missing.status_code == 404


async def test_filtering_the_list_by_stage(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([_job(1), _job(2)])
        await session.commit()
    async with _client() as client:
        await client.post("/applications", json={"job_id": 1})
        await client.post("/applications", json={"job_id": 2})
        await client.patch("/applications/2", json={"status": "offer"})

        offers = await client.get("/applications", params={"status": "offer"})
        assert [item["job_id"] for item in offers.json()["items"]] == [2]

        unknown = await client.get("/applications", params={"status": "vibing"})
        assert unknown.status_code == 422
