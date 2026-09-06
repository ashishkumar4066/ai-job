"""Eligibility + currency filter tests — the Phase 1 acceptance criteria.

These run against the **shipped** `config/filters.yaml` (IN + worldwide, USD),
not the permissive test config the ingest suite uses, so they assert the rules
that actually ship.
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
    currency: str | None = None,
    us_employer: bool | None = None,
):
    return evaluate(
        location_eligibility=locations,
        salary_currency=currency,
        is_us_employer=us_employer,
        filters=filters,
    )


# ------------------------------------------------------------- Shipped config
class TestShippedFilterConfig:
    def test_repo_file_exists_and_matches_the_spec(self, filters: FilterConfig) -> None:
        assert SHIPPED_FILTERS.exists()
        assert filters.allowed_locations == ["IN", WORLDWIDE]
        assert filters.required_currency == "USD"
        assert filters.allow_us_employer_when_currency_unknown is True

    def test_missing_file_degrades_to_defaults(self, tmp_path: Path) -> None:
        # Losing the config must not stop ingestion.
        loaded = load_filters(tmp_path / "nope.yaml")
        assert loaded.allowed_locations == ["IN", WORLDWIDE]

    def test_malformed_file_degrades_to_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "filters.yaml"
        path.write_text("just a string, not a mapping\n", encoding="utf-8")
        assert load_filters(path).required_currency == "USD"

    def test_config_is_normalized(self) -> None:
        loaded = FilterConfig(allowed_locations=["in", "Worldwide", "in"], required_currency="usd")
        assert loaded.allowed_locations == ["IN", WORLDWIDE]
        assert loaded.required_currency == "USD"


# -------------------------------------------------------- The two rules
class TestLocationRule:
    def test_worldwide_usd_job_passes(self, filters: FilterConfig) -> None:
        """Acceptance: a worldwide-eligible USD job passes the filter."""
        result = verdict(filters, locations=[WORLDWIDE], currency="USD")

        assert result.passed is True
        assert any(r.startswith("location_ok") for r in result.reasons)
        assert any(r.startswith("pay_ok") for r in result.reasons)

    def test_india_eligible_usd_job_passes(self, filters: FilterConfig) -> None:
        assert verdict(filters, locations=["IN"], currency="USD").passed is True

    def test_us_only_remote_job_is_flagged_with_a_location_reason(
        self, filters: FilterConfig
    ) -> None:
        """Acceptance: a US-candidates-only remote job fails on location."""
        result = verdict(filters, locations=["US"], currency="USD")

        assert result.passed is False
        location_reasons = [r for r in result.reasons if r.startswith("location_blocked")]
        assert location_reasons, "the failure must name location as the cause"
        assert "US" in location_reasons[0]
        # The pay half was fine; only location blocked it.
        assert any(r.startswith("pay_ok") for r in result.reasons)

    def test_a_region_containing_india_passes(self, filters: FilterConfig) -> None:
        codes, _, _ = resolve_eligibility(["APAC"])
        assert verdict(filters, locations=codes, currency="USD").passed is True

    def test_missing_eligibility_data_fails_with_an_explicit_reason(
        self, filters: FilterConfig
    ) -> None:
        result = verdict(filters, locations=[], currency="USD")
        assert result.passed is False
        assert any(r.startswith("location_unknown") for r in result.reasons)


class TestPayRule:
    def test_non_usd_currency_is_blocked(self, filters: FilterConfig) -> None:
        result = verdict(filters, locations=[WORLDWIDE], currency="PLN")
        assert result.passed is False
        assert any("PLN" in r and r.startswith("pay_blocked") for r in result.reasons)

    def test_unknown_currency_passes_for_a_known_us_employer(
        self, filters: FilterConfig
    ) -> None:
        result = verdict(filters, locations=[WORLDWIDE], currency=None, us_employer=True)
        assert result.passed is True

    def test_unknown_currency_and_unknown_employer_is_blocked(
        self, filters: FilterConfig
    ) -> None:
        result = verdict(filters, locations=[WORLDWIDE], currency=None, us_employer=None)
        assert result.passed is False
        assert any("employer origin unknown" in r for r in result.reasons)

    def test_unknown_currency_and_non_us_employer_is_blocked(
        self, filters: FilterConfig
    ) -> None:
        result = verdict(filters, locations=[WORLDWIDE], currency=None, us_employer=False)
        assert result.passed is False
        assert any("not US-based" in r for r in result.reasons)

    def test_the_us_employer_escape_hatch_can_be_switched_off(self) -> None:
        strict = FilterConfig(allow_us_employer_when_currency_unknown=False)
        result = verdict(strict, locations=[WORLDWIDE], currency=None, us_employer=True)
        assert result.passed is False

    def test_both_rules_must_hold(self, filters: FilterConfig) -> None:
        assert verdict(filters, locations=["US"], currency="PLN").passed is False


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
