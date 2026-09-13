"""Match preferences and the gate they drive.

The invariants worth defending:

  * **NULL passes.** Greenhouse publishes no employment or workplace type at
    all (0 of 2,224 open rows), so a gate that fails NULL silently deletes
    40% of the board. Every rule here treats silence as admission, and
    `include_unstated: false` is the explicit opt-out.
  * **`prefs_version` is stable and content-addressed.** Reordering a list or
    re-saving unchanged preferences must not produce a new version, or the
    dashboard would report a stale list after a no-op save.
  * **The SQL and Python forms agree.** Two implementations of one filter is
    how filters drift; `sql_clause` is documented as a superset and this
    asserts exactly that relationship rather than assuming it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import JobPosting
from app.prefs import (
    EMPLOYMENT_TYPES,
    WORKPLACE_TYPES,
    CompensationPrefs,
    ExperiencePrefs,
    FreshnessPrefs,
    Prefs,
    PrefsError,
    TransferPrefs,
    ValidityPrefs,
    WorkPrefs,
    load_prefs,
    save_prefs,
)
from app.job_filters import JobFilterSet
from app.prefs_gate import evaluate, sql_clause

# Relative to the real clock, not a fixed date: the gate enforces a 30-day
# posting-age limit against `utcnow()`, so a pinned date would fail every test
# here a month after it was written.
NOW = datetime.now(UTC).replace(microsecond=0)

JD_WITH_YEARS = (
    "We are looking for a senior backend engineer with 5+ years of "
    "professional software development experience. You will work in Python "
    "and Postgres on a distributed payments platform. "
) * 2

JD_NO_YEARS = (
    "Join our platform team to build services in Python and Postgres. You "
    "will own delivery end to end and partner closely with product. "
) * 2


def make_job(**kwargs: object) -> JobPosting:
    defaults: dict[str, object] = {
        "id": 1,
        "source_key": "ashby:acme:1",
        "source_id": "ashby:acme",
        "ats": "ashby",
        "company": "Acme",
        "title": "Senior Backend Engineer",
        "locations": ["Remote"],
        "remote": True,
        "apply_url": "https://example.com/1",
        "description_text": JD_WITH_YEARS,
        "posted_at": NOW - timedelta(days=2),
        "first_seen_at": NOW - timedelta(days=2),
        "last_seen_at": NOW,
        "status": "open",
        "content_hash": "h1",
        "employment_type": "full_time",
        "workplace_type": "remote",
        "eligibility_pass": True,
        "eligibility_reasons": [],
        "validity_reasons": [],
    }
    defaults.update(kwargs)
    return JobPosting(**defaults)  # type: ignore[arg-type]


class TestUnstatedPasses:
    """The doctrine. Greenhouse publishes neither field for 2,224 rows."""

    @pytest.mark.parametrize("field", ["employment_type", "workplace_type"])
    def test_a_null_field_is_admitted_by_default(self, field: str) -> None:
        v = evaluate(make_job(**{field: None}), Prefs())
        assert v.passed
        assert any("unstated" in r for r in v.reasons), v.reasons

    @pytest.mark.parametrize("field", ["employment_type", "workplace_type"])
    def test_include_unstated_false_rejects_it(self, field: str) -> None:
        v = evaluate(make_job(**{field: None}), Prefs(include_unstated=False))
        assert not v.passed

    def test_an_unstated_salary_is_admitted_by_default(self) -> None:
        v = evaluate(make_job(salary_min=None, salary_max=None), Prefs())
        assert v.passed
        assert "pay_unstated" in v.reasons

    def test_require_stated_pay_rejects_a_silent_posting(self) -> None:
        prefs = Prefs(compensation=CompensationPrefs(require_stated=True))
        v = evaluate(make_job(salary_min=None, salary_max=None), prefs)
        assert not v.passed
        assert "pay_unstated" in v.reasons

    def test_an_unstated_years_requirement_is_admitted(self) -> None:
        v = evaluate(make_job(description_text=JD_NO_YEARS), Prefs())
        assert v.passed
        assert "years_unstated" in v.reasons

    def test_an_unscored_row_passes_a_validity_gate(self) -> None:
        """An unrun validity pass must not empty the Matches list."""
        prefs = Prefs(validity=ValidityPrefs(min_score=80))
        v = evaluate(make_job(validity_score=None), prefs)
        assert v.passed
        assert "validity_unchecked" in v.reasons


class TestWorkRules:
    def test_an_onsite_role_fails_a_remote_preference(self) -> None:
        v = evaluate(make_job(workplace_type="onsite"), Prefs())
        assert not v.passed
        assert "workplace_mismatch:onsite" in v.reasons

    def test_widening_the_preference_admits_it(self) -> None:
        prefs = Prefs(work=WorkPrefs(workplace_types=["remote", "onsite"]))
        assert evaluate(make_job(workplace_type="onsite"), prefs).passed

    def test_an_empty_list_means_no_constraint(self) -> None:
        prefs = Prefs(work=WorkPrefs(workplace_types=[], employment_types=[]))
        v = evaluate(make_job(workplace_type="onsite", employment_type="contract"), prefs)
        assert v.passed
        assert not any("workplace" in r for r in v.reasons)

    def test_a_contract_role_fails_a_full_time_preference(self) -> None:
        v = evaluate(make_job(employment_type="contract"), Prefs())
        assert not v.passed
        assert "employment_mismatch:contract" in v.reasons


class TestPay:
    def test_a_salary_above_the_floor_passes(self) -> None:
        # USD 120k at the static rate of 88 -> ~INR 1.05 crore.
        v = evaluate(make_job(salary_min=120_000, salary_currency="USD"), Prefs())
        assert v.passed
        assert any(r.startswith("pay_ok:") for r in v.reasons)

    def test_a_salary_below_the_floor_fails(self) -> None:
        v = evaluate(make_job(salary_min=200_000, salary_currency="INR"), Prefs())
        assert not v.passed
        assert any(r.startswith("pay_below_floor:") for r in v.reasons)

    def test_an_hourly_rate_is_annualized_not_read_as_annual(self) -> None:
        """The bug this reuse exists to prevent.

        "$25 - $30/hr" is ~INR 55L/yr. Compared as an annual figure it reads
        as INR 2,640 and the row is dropped for a reason no human would find.
        """
        v = evaluate(make_job(salary_min=25, salary_max=30, salary_currency="USD"), Prefs())
        assert v.passed, v.reasons
        assert any("hourly" in r for r in v.reasons), v.reasons

    def test_an_unconvertible_currency_is_not_treated_as_zero(self) -> None:
        """Our missing FX rate is our gap, not the posting's failing."""
        v = evaluate(make_job(salary_min=500_000, salary_currency="XYZ"), Prefs())
        assert v.passed
        assert any("pay_unreadable" in r for r in v.reasons)

    def test_the_upper_bound_of_a_range_is_what_counts(self) -> None:
        v = evaluate(
            make_job(salary_min=1_000, salary_max=150_000, salary_currency="USD"), Prefs()
        )
        assert v.passed


class TestYears:
    def test_a_requirement_inside_the_band_passes(self) -> None:
        v = evaluate(make_job(), Prefs())
        assert v.passed
        assert "years_ok:5" in v.reasons

    def test_a_requirement_above_the_band_fails(self) -> None:
        jd = "We need a principal engineer with 12+ years of experience. " * 6
        v = evaluate(make_job(description_text=jd), Prefs())
        assert not v.passed
        assert any(r.startswith("years_out_of_band:") for r in v.reasons)

    def test_widening_the_band_admits_it(self) -> None:
        jd = "We need a principal engineer with 12+ years of experience. " * 6
        prefs = Prefs(experience=ExperiencePrefs(min_years=2, max_years=15))
        assert evaluate(make_job(description_text=jd), prefs).passed


class TestScope:
    def test_an_excluded_company_fails(self) -> None:
        prefs = Prefs.model_validate({"scope": {"exclude_companies": ["acme"]}})
        v = evaluate(make_job(), prefs)
        assert not v.passed
        assert "company_excluded" in v.reasons

    def test_exclusion_is_case_insensitive(self) -> None:
        prefs = Prefs.model_validate({"scope": {"exclude_companies": ["AcMe"]}})
        assert not evaluate(make_job(company="ACME"), prefs).passed

    def test_an_excluded_ats_fails(self) -> None:
        prefs = Prefs.model_validate({"scope": {"exclude_ats": ["ashby"]}})
        assert not evaluate(make_job(), prefs).passed


class TestValidation:
    def test_an_unknown_employment_type_is_rejected_loudly(self) -> None:
        """A typo must not silently match nothing and empty the list."""
        with pytest.raises(ValueError, match="unknown employment_types"):
            WorkPrefs(employment_types=["fulltime"])

    def test_an_unknown_workplace_type_is_rejected_loudly(self) -> None:
        with pytest.raises(ValueError, match="unknown workplace_types"):
            WorkPrefs(workplace_types=["wfh"])

    def test_an_inverted_year_band_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="below min_years"):
            ExperiencePrefs(min_years=8, max_years=2)

    def test_every_declared_vocabulary_value_is_accepted(self) -> None:
        """The options the API serves must all be settable."""
        WorkPrefs(
            workplace_types=list(WORKPLACE_TYPES), employment_types=list(EMPLOYMENT_TYPES)
        )


class TestVersioning:
    def test_the_version_is_stable_across_identical_prefs(self) -> None:
        assert Prefs().version == Prefs().version

    def test_list_order_does_not_change_the_version(self) -> None:
        a = Prefs(work=WorkPrefs(workplace_types=["remote", "hybrid"]))
        b = Prefs(work=WorkPrefs(workplace_types=["hybrid", "remote"]))
        assert a.version == b.version

    def test_a_real_change_changes_the_version(self) -> None:
        assert Prefs().version != Prefs(include_unstated=False).version

    def test_updated_at_is_excluded_from_the_version(self) -> None:
        """Re-saving unchanged preferences must not invalidate anything."""
        a = Prefs()
        b = Prefs(updated_at=NOW)
        assert a.version == b.version

    def test_an_integral_float_hashes_as_an_int(self) -> None:
        """Regression: the version must not depend on how a value was built.

        A YAML round-trip turns `3000000` into `3000000.0`. The two are equal
        but serialize differently, so hashing the raw values produced a fresh
        `prefs_version` on every save of unchanged preferences — and a fresh
        version is exactly how the dashboard decides the Matches list is
        stale. Caught by `test_save_then_load_round_trips`.
        """
        a = Prefs(compensation=CompensationPrefs(min_annual_inr=3_000_000))
        b = Prefs.model_validate({"compensation": {"min_annual_inr": 3000000.0}})
        assert a.version == b.version

    def test_a_genuinely_different_floor_still_changes_the_version(self) -> None:
        """The canonicalizer must not flatten real differences."""
        a = Prefs(compensation=CompensationPrefs(min_annual_inr=3_000_000))
        b = Prefs(compensation=CompensationPrefs(min_annual_inr=4_000_000))
        assert a.version != b.version


class TestPersistence:
    def test_a_missing_file_yields_defaults_not_an_error(self, tmp_path: Path) -> None:
        """Unlike the profile, absent preferences just mean "no view expressed"."""
        prefs = load_prefs(tmp_path / "nope.yaml")
        assert prefs.version == Prefs().version

    def test_a_malformed_file_raises_rather_than_degrading(self, tmp_path: Path) -> None:
        """Silently using defaults would show a list the file does not describe."""
        bad = tmp_path / "prefs.yaml"
        bad.write_text("work: [this is not a mapping]\n", encoding="utf-8")
        with pytest.raises(PrefsError):
            load_prefs(bad)

    def test_save_then_load_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "prefs.yaml"
        original = Prefs(
            work=WorkPrefs(workplace_types=["remote", "hybrid"], employment_types=["contract"]),
            experience=ExperiencePrefs(min_years=3, max_years=9),
            include_unstated=False,
        )
        save_prefs(original, path)
        loaded = load_prefs(path)
        assert loaded.version == original.version
        assert loaded.work.employment_types == ["contract"]
        assert loaded.include_unstated is False

    def test_a_saved_file_carries_the_explanatory_header(self, tmp_path: Path) -> None:
        """Hand-editing stays first-class, so the file has to explain itself."""
        path = tmp_path / "prefs.yaml"
        save_prefs(Prefs(), path)
        body = path.read_text(encoding="utf-8")
        assert body.lstrip().startswith("#")
        assert "ELIGIBILITY" in body

    def test_saving_stamps_updated_at(self, tmp_path: Path) -> None:
        stored = save_prefs(Prefs(), tmp_path / "prefs.yaml")
        assert stored.updated_at is not None


@pytest.mark.anyio
class TestSqlAndPythonAgree:
    """`sql_clause` is documented as a SUPERSET of `evaluate`. Assert it.

    The SQL form cannot express pay (needs the FX table and period inference)
    or years (needs the JD prose), so it must never exclude a row that
    `evaluate` would admit — otherwise the list query would hide a genuine
    match before the exact verdict ever ran.
    """

    async def test_sql_never_excludes_a_row_python_admits(self, session_factory) -> None:
        rows = [
            make_job(id=1, workplace_type="remote", employment_type="full_time"),
            make_job(id=2, workplace_type="onsite", employment_type="full_time"),
            make_job(id=3, workplace_type=None, employment_type=None),
            make_job(id=4, workplace_type="remote", employment_type="contract"),
            make_job(id=5, workplace_type="hybrid", employment_type="full_time"),
        ]
        for i, row in enumerate(rows, start=1):
            row.source_key = f"ashby:acme:{i}"

        async with session_factory() as session:
            session.add_all(rows)
            await session.commit()

            for prefs in (
                Prefs(),
                Prefs(work=WorkPrefs(workplace_types=["remote", "hybrid"])),
                Prefs(work=WorkPrefs(workplace_types=[], employment_types=[])),
                Prefs(include_unstated=False),
            ):
                sql_ids = {
                    r.id
                    for r in (
                        await session.execute(select(JobPosting).where(sql_clause(prefs)))
                    )
                    .scalars()
                    .all()
                }
                all_rows = (await session.execute(select(JobPosting))).scalars().all()
                python_ids = {r.id for r in all_rows if evaluate(r, prefs).passed}

                assert python_ids <= sql_ids, (
                    f"SQL dropped rows Python admits: {python_ids - sql_ids} "
                    f"(prefs {prefs.version})"
                )


class TestPostingAge:
    """Older than a month is no use — a hard limit, never admitted on silence."""

    def test_a_month_old_posting_fails(self) -> None:
        v = evaluate(make_job(posted_at=NOW - timedelta(days=31)), Prefs(), now=NOW)
        assert not v.passed
        assert any(r.startswith("posted_too_old:31d") for r in v.reasons), v.reasons

    def test_the_cutoff_is_an_instant_not_whole_days(self) -> None:
        """Rounding the age down admitted 30.99-day-old jobs: 619 rows in
        Matches against 612 in Jobs for the same '≤ 30d'."""
        just_over = make_job(posted_at=NOW - timedelta(days=30, hours=6))
        assert not evaluate(just_over, Prefs(), now=NOW).passed
        just_under = make_job(posted_at=NOW - timedelta(days=29, hours=23))
        assert evaluate(just_under, Prefs(), now=NOW).passed

    def test_a_tighter_window_applies(self) -> None:
        prefs = Prefs(freshness=FreshnessPrefs(max_age_days=7))
        assert not evaluate(make_job(posted_at=NOW - timedelta(days=8)), prefs, now=NOW).passed

    def test_the_window_cannot_be_widened_past_thirty_days(self) -> None:
        with pytest.raises(ValueError):
            FreshnessPrefs(max_age_days=60)

    def test_an_undated_posting_ages_from_first_seen(self) -> None:
        job = make_job(posted_at=None, first_seen_at=NOW - timedelta(days=40))
        assert not evaluate(job, Prefs(), now=NOW).passed


class TestTransfer:
    def test_a_row_outside_the_sent_scope_fails_first(self) -> None:
        prefs = Prefs(transfer=TransferPrefs(filters=JobFilterSet(remote=True)))
        v = evaluate(make_job(), prefs, in_transfer=False, now=NOW)
        assert not v.passed
        assert v.reasons[0] == "not_transferred"

    def test_without_a_transfer_every_row_is_in_scope(self) -> None:
        assert evaluate(make_job(), Prefs(), in_transfer=False, now=NOW).passed

    def test_the_scope_is_part_of_the_version(self) -> None:
        a = Prefs(transfer=TransferPrefs(filters=JobFilterSet(remote=True)))
        b = Prefs(transfer=TransferPrefs(filters=JobFilterSet(remote=False)))
        assert a.version != b.version != Prefs().version

    def test_transfer_bookkeeping_is_not_part_of_the_version(self) -> None:
        f = JobFilterSet(remote=True)
        a = Prefs(transfer=TransferPrefs(filters=f, count_at_transfer=10))
        b = Prefs(transfer=TransferPrefs(filters=f, count_at_transfer=99, transferred_at=NOW))
        assert a.version == b.version

    def test_it_round_trips_through_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "prefs.yaml"
        f = JobFilterSet(remote=True, eligibility_pass=True, posted_within_days=30, q="python")
        save_prefs(Prefs(transfer=TransferPrefs(filters=f, count_at_transfer=534)), path)
        loaded = load_prefs(path)
        assert loaded.transfer is not None
        assert loaded.transfer.filters == f
        assert loaded.transfer.count_at_transfer == 534


def test_the_experience_floor_defaults_to_five_years() -> None:
    """At 6 the band dropped 162 eligible jobs asking exactly 5+ years."""
    assert Prefs().experience.min_years == 5
    assert evaluate(make_job(), Prefs(), now=NOW).passed  # JD asks 5+
