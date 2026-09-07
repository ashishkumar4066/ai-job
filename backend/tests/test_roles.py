"""Role + seniority filter tests.

Every title below is a real one, taken from the 5,251 open rows in the live DB
at the time the rule was written. The point of using real titles is that the
two bugs this module was born with were both invisible against invented ones:
a synthetic "Senior Backend Engineer" has no mentor bullet to misparse and no
"AI Product" suffix to over-match.
"""

from __future__ import annotations

import pytest

from app.config import BACKEND_ROOT
from app.eligibility import evaluate, load_filters
from app.roles import DEFAULT_FAMILIES, JUNIOR, RoleRules, classify, years_required

SHIPPED_FILTERS = BACKEND_ROOT / "config" / "filters.yaml"


@pytest.fixture(scope="module")
def rules() -> RoleRules:
    return load_filters(SHIPPED_FILTERS).role


# --------------------------------------------------------------------------
# Years parsing — the subtle half
# --------------------------------------------------------------------------
# Palantir, "Senior Software Engineer - Observability". The headline is 5+; the
# 2+ and 1+ lines are narrower slices of it. Reading the minimum files a real
# senior role as junior, which is exactly what the first cut of this module did.
PALANTIR_OBSERVABILITY = (
    "What We Require: 5+ years of professional software development experience. "
    "2+ years of experience contributing to the system design or architecture "
    "of new and existing systems. 1+ years of experience as a mentor, tech lead "
    "or leading an engineering team."
)


def test_years_takes_the_headline_not_the_smallest_subclause() -> None:
    assert years_required(PALANTIR_OBSERVABILITY) == 5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15+ years industry experience building large-scale systems", 15),
        ("9+ years of professional engineering experience, excluding internships", 9),
        ("We are looking for 4-8 years of experience", 4),
        ("Minimum 3 yrs experience with Go", 3),
        ("", None),
        (None, None),
        ("A great place to work with experience in mind", None),
        # Prose, not a requirement — above the plausibility ceiling.
        ("Serving customers for 50 years, we value experience", None),
    ],
)
def test_years_parsing(text: str | None, expected: int | None) -> None:
    assert years_required(text) == expected


def test_years_does_not_leap_across_a_sentence() -> None:
    """`experience` must attach to the figure, not merely follow it somewhere."""
    assert years_required("Founded 12 years ago. We value experience above all.") is None


# --------------------------------------------------------------------------
# Wanted families
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Senior Software Engineer - Backend", "software"),
        ("Forward Deployed Software Engineer", "software"),
        ("Staff Engineer, Seller Systems", "staff"),
        ("Senior Backend Engineer", "backend"),
        ("Brave News Backend Engineer", "backend"),
        ("Senior Frontend Engineer", "frontend"),
        ("Senior / Staff Fullstack Engineer", "fullstack"),
        ("Full Stack Engineer", "fullstack"),
        ("Machine Learning Engineer", "ai_ml"),
        ("AI/ML Engineer", "ai_ml"),
        ("Staff Engineer - Platform Engineering", "staff"),
        # Startup shorthand for a full-stack engineer; enabled on purpose.
        ("Senior / Staff Product Engineer", "product_engineer"),
    ],
)
def test_wanted_titles_pass(title: str, family: str, rules: RoleRules) -> None:
    verdict = classify(title, "", rules)
    assert verdict.passed, verdict.reasons
    assert verdict.family == family


# --------------------------------------------------------------------------
# The over-matching guard — these were real false rejections
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "title",
    [
        # A bare "product" match rejects this; the role head is Software Engineer.
        "Software Engineer, AI Product",
        "Software Engineer, Core Product",
        "Software Engineer, Monetization Product & Platform",
        # A bare "partner" match rejects this one.
        "Senior Staff Software Engineer - App and Partner Ecosystem",
        "Software Engineer, Enterprise Product",
        # "II" and "10" must survive the junior `engineer i|1` pattern.
        "Software Engineer II",
        "Software Engineer III",
        # "Developer Support" is the team, not the role — the support patterns
        # must not reach across a comma and eat the engineering role head.
        "Software Engineer, Developer Support",
        # Nagarro's real IC ladder. "Associate Staff Engineer" is a mid-level
        # engineering title (21 live rows), not an entry-level one, so bare
        # `associate` is deliberately NOT a junior marker.
        "Associate Staff Engineer, Python",
        # L3+ is a mid-level band at most large employers, so only L1/L2
        # are junior markers. These must not be swept up with them.
        "Backend Engineer (L3)",
        "Staff Engineer L5",
    ],
)
def test_qualifiers_do_not_reject_engineering_roles(title: str, rules: RoleRules) -> None:
    verdict = classify(title, "", rules)
    assert verdict.passed, f"{title} wrongly rejected: {verdict.reasons}"


# --------------------------------------------------------------------------
# Rejections, each with its own reason class
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "reason_prefix"),
    [
        ("Software Engineer, New Grad", "role_junior"),
        ("Forward Deployed Software Engineer, Internship - Commercial", "role_junior"),
        ("Junior Frontend Developer", "role_junior"),
        ("Software Engineer I", "role_junior"),
        ("Graduate Software Engineer Program", "role_junior"),
        ("Director of Engineering", "role_too_senior"),
        ("Principal Engineer - Privacy", "role_too_senior"),
        ("Head of Corporate Engineering", "role_too_senior"),
        ("VP of Engineering", "role_too_senior"),
        ("Product Designer", "role_blocked"),
        ("Engineering Manager - Backend", "role_blocked"),
        ("Technical Program Manager (TPM)", "role_blocked"),
        ("Staff Data Scientist", "role_blocked"),
        ("Technical Writer", "role_blocked"),
        ("Deployment Strategist", "role_blocked"),
        ("Account Executive", "role_blocked"),
        # Support tiers. Both leaked past a pattern that spelled only
        # "technical support" — these are the forms boards actually use.
        ("Software Engineer - L3 Support", "role_blocked"),
        ("Associate Staff Engineer, Tech support", "role_blocked"),
        ("Support Engineer", "role_blocked"),
        # Tier suffix — leaked past `engineer\s*1`, which needs the digit
        # adjacent to the noun.
        ("Robotics Software Engineer - L1", "role_junior"),
        ("Software Engineer L2", "role_junior"),
        # Real engineering, family switched off by config.
        ("DevOps Engineer", "role_blocked"),
        ("Site Reliability Engineer - US Government", "role_blocked"),
        ("Data Engineer", "role_blocked"),
        ("Mobile App Engineer (Xamarin)", "role_blocked"),
        ("Mobility Tax Analyst", "role_blocked"),
    ],
)
def test_unwanted_titles_are_rejected(title: str, reason_prefix: str, rules: RoleRules) -> None:
    verdict = classify(title, "", rules)
    assert not verdict.passed, f"{title} wrongly accepted"
    assert verdict.reasons[0].startswith(reason_prefix), verdict.reasons


@pytest.mark.parametrize(
    ("title", "is_junior"),
    [
        ("Robotics Software Engineer - L1", True),
        ("Software Engineer L2", True),
        ("Backend Engineer (L3)", False),
        ("Staff Engineer L5", False),
        # Must not fire mid-token: the enclosing  needs a non-word character
        # before the `l`, which "SQL1" and "HTML1" do not provide.
        ("Senior SQL1 Developer", False),
        ("HTML1 Engineer", False),
    ],
)
def test_l_tier_is_a_junior_marker_only_at_l1_l2(title: str, is_junior: bool) -> None:
    assert bool(JUNIOR.search(title)) is is_junior


def test_a_title_rejection_names_the_real_problem(rules: RoleRules) -> None:
    """Junior beats family: the useful reason is the level, not the family."""
    verdict = classify("Product Designer, New Grad", "", rules)
    assert verdict.reasons[0].startswith("role_junior")


def test_missing_title_fails_rather_than_passing(rules: RoleRules) -> None:
    assert not classify("", "", rules).passed
    assert not classify("   ", "", rules).passed


# --------------------------------------------------------------------------
# The years window
# --------------------------------------------------------------------------
def test_unstated_experience_passes(rules: RoleRules) -> None:
    """Same treatment as an unstated salary — most JDs name no figure."""
    verdict = classify("Staff Software Engineer", "We build great things.", rules)
    assert verdict.passed
    assert verdict.years is None
    assert "unstated" in verdict.reasons[0]


def test_a_wanted_title_does_not_rescue_an_out_of_range_bar(rules: RoleRules) -> None:
    """Staff is a wanted family, but Databricks asking 15 years is a real bar."""
    verdict = classify(
        "Senior Staff Software Engineer - Delta",
        "15+ years industry experience building distributed systems",
        rules,
    )
    assert not verdict.passed
    assert verdict.reasons[0].startswith("role_over_experienced")
    assert verdict.years == 15


@pytest.mark.parametrize(
    ("years", "passes"),
    [(1, False), (2, True), (5, True), (7, True), (8, True), (9, False), (12, False)],
)
def test_years_window_boundaries(years: int, passes: bool, rules: RoleRules) -> None:
    verdict = classify(
        "Senior Software Engineer", f"{years}+ years of professional experience", rules
    )
    assert verdict.passed is passes, verdict.reasons


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def test_shipped_config_matches_the_intended_families(rules: RoleRules) -> None:
    assert rules.enabled
    assert list(rules.families) == list(DEFAULT_FAMILIES)
    assert (rules.min_years, rules.max_years) == (2, 8)
    assert rules.unknown_families == []


def test_enabling_a_family_admits_it_without_touching_code() -> None:
    off = RoleRules()
    on = RoleRules(families=[*DEFAULT_FAMILIES, "devops_sre", "data", "mobile"])
    for title in ("DevOps Engineer", "Data Engineer", "iOS Engineer"):
        assert not classify(title, "", off).passed
        assert classify(title, "", on).passed


def test_a_typo_in_families_is_reported_not_silently_dropped() -> None:
    rules = RoleRules(families=["software", "backendd", "nonsense"])
    assert rules.unknown_families == ["backendd", "nonsense"]
    assert classify("Senior Software Engineer", "", rules).passed


def test_disabling_the_rule_passes_everything() -> None:
    rules = RoleRules(enabled=False)
    assert classify("Mobility Tax Analyst", "", rules).passed


# --------------------------------------------------------------------------
# Integration with the other three rules
# --------------------------------------------------------------------------
def test_role_is_a_gate_on_an_otherwise_perfect_job() -> None:
    """Worldwide + remote + USD 150k, and still rejected on the role alone."""
    filters = load_filters(SHIPPED_FILTERS)
    result = evaluate(
        location_eligibility=["worldwide"],
        remote=True,
        salary_max=150_000,
        salary_currency="USD",
        title="Product Designer",
        description_text="",
        filters=filters,
    )
    assert not result.passed
    assert any(r.startswith("role_blocked") for r in result.reasons)
    # The other three still report their own verdicts — a single failing rule
    # must not hide why the rest passed.
    assert any(r.startswith("location_ok") for r in result.reasons)
    assert any(r.startswith("pay_ok") for r in result.reasons)


def test_a_wanted_role_clears_all_four_rules() -> None:
    filters = load_filters(SHIPPED_FILTERS)
    result = evaluate(
        location_eligibility=["IN"],
        remote=True,
        salary_max=150_000,
        salary_currency="USD",
        title="Senior Backend Engineer",
        description_text=PALANTIR_OBSERVABILITY,
        filters=filters,
    )
    assert result.passed, result.reasons
    assert any(r.startswith("role_ok") for r in result.reasons)


def test_omitting_the_title_fails_closed() -> None:
    """A caller that forgets the new arguments must not silently pass jobs."""
    filters = load_filters(SHIPPED_FILTERS)
    result = evaluate(
        location_eligibility=["worldwide"],
        remote=True,
        salary_currency="USD",
        salary_max=150_000,
        filters=filters,
    )
    assert not result.passed
