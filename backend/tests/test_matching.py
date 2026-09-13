"""Fit-scoring tests.

The load-bearing one is `test_prose_written_jd_is_routed_despite_low_score`.
It encodes a real failure: an early keyword-overlap scorer hard-dropped 45% of
the live board, and among the casualties was a full-stack AI role that said
"candidates must be based in India. We are hiring for this market
specifically" — scored zero because its description names no technology, only
"modern web frameworks, APIs, databases". If that test ever goes green by
having the row dropped or left unrouted, the regression is back.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.matching import MatchWeights, evaluate
from app.models import utcnow
from app.profile import Profile, ProfileError, load_profile

WEIGHTS = MatchWeights()


@pytest.fixture
def profile() -> Profile:
    return Profile(
        skills={
            "backend": {"python": 3, "fastapi": 3, "node": 2},
            "frontend": {"react": 3, "typescript": 2},
            "ai": {"llm": 3, "rag": 3, "langgraph": 3},
            "cloud": {"gcp": 3, "docker": 2},
            "data": {"postgres": 2, "sql": 2},
        },
        gaps={"aws": 2, "kubernetes": 2, "php": 2, "golang": 2},
        seniority={"total_years": 6, "ai_years": 3, "current_title": "Senior Full Stack Developer"},
    )


# --------------------------------------------------------------------------
# The contract: ranking, never filtering
# --------------------------------------------------------------------------
def test_score_is_bounded(profile: Profile) -> None:
    worst = evaluate(
        title="Senior PHP Developer",
        description_text="PHP, Drupal, golang, kubernetes, aws. Always looking for talent. "
        "*Urgent* immediate joiner. Equity only.",
        profile=profile,
        weights=WEIGHTS,
    )
    best = evaluate(
        title="Senior Full Stack AI Engineer",
        description_text="python fastapi react typescript llm rag langgraph gcp docker "
        "postgres sql node. 5+ years. Remote, India.",
        posted_at=utcnow(),
        now=utcnow(),
        profile=profile,
        weights=WEIGHTS,
    )
    assert 0 <= worst.score <= 100
    assert 0 <= best.score <= 100
    assert best.score > worst.score


def test_a_terrible_match_still_returns_a_verdict(profile: Profile) -> None:
    """There is no drop path. A bad fit sorts last; it never disappears."""
    verdict = evaluate(
        title="Senior PHP Developer",
        description_text="PHP and Drupal only.",
        profile=profile,
        weights=WEIGHTS,
    )
    assert verdict.score >= 0
    assert verdict.reasons, "a low score must still explain itself"


def test_prose_written_jd_is_routed_despite_low_score(profile: Profile) -> None:
    """The Bjak case — see the module docstring.

    A genuinely strong role described without naming any technology must be
    flagged low-confidence and queued for a read, *even though* its overlap
    score lands mid-pack. Score alone would bury it.
    """
    verdict = evaluate(
        title="Full Stack Software Engineer - AI Neobank App",
        description_text=(
            "We are looking for full stack engineers to build fast across our AI Neobank "
            "app. 3+ years of full stack software engineering experience. Strong frontend "
            "and backend fundamentals. Experience with modern web frameworks, APIs, "
            "databases and production systems. This role is remote, but candidates must "
            "be based in India. We are hiring for this market specifically."
        ),
        posted_at=utcnow() - timedelta(days=33),
        now=utcnow(),
        profile=profile,
        weights=WEIGHTS,
    )
    assert verdict.confident is False, "a JD naming no tech cannot be scored confidently"
    assert verdict.needs_llm is True, "low confidence must route regardless of score"
    assert verdict.score < WEIGHTS.llm_threshold, (
        "fixture assumes this scores below the threshold — if it now scores above it, "
        "the test no longer exercises the confidence path"
    )
    assert any("low_confidence" in r for r in verdict.reasons)


def test_high_score_is_routed_even_when_confident(profile: Profile) -> None:
    verdict = evaluate(
        title="Senior Full Stack AI Engineer",
        description_text="python fastapi react typescript llm rag langgraph gcp docker "
        "postgres node sql. 5+ years experience. India remote.",
        posted_at=utcnow(),
        now=utcnow(),
        profile=profile,
        weights=WEIGHTS,
    )
    assert verdict.confident is True
    assert verdict.score >= WEIGHTS.llm_threshold
    assert verdict.needs_llm is True


# --------------------------------------------------------------------------
# Individual signals
# --------------------------------------------------------------------------
def test_india_mention_beats_an_otherwise_identical_posting(profile: Profile) -> None:
    body = "python fastapi react postgres docker. 5+ years."
    plain = evaluate(title="Backend Engineer", description_text=body, profile=profile, weights=WEIGHTS)
    india = evaluate(
        title="Backend Engineer",
        description_text=body + " Candidates must be based in India.",
        profile=profile,
        weights=WEIGHTS,
    )
    assert india.score > plain.score
    assert any("india_named" in r for r in india.reasons)


def test_spam_markers_sink_a_keyword_stuffed_posting(profile: Profile) -> None:
    """Counting keywords rewards keyword stuffing; the penalty is the counterweight."""
    stuffed = "python fastapi react typescript llm rag langgraph gcp docker postgres node sql"
    clean = evaluate(title="AI Engineer", description_text=stuffed, profile=profile, weights=WEIGHTS)
    spam = evaluate(
        title="*Urgent* Gen AI Engineer / Immediate-30 days",
        description_text=stuffed + " Equity only, unpaid trial period.",
        profile=profile,
        weights=WEIGHTS,
    )
    assert spam.score < clean.score
    assert any("spam_markers" in r for r in spam.reasons)


def test_evergreen_language_is_penalised(profile: Profile) -> None:
    body = "python fastapi react postgres. 5+ years."
    normal = evaluate(title="Backend Engineer", description_text=body, profile=profile, weights=WEIGHTS)
    pool = evaluate(
        title="Backend Engineer",
        description_text=body + " We are always looking for talented people; general application.",
        profile=profile,
        weights=WEIGHTS,
    )
    assert pool.score < normal.score
    assert any("evergreen" in r for r in pool.reasons)


def test_missing_stacks_are_named_not_just_scored(profile: Profile) -> None:
    verdict = evaluate(
        title="Backend Engineer",
        description_text="python fastapi with aws lambda and kubernetes orchestration",
        profile=profile,
        weights=WEIGHTS,
    )
    assert set(verdict.missing_stacks) == {"aws", "kubernetes"}
    assert any("stack_gaps" in r for r in verdict.reasons)


def test_unstated_years_scores_near_ideal_not_zero(profile: Profile) -> None:
    """Silence is not a negative — the same rule `eligibility.py` uses for pay."""
    silent = evaluate(
        title="Backend Engineer",
        description_text="python fastapi react postgres",
        profile=profile,
        weights=WEIGHTS,
    )
    assert silent.years_required is None
    assert silent.subscores["years"] >= int(WEIGHTS.years_cap * 0.75)
    assert any("years_unstated" in r for r in silent.reasons)


def test_gap_penalty_is_capped(profile: Profile) -> None:
    """One stray mention of AWS in a benefits list must not sink a good role."""
    verdict = evaluate(
        title="Senior Full Stack AI Engineer",
        description_text="python fastapi react llm rag gcp docker postgres. "
        "aws kubernetes php golang all mentioned repeatedly aws kubernetes php golang.",
        profile=profile,
        weights=WEIGHTS,
    )
    assert verdict.subscores["penalty"] >= -(WEIGHTS.gap_penalty_cap)


def test_special_characters_in_skill_names_do_not_break_matching() -> None:
    """`c++`, `.net` and `ci/cd` are regex metacharacters if not escaped."""
    profile = Profile(
        skills={"backend": {"c++": 3, ".net": 2, "ci/cd": 1}},
        gaps={},
        seniority={"total_years": 6},
    )
    verdict = evaluate(
        title="Engineer",
        description_text="We use c++ and .net with ci/cd pipelines.",
        profile=profile,
        weights=WEIGHTS,
    )
    assert set(verdict.matched_skills) == {"c++", ".net", "ci/cd"}


# --------------------------------------------------------------------------
# profile_version
# --------------------------------------------------------------------------
def test_profile_version_changes_when_skills_change(profile: Profile) -> None:
    other = profile.model_copy(
        update={"skills": {**profile.skills, "backend": {"python": 3, "fastapi": 3, "go": 3}}}
    )
    assert other.version != profile.version


def test_profile_version_ignores_fields_that_cannot_change_a_score(profile: Profile) -> None:
    """Editing a phone number must not invalidate 917 cached scores."""
    noisy = profile.model_copy(
        update={"identity": profile.identity.model_copy(update={"phone": "+91-0000000000"})}
    )
    assert noisy.version == profile.version


def test_flat_skills_keeps_the_strongest_weight(profile: Profile) -> None:
    p = Profile(
        skills={"backend": {"sql": 1}, "data": {"sql": 3}},
        seniority={"total_years": 6},
    )
    assert p.flat_skills["sql"] == 3


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------
def test_missing_profile_raises_rather_than_degrading(tmp_path) -> None:
    """Unlike `load_filters`, an absent profile is fatal.

    Degrading to an empty profile would score every job as a zero-skill match
    and write hundreds of confidently-wrong rows that look like a working
    feature.
    """
    with pytest.raises(ProfileError):
        load_profile(tmp_path / "nope.yaml")


def test_profile_without_skills_is_rejected(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("identity:\n  full_name: Nobody\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="no skills"):
        load_profile(path)


def test_shipped_profile_loads_and_is_usable() -> None:
    """The real `profile.yaml` must parse — it is what production scores against."""
    from app.config import get_settings

    p = load_profile(get_settings().profile_file)
    assert p.flat_skills
    assert p.seniority.total_years > 0
    assert len(p.version) == 16


# --------------------------------------------------------------------------
# The scoring pass
# --------------------------------------------------------------------------
@pytest.mark.anyio
async def test_scoring_pass_is_idempotent(session_factory, monkeypatch, tmp_path) -> None:
    """CLAUDE.md's acceptance criterion: an unchanged board and an unchanged
    profile must write nothing on a re-run."""
    from app.match_runner import run_matching
    from app.models import JobPosting

    path = tmp_path / "profile.yaml"
    path.write_text(
        "seniority:\n  total_years: 6\n"
        "skills:\n  backend:\n    python: 3\n    fastapi: 3\n",
        encoding="utf-8",
    )
    profile = load_profile(path)

    now = utcnow()
    async with session_factory() as session:
        session.add(
            JobPosting(
                source_key="greenhouse:acme:1",
                source_id="greenhouse:acme",
                ats="greenhouse",
                company="Acme",
                title="Senior Backend Engineer",
                locations=["Remote"],
                remote=True,
                apply_url="https://example.test/1",
                description_text="python and fastapi, 5+ years, remote worldwide",
                posted_at=now,
                first_seen_at=now,
                last_seen_at=now,
                status="open",
                content_hash="hash-1",
                eligibility_pass=True,
            )
        )
        await session.commit()

        first = await run_matching(session, profile=profile)
        assert first.scored == 1
        assert first.skipped == 0

        second = await run_matching(session, profile=profile)
        assert second.scored == 0, "a re-run must not insert"
        assert second.updated == 0, "a re-run must not rewrite"
        assert second.skipped == 1


@pytest.mark.anyio
async def test_editing_the_profile_rescores_under_a_new_version(
    session_factory, tmp_path
) -> None:
    """A stale score must never be shown as current — so an edited profile
    writes fresh rows rather than mutating the old ones."""
    from sqlalchemy import select

    from app.match_runner import run_matching
    from app.models import JobMatch, JobPosting

    def write(skills: str) -> Profile:
        path = tmp_path / "p.yaml"
        path.write_text(f"seniority:\n  total_years: 6\nskills:\n  backend:\n{skills}", "utf-8")
        return load_profile(path)

    now = utcnow()
    async with session_factory() as session:
        session.add(
            JobPosting(
                source_key="greenhouse:acme:2",
                source_id="greenhouse:acme",
                ats="greenhouse",
                company="Acme",
                title="Senior Backend Engineer",
                locations=["Remote"],
                remote=True,
                apply_url="https://example.test/2",
                description_text="python fastapi react postgres docker, 5+ years",
                posted_at=now,
                first_seen_at=now,
                last_seen_at=now,
                status="open",
                content_hash="hash-2",
                eligibility_pass=True,
            )
        )
        await session.commit()

        v1 = write("    python: 3\n")
        r1 = await run_matching(session, profile=v1)
        v2 = write("    python: 3\n    react: 3\n    postgres: 2\n")
        r2 = await run_matching(session, profile=v2)

        assert r1.profile_version != r2.profile_version
        rows = (await session.execute(select(JobMatch))).scalars().all()
        assert len(rows) == 2, "the old version's score stays readable"
        by_version = {r.profile_version: r.score for r in rows}
        assert by_version[r2.profile_version] > by_version[r1.profile_version]


@pytest.mark.anyio
async def test_ineligible_and_closed_jobs_are_never_scored(session_factory, tmp_path) -> None:
    """Fit is a ranking on top of Phase 1's gate, not a second opinion on it."""
    from app.match_runner import run_matching
    from app.models import JobPosting

    path = tmp_path / "p.yaml"
    path.write_text("seniority:\n  total_years: 6\nskills:\n  backend:\n    python: 3\n", "utf-8")
    profile = load_profile(path)

    now = utcnow()
    async with session_factory() as session:
        for i, (status, eligible) in enumerate(
            [("open", False), ("closed", True), ("open", True)]
        ):
            session.add(
                JobPosting(
                    source_key=f"greenhouse:acme:{10 + i}",
                    source_id="greenhouse:acme",
                    ats="greenhouse",
                    company="Acme",
                    title="Backend Engineer",
                    locations=["Remote"],
                    remote=True,
                    apply_url=f"https://example.test/{10 + i}",
                    description_text="python",
                    posted_at=now,
                    first_seen_at=now,
                    last_seen_at=now,
                    status=status,
                    content_hash=f"h{i}",
                    eligibility_pass=eligible,
                )
            )
        await session.commit()

        result = await run_matching(session, profile=profile)
        assert result.considered == 1, "only the open + eligible row is scored"
