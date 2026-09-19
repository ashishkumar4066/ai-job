"""The Matches routes through HTTP — the path a unit test of the runner skips.

`GET /matches` crashed with a 500 on its first live request after the filter
builder moved to `job_filters`: FastAPI hands absent list parameters over as
None, which the shared model rejected. Nothing below the route saw it.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from app.main import app
from app.match_runner import run_matching
from app.matching import MatchWeights
from app.models import JobPosting, utcnow
from app.prefs import Prefs
from app.profile import Profile


def _job(n: int, **over: object) -> JobPosting:
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
        "description_text": "Senior backend engineer, 5+ years. Python, FastAPI, GCP. " * 4,
        "posted_at": now - timedelta(days=2),
        "first_seen_at": now - timedelta(days=2),
        "last_seen_at": now,
        "status": "open",
        "content_hash": f"h{n}",
        "eligibility_pass": True,
        "validity_score": 90,
    }
    base.update(over)
    return JobPosting(**base)  # type: ignore[arg-type]


async def test_matches_list_funnel_and_transfer_over_http(
    session_factory, monkeypatch, tmp_path
) -> None:
    profile = Profile(skills={"backend": {"python": 3, "fastapi": 3}, "cloud": {"gcp": 3}}, gaps={})
    monkeypatch.setattr("app.api.get_profile", lambda: profile)
    monkeypatch.setenv("PREFS_FILE", str(tmp_path / "prefs.yaml"))
    from app.config import get_settings
    from app.prefs import get_prefs

    get_settings.cache_clear()
    get_prefs.cache_clear()

    async with session_factory() as session:
        session.add_all([_job(1), _job(2, workplace_type="onsite", remote=False)])
        await session.commit()
        await run_matching(session, profile=profile, weights=MatchWeights(), prefs=Prefs())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/matches", params={"match_prefs": "false"})
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["total"] == 2
        assert {"shortlisted", "shortlist_reasons", "blockers", "llm_read"} <= body["items"][0].keys()

        funnel = await client.get("/matches/funnel")
        assert funnel.status_code == 200, funnel.text
        assert funnel.json()["steps"][0]["count"] == 2

        # Clicking a band selects within the list; the chip counts, the "All"
        # count and the shortlist count must not collapse to that band.
        some_band = body["items"][0]["band"]
        banded = (
            await client.get("/matches", params={"match_prefs": "false", "fit_band": some_band})
        ).json()
        assert banded["total_all"] == body["total_all"] == 2
        assert banded["bands"] == body["bands"]
        assert banded["shortlisted"] == body["shortlisted"]
        empty_band = next(b for b in ("excellent", "poor") if b != some_band)
        other = (
            await client.get("/matches", params={"match_prefs": "false", "fit_band": empty_band})
        ).json()
        assert other["total_all"] == 2 and other["bands"] == body["bands"]

        sent = await client.put("/matches/transfer", json={"remote": True, "company": []})
        assert sent.status_code == 200, sent.text
        assert sent.json()["transfer"]["count_at_transfer"] == 1
        assert "Remote" in sent.json()["transfer"]["labels"]

        too_many = await client.post("/matches/llm", params={"limit": 51})
        assert too_many.status_code == 422

        cleared = await client.delete("/matches/transfer")
        assert cleared.json()["transfer"] is None


async def test_hide_blocked_removes_blocked_jobs(session_factory, monkeypatch, tmp_path) -> None:
    profile = Profile(skills={"backend": {"python": 3}}, gaps={})
    monkeypatch.setattr("app.api.get_profile", lambda: profile)
    monkeypatch.setenv("PREFS_FILE", str(tmp_path / "prefs.yaml"))
    from app.config import get_settings
    from app.prefs import get_prefs

    get_settings.cache_clear()
    get_prefs.cache_clear()

    async with session_factory() as session:
        blocked_jd = "Senior backend engineer, 5+ years. Python. No visa sponsorship. " * 4
        session.add_all([_job(11), _job(12, description_text=blocked_jd)])
        await session.commit()
        await run_matching(session, profile=profile, weights=MatchWeights(), prefs=Prefs())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        shown = (await client.get("/matches", params={"match_prefs": "false"})).json()
        hidden = (
            await client.get("/matches", params={"match_prefs": "false", "hide_blocked": "true"})
        ).json()

    assert shown["total"] == 2 and shown["blocked"] == 1
    assert hidden["total"] == 1 and hidden["total_all"] == 1
    # The toggle's own count is what it removes, so it does not drop to 0 when on.
    assert hidden["blocked"] == 1
    assert all(not item["blockers"] for item in hidden["items"])


async def test_llm_band_selects_on_the_deep_read(session_factory, monkeypatch, tmp_path) -> None:
    """`llm_band` filters on the LLM's fit_band; stale and failed reads count as unread."""
    from sqlalchemy import select

    from app.models import JobMatch

    profile = Profile(skills={"backend": {"python": 3}}, gaps={})
    monkeypatch.setattr("app.api.get_profile", lambda: profile)
    monkeypatch.setenv("PREFS_FILE", str(tmp_path / "prefs.yaml"))
    from app.config import get_settings
    from app.prefs import get_prefs

    get_settings.cache_clear()
    get_prefs.cache_clear()

    async with session_factory() as session:
        session.add_all([_job(21), _job(22), _job(23), _job(24)])
        await session.commit()
        await run_matching(session, profile=profile, weights=MatchWeights(), prefs=Prefs())
        rows = {
            m.job_id: m
            for m in (await session.execute(select(JobMatch))).scalars()
        }
        jobs = {
            j.source_key: j for j in (await session.execute(select(JobPosting))).scalars()
        }
        read = {"fit_band": "moderate", "fit_reasons": [], "blocked": False}
        for key, verdict, hash_ok in (
            ("test:acme:21", read, True),
            ("test:acme:22", read, False),  # JD changed since the read
            ("test:acme:23", {"error": "boom"}, True),  # failed read
        ):
            job = jobs[key]
            match = rows[job.id]
            match.llm_used = "error" not in verdict
            match.llm_verdict = verdict
            match.llm_content_hash = job.content_hash if hash_ok else "old"
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        base = {"match_prefs": "false"}
        everything = (await client.get("/matches", params=base)).json()
        moderate = (await client.get("/matches", params={**base, "llm_band": "moderate"})).json()
        unread = (await client.get("/matches", params={**base, "llm_band": "unread"})).json()
        both = (
            await client.get("/matches", params={**base, "llm_band": ["moderate", "unread"]})
        ).json()
        # Filters combine: an LLM band AND a ranker band nobody is in is empty,
        # and the LLM counts follow the ranker selection, not their own.
        fit = everything["items"][0]["band"]
        crossed = (
            await client.get("/matches", params={**base, "llm_band": "moderate", "fit_band": fit})
        ).json()
        suspect = (await client.get("/matches", params={**base, "validity": "suspect"})).json()
        other_fit = next(b for b in ("excellent", "poor") if b != fit)
        none = (
            await client.get("/matches", params={**base, "llm_band": "moderate", "fit_band": other_fit})
        ).json()

    assert everything["llm_bands"] == {"moderate": 1, "unread": 3}
    assert moderate["total"] == 1
    assert moderate["items"][0]["job"]["source_key"] == "test:acme:21"
    assert unread["total"] == 3 and unread["total_all"] == 4
    assert both["total"] == 4
    # A chip's own filter never shrinks its own counts.
    assert moderate["llm_bands"] == everything["llm_bands"]
    assert crossed["total"] == 1
    assert none["total"] == 0 and none["llm_bands"] == {}
    assert everything["validity_bands"] == {"solid": 4}
    assert suspect["total"] == 0 and suspect["validity_bands"] == {"solid": 4}
