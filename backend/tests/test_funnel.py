"""Every number on the dashboard must reconcile with the one next to it.

The bug report behind these tests: "5,480 jobs, 740 with Remote on, then
matching says 430 of 910 eligible — what is going on?" Each count came from a
different query. These pin the invariants that make the funnel trustworthy:
the Jobs funnel ends at the Jobs list's total, Remote means one thing, and
each Matches step equals the step before it minus what it dropped.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import count

from sqlalchemy import func, select

from app.funnel import jobs_funnel, matches_funnel
from app.job_filters import JobFilterSet, apply_job_filters
from app.match_runner import run_matching
from app.matching import MatchWeights
from app.models import JobPosting, utcnow
from app.prefs import Prefs, TransferPrefs
from app.profile import Profile

_ids = count(1)

STRONG_JD = (
    "Senior backend engineer, 5+ years. Python, FastAPI, Postgres, GCP, Docker, "
    "React, TypeScript, LLM, RAG, LangGraph on a remote-first team. "
) * 3


def job(**over: object) -> JobPosting:
    n = next(_ids)
    now = utcnow()
    base: dict[str, object] = {
        "source_key": f"test:acme:{n}",
        "source_id": "test:acme",
        "ats": "ashby",
        "company": "Acme",
        "title": "Senior Backend Engineer",
        "locations": ["Remote"],
        "remote": True,
        "workplace_type": "remote",
        "employment_type": "full_time",
        "apply_url": f"https://example.com/{n}",
        "description_text": STRONG_JD,
        "posted_at": now - timedelta(days=3),
        "first_seen_at": now - timedelta(days=3),
        "last_seen_at": now,
        "status": "open",
        "content_hash": f"h{n}",
        "eligibility_pass": True,
        "validity_score": 90,
    }
    base.update(over)
    return JobPosting(**base)  # type: ignore[arg-type]


async def _count(session, filters: JobFilterSet) -> int:
    stmt = apply_job_filters(select(func.count()).select_from(JobPosting), filters)
    return await session.scalar(stmt) or 0


async def test_remote_means_the_boards_field_first(session_factory) -> None:
    """The keyword flag and the board's field used to be two definitions."""
    async with session_factory() as session:
        session.add_all([
            job(workplace_type="remote", remote=False),   # board says remote
            job(workplace_type="onsite", remote=True),    # board says onsite
            job(workplace_type=None, remote=True),        # board silent, keywords say remote
            job(workplace_type=None, remote=False),
        ])
        await session.commit()

        assert await _count(session, JobFilterSet(remote=True)) == 2
        assert await _count(session, JobFilterSet(remote=False)) == 2


async def test_the_jobs_funnel_ends_at_the_list_total(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([
            job(),
            job(eligibility_pass=False),
            job(posted_at=utcnow() - timedelta(days=45)),
            job(workplace_type="onsite", remote=False),
            job(status="closed"),
        ])
        await session.commit()

        filters = JobFilterSet(eligibility_pass=True, posted_within_days=30, remote=True)
        funnel = await jobs_funnel(session, filters)

    steps = funnel["steps"]
    assert [s["count"] for s in steps] == [4, 3, 2, 1]
    assert funnel["total"] == 1
    for before, after in zip(steps, steps[1:]):
        assert after["count"] == before["count"] - after["dropped"]


async def test_matches_funnel_reconciles_step_by_step(session_factory, tmp_path) -> None:
    profile = Profile(
        skills={
            "backend": {"python": 3, "fastapi": 3},
            "ai": {"llm": 3, "rag": 3, "langgraph": 3},
            "cloud": {"gcp": 3, "docker": 2},
            "frontend": {"react": 3, "typescript": 2},
            "data": {"postgres": 2},
        },
        gaps={},
    )
    async with session_factory() as session:
        session.add_all([
            job(),                                                     # shortlisted
            job(),                                                     # shortlisted
            job(workplace_type="hybrid", remote=False),                # not transferred
            job(posted_at=utcnow() - timedelta(days=40)),              # too old
            job(employment_type="contract"),                           # preference miss
            job(description_text=STRONG_JD + " No visa sponsorship."), # blocker
            job(eligibility_pass=False),                               # never counted
            job(workplace_type="onsite", remote=False,                 # blocker, out of scope
                description_text=STRONG_JD + " No visa sponsorship."),
        ])
        await session.commit()

        prefs = Prefs(transfer=TransferPrefs(filters=JobFilterSet(remote=True)))
        result = await run_matching(session, profile=profile, weights=MatchWeights(), prefs=prefs)
        assert result.considered == 7
        assert result.in_transfer == 5
        # Counted over the 5 in scope only: the one blocker is among them.
        # A blocked job OUTSIDE the scope must not show up in this number.
        assert result.blocked == 1

        funnel = await matches_funnel(
            session,
            profile_version=profile.version,
            prefs=prefs,
            threshold=MatchWeights().llm_threshold,
            default_reads=15,
        )

    by_key = {s["key"]: s for s in funnel["steps"]}
    # The funnel OPENS at what was sent (5 remote), not at every eligible job
    # (6) with the difference subtracted afterwards.
    assert funnel["steps"][0]["key"] == "transfer"
    assert funnel["steps"][0]["count"] == 5
    assert "eligible" not in by_key
    assert by_key["posted"]["dropped"] == 1
    assert by_key["preferences"]["breakdown"] == {"commitment": 1}
    assert by_key["shortlist"]["count"] == 2
    assert by_key["shortlist"]["breakdown"] == {"blocker in JD": 1}
    assert funnel["shortlisted"] == result.shortlisted == 2
    assert funnel["stale"] is False

    chain = [s for s in funnel["steps"] if s["key"] != "llm"]
    for before, after in zip(chain, chain[1:]):
        assert after["count"] == before["count"] - after["dropped"], (before, after)


async def test_a_new_send_shows_immediately_before_any_run(session_factory) -> None:
    """Right after "Send to Matches" the funnel must show the sent count, not
    wait for the next Run to re-stamp the stored verdicts."""
    profile = Profile(skills={"backend": {"python": 3}}, gaps={})
    async with session_factory() as session:
        session.add_all([job(), job(), job(workplace_type="onsite", remote=False)])
        await session.commit()
        # Ranked with no scope at all...
        await run_matching(session, profile=profile, weights=MatchWeights(), prefs=Prefs())
        # ...then a scope is sent, and the funnel is read before any Run.
        sent = Prefs(transfer=TransferPrefs(filters=JobFilterSet(remote=True), count_at_transfer=2))
        funnel = await matches_funnel(
            session,
            profile_version=profile.version,
            prefs=sent,
            threshold=MatchWeights().llm_threshold,
            default_reads=15,
        )

    assert funnel["steps"][0]["label"] == "Sent from Jobs"
    assert funnel["steps"][0]["count"] == 2
    assert funnel["stale"] is True  # verdicts predate the send — Run to refresh the rest
