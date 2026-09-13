"""The deterministic validity layer.

The checks worth defending in a test are the ones where the obvious
implementation is wrong, and this module has three:

  * the ghost check must compare against a run's START, not its finish —
    the first cut used `finished_at` and reported 2,123 live rows as ghosts;
  * NULL validity must mean "not yet checked", never 0;
  * the duplicate key deliberately ignores location, which is why the
    whole-board count (223) and the eligible-slice count (2) disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import JobPosting
from app.validation import (
    ValidityConfig,
    apply_llm_validity,
    evaluate,
    evaluate_all,
    find_duplicates,
    get_validity_config,
    jd_fingerprint,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

# Long enough to clear `thin_description_chars` so age/ghost tests are not
# quietly also testing the thin-description penalty.
BODY = (
    "We are hiring a senior backend engineer to own our payments platform. "
    "You will design services in Python, work with Postgres, and partner with "
    "product on roadmap decisions. Experience with distributed systems is "
    "valued. We offer a competitive package and a remote-first culture. "
) * 3


def make_job(**kwargs: object) -> JobPosting:
    """A plausible, healthy posting. Tests override one field at a time."""
    defaults: dict[str, object] = {
        "id": 1,
        "source_key": "greenhouse:acme:1",
        "source_id": "greenhouse:acme",
        "ats": "greenhouse",
        "company": "Acme",
        "title": "Senior Backend Engineer",
        "locations": ["Remote"],
        "remote": True,
        "apply_url": "https://example.com/apply/1",
        "description_text": BODY,
        "posted_at": NOW - timedelta(days=3),
        "first_seen_at": NOW - timedelta(days=3),
        "last_seen_at": NOW,
        "status": "open",
        "content_hash": "hash-1",
        "validity_reasons": [],
        "eligibility_reasons": [],
        "locations_eligibility": [],
    }
    defaults.pop("locations_eligibility")
    defaults.update(kwargs)
    return JobPosting(**defaults)  # type: ignore[arg-type]


class TestAge:
    def test_a_fresh_posting_scores_full(self) -> None:
        v = evaluate(make_job(), now=NOW)
        assert v.score == 100
        assert any(r.startswith("fresh:") for r in v.reasons)

    @pytest.mark.parametrize(
        ("days", "label"),
        [(30, "aging"), (60, "stale"), (200, "very_stale")],
    )
    def test_age_bands_are_labelled_and_ramp(self, days: int, label: str) -> None:
        v = evaluate(make_job(posted_at=NOW - timedelta(days=days)), now=NOW)
        assert any(r.startswith(f"{label}:") for r in v.reasons), v.reasons
        assert v.score < 100

    def test_older_postings_never_score_above_younger_ones(self) -> None:
        scores = [
            evaluate(make_job(posted_at=NOW - timedelta(days=d)), now=NOW).score
            for d in (3, 30, 60, 200)
        ]
        assert scores == sorted(scores, reverse=True), scores

    def test_a_missing_posted_date_is_penalized_not_crashed(self) -> None:
        v = evaluate(make_job(posted_at=None), now=NOW)
        assert "no_posted_date" in " ".join(v.reasons)
        assert v.score < 100


class TestGhostCheck:
    """The check that got this wrong once. See `board_last_success`."""

    def test_a_row_seen_during_the_run_is_not_a_ghost(self) -> None:
        # The run began at 11:00 and this row was stamped at 11:02, partway
        # through a long sweep. Comparing against the run's FINISH would flag
        # it; comparing against its start correctly does not.
        run_started = NOW - timedelta(hours=1)
        job = make_job(last_seen_at=run_started + timedelta(minutes=2))
        v = evaluate(job, now=NOW, board_last_success=run_started)
        assert "seen_in_last_sweep" in v.reasons
        assert not any(r.startswith("missed_last_sweep") for r in v.reasons)

    def test_a_row_seen_before_the_run_began_is_a_ghost(self) -> None:
        run_started = NOW - timedelta(hours=1)
        job = make_job(last_seen_at=run_started - timedelta(days=2))
        v = evaluate(job, now=NOW, board_last_success=run_started)
        assert any(r.startswith("missed_last_sweep") for r in v.reasons), v.reasons
        assert v.score < 70

    def test_a_row_seen_exactly_at_the_run_start_passes(self) -> None:
        """The boundary is inclusive — the first row of a sweep is not a ghost."""
        run_started = NOW - timedelta(hours=1)
        v = evaluate(
            make_job(last_seen_at=run_started), now=NOW, board_last_success=run_started
        )
        assert "seen_in_last_sweep" in v.reasons

    def test_a_closed_row_is_not_judged_on_presence(self) -> None:
        """Closure by disappearance already explains it; penalizing twice is noise."""
        v = evaluate(
            make_job(status="closed"),
            now=NOW,
            board_last_success=NOW - timedelta(days=1),
        )
        assert "closed" in v.reasons
        assert not any(r.startswith("missed_last_sweep") for r in v.reasons)

    def test_no_run_history_means_no_presence_verdict(self) -> None:
        """A fresh DB must not label every row a ghost for lack of evidence."""
        v = evaluate(make_job(), now=NOW, board_last_success=None)
        assert not any("sweep" in r for r in v.reasons)


class TestMissingFields:
    def test_an_unapplyable_posting_is_heavily_penalized(self) -> None:
        v = evaluate(make_job(apply_url=""), now=NOW)
        assert "no_apply_url" in " ".join(v.reasons)
        assert v.score <= 60

    def test_an_empty_description_is_penalized(self) -> None:
        v = evaluate(make_job(description_text=""), now=NOW)
        assert "no_description" in " ".join(v.reasons)

    def test_a_thin_description_is_penalized_less_than_an_absent_one(self) -> None:
        thin = evaluate(make_job(description_text="Backend engineer wanted."), now=NOW)
        absent = evaluate(make_job(description_text=""), now=NOW)
        assert absent.score < thin.score < 100


class TestProse:
    @pytest.mark.parametrize(
        "text",
        [
            "We are always looking for talented engineers to join us.",
            "Join our talent pool and we will reach out when a role opens.",
            "This is a general application for future openings.",
        ],
    )
    def test_evergreen_language_is_flagged(self, text: str) -> None:
        v = evaluate(make_job(description_text=BODY + text), now=NOW)
        assert "evergreen_language" in " ".join(v.reasons), v.reasons

    def test_an_ordinary_enthusiastic_jd_is_not_evergreen(self) -> None:
        """The patterns must not fire on normal recruiting warmth."""
        text = BODY + "We are growing fast and we would love for you to join us!"
        v = evaluate(make_job(description_text=text), now=NOW)
        assert "evergreen_language" not in " ".join(v.reasons)

    def test_contentless_boilerplate_is_flagged(self) -> None:
        v = evaluate(
            make_job(description_text=BODY + "Job description will be shared later."),
            now=NOW,
        )
        assert "contentless_description" in " ".join(v.reasons)


class TestDuplicates:
    def test_the_first_row_of_a_group_is_never_flagged(self) -> None:
        rows = [make_job(id=i, content_hash=f"h{i}") for i in (1, 2, 3)]
        flagged = find_duplicates(rows)
        assert 1 not in flagged
        assert set(flagged) == {2, 3}

    def test_a_different_title_is_not_a_duplicate(self) -> None:
        rows = [make_job(id=1), make_job(id=2, title="Staff Platform Engineer")]
        assert find_duplicates(rows) == {}

    def test_the_same_jd_under_two_companies_is_reported_separately(self) -> None:
        rows = [make_job(id=1), make_job(id=2, company="Globex")]
        flagged = find_duplicates(rows)
        assert flagged[2] == "duplicate_cross_company"

    def test_location_is_deliberately_not_part_of_the_key(self) -> None:
        """Why the whole-board count (223) dwarfs the eligible one (2).

        Databricks posts one role across nine cities as nine rows with a
        byte-identical JD. For a remote-only candidate those are one
        opportunity, so they collapse.
        """
        rows = [
            make_job(id=1, locations=["Austin, Texas"]),
            make_job(id=2, locations=["Seattle, Washington"]),
        ]
        assert find_duplicates(rows) == {2: "duplicate_same_company"}

    def test_a_short_description_is_not_fingerprinted(self) -> None:
        """Two stub JDs must not collide into a false duplicate."""
        assert jd_fingerprint("Engineer wanted.") == ""
        rows = [make_job(id=1, description_text="hi"), make_job(id=2, description_text="hi")]
        assert find_duplicates(rows) == {}

    def test_markup_and_whitespace_do_not_defeat_the_fingerprint(self) -> None:
        a = jd_fingerprint(BODY)
        b = jd_fingerprint(BODY.replace(" ", "\n  ").upper())
        assert a == b != ""


class TestLLMOverlay:
    def test_a_ghost_verdict_dominates_the_score(self) -> None:
        v = apply_llm_validity(evaluate(make_job(), now=NOW), {"posting_status": "ghost"})
        assert "llm:ghost" in " ".join(v.reasons)
        assert v.score <= 60

    def test_an_active_verdict_is_recorded_without_penalty(self) -> None:
        base = evaluate(make_job(), now=NOW)
        v = apply_llm_validity(base, {"posting_status": "active"})
        assert v.score == 100
        assert "llm:active" in v.reasons

    def test_evergreen_is_not_charged_twice(self) -> None:
        """The regex already caught it; the model confirming it is not new."""
        text = BODY + "We are always looking for great people."
        job = make_job(description_text=text)
        deterministic = evaluate(job, now=NOW)
        confirmed = apply_llm_validity(
            evaluate(job, now=NOW), {"posting_status": "evergreen"}
        )
        assert confirmed.score == deterministic.score
        assert "llm:evergreen_confirmed" in confirmed.reasons

    def test_evergreen_the_regex_missed_is_charged_once(self) -> None:
        job = make_job()
        v = apply_llm_validity(evaluate(job, now=NOW), {"posting_status": "evergreen"})
        assert "llm:evergreen" in " ".join(v.reasons)
        assert v.score < 100

    def test_no_verdict_leaves_the_score_untouched(self) -> None:
        base = evaluate(make_job(), now=NOW)
        assert apply_llm_validity(base, None).score == base.score

    def test_a_stale_verdict_is_not_applied(self) -> None:
        """`llm_validity_hash` must match `content_hash` or the read is void."""
        job = make_job(
            content_hash="new-hash",
            llm_validity={"posting_status": "ghost"},
            llm_validity_hash="old-hash",
        )
        verdicts = evaluate_all([job], now=NOW)
        assert "llm:ghost" not in " ".join(verdicts[job.id].reasons)

    def test_a_current_verdict_is_applied(self) -> None:
        job = make_job(
            content_hash="h",
            llm_validity={"posting_status": "ghost"},
            llm_validity_hash="h",
        )
        verdicts = evaluate_all([job], now=NOW)
        assert "llm:ghost" in " ".join(verdicts[job.id].reasons)


class TestScoreFloorAndConfig:
    def test_the_score_never_goes_negative(self) -> None:
        """Every penalty at once must floor at 0, not wrap."""
        job = make_job(
            apply_url="",
            description_text="",
            posted_at=None,
            last_seen_at=NOW - timedelta(days=90),
            first_seen_at=NOW - timedelta(days=400),
        )
        v = evaluate(job, now=NOW, board_last_success=NOW - timedelta(days=1))
        assert v.score == 0

    def test_a_penalty_always_carries_its_amount(self) -> None:
        """A bare number is not auditable; every deduction names itself."""
        v = evaluate(make_job(apply_url=""), now=NOW)
        penalties = [r for r in v.reasons if ":-" in r]
        assert penalties
        for reason in penalties:
            assert reason.rsplit(":-", 1)[1].isdigit()

    def test_config_defaults_match_the_shipped_file(self) -> None:
        """`filters.yaml` and the model defaults must not drift apart."""
        assert get_validity_config() == ValidityConfig(
            **get_validity_config().model_dump()
        )

    def test_disabling_a_penalty_removes_it_entirely(self) -> None:
        cfg = ValidityConfig(missing_apply_url_penalty=0)
        v = evaluate(make_job(apply_url=""), now=NOW, config=cfg)
        assert "no_apply_url" not in " ".join(v.reasons)
        assert v.score == 100
