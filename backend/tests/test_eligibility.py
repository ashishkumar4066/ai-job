"""Eligibility filter tests — the Phase 1 acceptance criteria.

These run against the **shipped** `config/filters.yaml` (IN + worldwide,
remote-only, INR 20L/yr floor), not the permissive test config the ingest suite
uses, so they assert the rules that actually ship.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import BACKEND_ROOT
from app.eligibility import FilterConfig, evaluate, load_filters
from app.geo import WORLDWIDE, country_code, format_utc_offset, resolve_eligibility, split_location_text
from app.normalize import parse_salary_text

SHIPPED_FILTERS = BACKEND_ROOT / "config" / "filters.yaml"


@pytest.fixture(scope="module")
def filters() -> FilterConfig:
    return load_filters(SHIPPED_FILTERS)


def verdict(
    filters: FilterConfig,
    *,
    locations: list[str],
    remote: bool | None = True,
    currency: str | None = None,
    salary_min: float | None = None,
    salary_max: float | None = None,
    # Rule 4 is exercised in `test_roles.py`. These tests isolate the location,
    # remote and pay rules, so they hold the role constant at something that
    # unambiguously passes — a title and a JD that state nothing about years.
    title: str = "Senior Software Engineer",
    description_text: str | None = None,
):
    return evaluate(
        location_eligibility=locations,
        remote=remote,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        title=title,
        description_text=description_text,
        filters=filters,
    )


# ------------------------------------------------------------- Shipped config
class TestShippedFilterConfig:
    def test_repo_file_exists_and_matches_the_spec(self, filters: FilterConfig) -> None:
        assert SHIPPED_FILTERS.exists()
        assert filters.allowed_locations == ["IN", WORLDWIDE]
        # Off by design: `location_eligibility` already implies remote-from-India,
        # and `detect_remote` under-reports (GitLab, an all-remote employer,
        # returns remote=false on 42 rows).
        assert filters.require_remote is False
        assert filters.min_annual_salary_inr == 2_000_000
        assert filters.fx_to_inr["INR"] == 1.0
        assert filters.fx_to_inr["USD"] > 1

    def test_missing_file_degrades_to_defaults(self, tmp_path: Path) -> None:
        # Losing the config must not stop ingestion.
        loaded = load_filters(tmp_path / "nope.yaml")
        assert loaded.allowed_locations == ["IN", WORLDWIDE]

    def test_malformed_file_degrades_to_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "filters.yaml"
        path.write_text("just a string, not a mapping\n", encoding="utf-8")
        assert load_filters(path).min_annual_salary_inr == 2_000_000

    def test_config_is_normalized(self) -> None:
        loaded = FilterConfig(
            allowed_locations=["in", "Worldwide", "in"], fx_to_inr={"usd": 90, " eur ": 95}
        )
        assert loaded.allowed_locations == ["IN", WORLDWIDE]
        assert loaded.fx_to_inr == {"USD": 90.0, "EUR": 95.0}


# -------------------------------------------------------- The three rules
class TestLocationRule:
    def test_worldwide_job_passes(self, filters: FilterConfig) -> None:
        """Acceptance: a worldwide-eligible remote job passes the filter."""
        result = verdict(filters, locations=[WORLDWIDE])

        assert result.passed is True
        assert any(r.startswith("location_ok") for r in result.reasons)

    def test_india_eligible_job_passes(self, filters: FilterConfig) -> None:
        assert verdict(filters, locations=["IN"]).passed is True

    def test_us_only_remote_job_is_flagged_with_a_location_reason(
        self, filters: FilterConfig
    ) -> None:
        """Acceptance: a US-candidates-only remote job fails on location."""
        result = verdict(filters, locations=["US"])

        assert result.passed is False
        location_reasons = [r for r in result.reasons if r.startswith("location_blocked")]
        assert location_reasons, "the failure must name location as the cause"
        assert "US" in location_reasons[0]
        # Only location blocked it; the other two rules were satisfied.
        assert any(r.startswith("remote_ok") for r in result.reasons)

    def test_a_region_containing_india_passes(self, filters: FilterConfig) -> None:
        codes, _, _ = resolve_eligibility(["APAC"])
        assert verdict(filters, locations=codes).passed is True

    def test_office_location_does_not_decide_eligibility(self, filters: FilterConfig) -> None:
        """A London employer that hires India-based remote staff is eligible.

        Real row: Innovify's "Full-Stack AI Engineer" is `locations: ["London"]`
        with `acceptedRemoteLocationNames: ["India"]`. Eligibility keys off who
        they will hire, never the office address.
        """
        assert verdict(filters, locations=["IN"], remote=True).passed is True

    def test_missing_eligibility_data_fails_with_an_explicit_reason(
        self, filters: FilterConfig
    ) -> None:
        result = verdict(filters, locations=[])
        assert result.passed is False
        assert any(r.startswith("location_unknown") for r in result.reasons)


class TestRemoteRule:
    """The rule ships disabled; these cover it for when it is switched on.

    It is off in `config/filters.yaml` because `detect_remote()` under-reports —
    GitLab is all-remote yet 42 of its Greenhouse rows come back `remote=false`,
    and gating on that silently drops 190 India-eligible jobs.
    """

    @pytest.fixture
    def strict(self) -> FilterConfig:
        return FilterConfig(require_remote=True)

    def test_onsite_role_is_blocked(self, strict: FilterConfig) -> None:
        result = verdict(strict, locations=["IN"], remote=False)
        assert result.passed is False
        assert any(r.startswith("remote_blocked") for r in result.reasons)

    def test_unknown_remote_flag_is_blocked(self, strict: FilterConfig) -> None:
        assert verdict(strict, locations=["IN"], remote=None).passed is False

    def test_remote_role_passes(self, strict: FilterConfig) -> None:
        assert verdict(strict, locations=["IN"], remote=True).passed is True

    def test_the_shipped_config_does_not_gate_on_remote(self, filters: FilterConfig) -> None:
        assert verdict(filters, locations=["IN"], remote=False).passed is True


class TestPayRule:
    def test_unstated_salary_passes_and_is_flagged(self, filters: FilterConfig) -> None:
        """~84% of postings state no pay; dropping them would empty the board."""
        result = verdict(filters, locations=["IN"])
        assert result.passed is True
        assert any(r.startswith("pay_unstated") for r in result.reasons)

    def test_a_currency_without_an_amount_is_still_unstated(self, filters: FilterConfig) -> None:
        result = verdict(filters, locations=["IN"], currency="USD")
        assert result.passed is True
        assert any(r.startswith("pay_unstated") for r in result.reasons)

    @pytest.mark.parametrize("currency", ["USD", "EUR", "GBP", "INR"])
    def test_any_currency_is_acceptable_above_the_floor(
        self, filters: FilterConfig, currency: str
    ) -> None:
        """Employer nationality and currency are irrelevant — only the amount."""
        result = verdict(filters, locations=["IN"], currency=currency, salary_max=10_000_000)
        assert result.passed is True

    def test_inr_below_the_floor_is_blocked(self, filters: FilterConfig) -> None:
        # Real row: Idyaite, "Sr. Consultant - Full Stack Engineer", INR 6L-7.2L.
        result = verdict(
            filters, locations=["IN"], currency="INR", salary_min=600_000, salary_max=720_000
        )
        assert result.passed is False
        assert any(r.startswith("pay_blocked") for r in result.reasons)

    def test_the_top_of_a_range_decides(self, filters: FilterConfig) -> None:
        """"INR 15L - 30L" is worth surfacing even though its floor is low."""
        result = verdict(
            filters, locations=["IN"], currency="INR", salary_min=1_500_000, salary_max=3_000_000
        )
        assert result.passed is True

    def test_usd_above_the_floor_passes(self, filters: FilterConfig) -> None:
        # Real row: The Prompt Academy, "Software Engineer", $110k-$140k.
        result = verdict(
            filters, locations=[WORLDWIDE], currency="USD", salary_min=110_000, salary_max=140_000
        )
        assert result.passed is True

    def test_an_unconvertible_currency_passes_rather_than_guessing(
        self, filters: FilterConfig
    ) -> None:
        """Being unable to price a job is not evidence that it pays badly."""
        result = verdict(filters, locations=["IN"], currency="XYZ", salary_max=99)
        assert result.passed is True
        assert any(r.startswith("pay_unknown_currency") for r in result.reasons)

    def test_all_three_rules_must_hold(self, filters: FilterConfig) -> None:
        assert verdict(filters, locations=["US"], remote=False).passed is False


class TestSalaryPeriodInference:
    """An hourly or monthly rate must not be read as an annual figure.

    Without this, Search Atlas's "$25 - $30" (an hourly rate worth ~INR 55L/yr)
    compares as INR 2,640/yr and is dropped — a good job discarded for a reason
    no human would ever see.
    """

    def test_hourly_usd_is_annualized_and_passes(self, filters: FilterConfig) -> None:
        result = verdict(filters, locations=["IN"], currency="USD", salary_min=25, salary_max=30)
        assert result.passed is True, result.reasons
        assert any("hourly" in r for r in result.reasons)

    def test_a_genuinely_low_hourly_rate_still_fails(self, filters: FilterConfig) -> None:
        # $3/hr -> ~INR 5.5L/yr, under the floor even after annualizing.
        result = verdict(filters, locations=["IN"], currency="USD", salary_max=3)
        assert result.passed is False

    def test_monthly_inr_is_annualized(self, filters: FilterConfig) -> None:
        # Real row: "INR 30,000 - 40,000" is monthly -> INR 4.8L/yr, still low.
        result = verdict(filters, locations=["IN"], currency="INR", salary_max=40_000)
        assert result.passed is False
        assert any("monthly" in r for r in result.reasons)

    def test_monthly_usd_clears_the_floor(self, filters: FilterConfig) -> None:
        # $2,000/mo -> $24k/yr -> ~INR 21L/yr.
        result = verdict(filters, locations=["IN"], currency="USD", salary_max=2_000)
        assert result.passed is True
        assert any("monthly" in r for r in result.reasons)

    def test_an_annual_figure_is_left_alone(self, filters: FilterConfig) -> None:
        result = verdict(filters, locations=["IN"], currency="USD", salary_max=120_000)
        assert result.passed is True
        assert not any("read as" in r for r in result.reasons)


# ------------------------------------------------------------- Geo resolution
class TestGeoResolution:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Worldwide", WORLDWIDE),
            ("Global", WORLDWIDE),
            ("Anywhere", WORLDWIDE),
            ("anywhere in the world", WORLDWIDE),
        ],
    )
    def test_worldwide_synonyms(self, text: str, expected: str) -> None:
        codes, _, _ = resolve_eligibility([text])
        assert codes == [expected]

    def test_us_state_codes_do_not_become_countries(self) -> None:
        """"San Francisco, CA" is California, not Canada."""
        codes, _, _ = resolve_eligibility(split_location_text("San Francisco, CA"))
        assert codes == ["US"]
        assert country_code("CA") == "US"
        assert country_code("Canada") == "CA"

    def test_indian_cities_resolve_without_a_country(self) -> None:
        # GitLab writes "Remote, Bangalore" with no country anywhere.
        codes, _, _ = resolve_eligibility(split_location_text("Remote, Bangalore"))
        assert codes == ["IN"]

    def test_spaced_dash_is_a_separator(self) -> None:
        # Turing writes "India - Remote"; GitLab writes "Remote, US".
        assert resolve_eligibility(split_location_text("India - Remote"))[0] == ["IN"]
        assert resolve_eligibility(split_location_text("Remote - US"))[0] == ["US"]

    def test_multi_country_string_keeps_every_country(self) -> None:
        codes, _, _ = resolve_eligibility(
            split_location_text("Remote, Canada; Remote, United States")
        )
        assert set(codes) == {"CA", "US"}

    def test_unresolved_tokens_are_returned_not_dropped(self) -> None:
        _, _, unresolved = resolve_eligibility(split_location_text("Atlantis, HQ"))
        assert "Atlantis" in unresolved and "HQ" in unresolved

    def test_empty_means_worldwide_only_when_asked(self) -> None:
        assert resolve_eligibility([])[0] == []
        assert resolve_eligibility([], empty_means_worldwide=True)[0] == [WORLDWIDE]

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [(0, "UTC+00:00"), (5.5, "UTC+05:30"), (-8, "UTC-08:00"), (-9.5, "UTC-09:30"), (14, "UTC+14:00")],
    )
    def test_utc_offsets_render_readably(self, offset: float, expected: str) -> None:
        assert format_utc_offset(offset) == expected


# --------------------------------------------------------------- Salary text
class TestSalaryParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("$150k - $230k", (150000, 230000, "USD")),
            ("$45,000 - $50,000", (45000, 50000, "USD")),
            ("$18 - $22/hr", (18, 22, "USD")),
            ("$36k", (36000, None, "USD")),
            ("OTE $25k - $35k", (25000, 35000, "USD")),
            ("$50-$75 /hour", (50, 75, "USD")),
            # Remotive mixes separator conventions inside one feed.
            ("$31,2k- $52k", (31200, 52000, "USD")),
            ("€80k - €100k", (80000, 100000, "EUR")),
            ("120000 USD", (120000, None, "USD")),
            ("", (None, None, None)),
            (None, (None, None, None)),
            ("Competitive", (None, None, None)),
        ],
    )
    def test_live_salary_strings(self, text: str | None, expected: tuple) -> None:
        assert parse_salary_text(text) == expected

    def test_bounds_are_ordered(self) -> None:
        assert parse_salary_text("$230k - $150k")[:2] == (150000, 230000)
