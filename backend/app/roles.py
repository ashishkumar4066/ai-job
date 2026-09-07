"""The role + seniority filter.

The fourth eligibility rule. Location, remote and pay all key off *structured*
fields a board hands us; this one has only the title and the JD prose, so it is
the only rule that has to read English. It answers two questions:

  1. Is this one of the role families I want?  (software / staff engineer,
     software developer, frontend, backend, full-stack, AI/ML, product engineer)
  2. Is it pitched at my level — roughly 5-7 years?

Both are decided from the **title** first, because a title is short, written to
be scanned, and almost never lies about the role family. The JD body is
consulted only for the years-of-experience figure, which titles rarely carry.

Why the title carries the seniority verdict
-------------------------------------------
Three title classes are rejected outright, and each one is a different failure:

  * ``JUNIOR`` — "New Grad", "Internship", "Software Engineer I". Wrong level
    down. 123 of 5,251 live rows.
  * ``LEADERSHIP`` — "Director of Engineering", "Principal Engineer",
    "Head of". Wrong level up, and usually a people-management job.
  * ``NON_ENGINEERING`` — "Product Designer", "Technical Program Manager",
    "Staff Data Scientist". Adjacent to engineering, not it.

``NON_ENGINEERING`` is the one that needs care, because a naive word list eats
good jobs. Matching a bare "product" rejects "Software Engineer, AI Product";
matching a bare "partner" rejects "Senior Staff Software Engineer - App and
Partner Ecosystem". Both are roles worth seeing. So every pattern here anchors
on a role *head* ("product designer", "program manager") and never on a lone
qualifier — a qualifier describes the team a role sits in, not the role.

On reading years of experience
------------------------------
`years_required` takes the **maximum** stated figure, not the minimum, and this
is the single least obvious decision in the module.

A JD states several year counts, and they are not alternatives — they are a
headline requirement plus its sub-clauses. Palantir's "Senior Software Engineer
- Observability" reads:

    5+ years of professional software development experience.
    2+ years of experience contributing to the system design ...
    1+ years of experience as a mentor, tech lead ...

Taking the minimum reads that job as needing one year and files a genuine
senior role under "junior". Taking the maximum reads it as 5 — which is what a
human reads off the page. The sub-clauses are always narrower slices of the
headline, so the largest figure is the requirement and the rest are detail.

A stated figure above `max_years` is a real signal and not a near-miss:
Databricks' Staff roles ask 12-15 years, Stripe's ask 10. Those are dropped on
purpose even though "Staff Engineer" is a wanted family — the title is right
and the bar is not.

An **unstated** figure passes, exactly as an unstated salary does. Most JDs
never name a number, and requiring one would discard most of the board to
enforce a rule the posting never stated.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Role families. Keys are the names `filters.yaml` switches on.
# --------------------------------------------------------------------------
ROLE_FAMILIES: Final[dict[str, re.Pattern[str]]] = {
    "software": re.compile(r"(?ix)\b(software\s*(engineer|developer|engineering)|sde|swe)\b"),
    "staff": re.compile(r"(?ix)\bstaff\s*engineer\b"),
    "backend": re.compile(
        r"(?ix)\b(back\s*-?\s*end|backend|server\s*-?\s*side|api)\s*"
        r"(engineer|developer|engineering)\b"
    ),
    "frontend": re.compile(
        r"(?ix)\b(front\s*-?\s*end|frontend|web|ui)\s*(engineer|developer)\b"
    ),
    "fullstack": re.compile(r"(?ix)\b(full\s*-?\s*stack|fullstack)\b"),
    "ai_ml": re.compile(
        r"(?ix)\b("
        r"(ai|ml)\s*(engineer|developer)"
        r"|(ai|ml)\s*/\s*(ml|ai)"
        r"|machine\s*learning\s*(engineer|developer)"
        r"|deep\s*learning\s*engineer"
        r"|(genai|llm)\s*engineer"
        r")\b"
    ),
    "platform": re.compile(
        r"(?ix)\b(platform|infrastructure|systems?|distributed\s*systems)\s*engineer\b"
    ),
    "application": re.compile(r"(?ix)\b(applications?)\s*(engineer|developer)\b"),
    # Startup shorthand for a full-stack engineer who owns a product surface
    # (Linear's "Senior / Staff Product Engineer"). Enabled by choice: it is a
    # naming convention for a wanted role, not a different job.
    "product_engineer": re.compile(r"(?ix)\bproduct\s*engineer\b"),
    # --- Off by default: real engineering, different specialization ---------
    "devops_sre": re.compile(
        r"(?ix)\b(devops|sre|site\s*reliability|cloud)\s*(engineer|engineering)\b"
    ),
    "data": re.compile(r"(?ix)\bdata\s*(engineer|engineering)\b"),
    "security": re.compile(r"(?ix)\b(security|appsec)\s*engineer\b"),
    "mobile": re.compile(
        r"(?ix)\b(mobile|ios|android|react\s*native)\s*(engineer|developer)\b"
    ),
    "qa": re.compile(r"(?ix)\b(qa|quality|test|testing|sdet)\s*(engineer|automation)\b"),
}

DEFAULT_FAMILIES: Final[tuple[str, ...]] = (
    "software",
    "staff",
    "backend",
    "frontend",
    "fullstack",
    "ai_ml",
    "platform",
    "application",
    "product_engineer",
)

# Wrong level, downward. `engineer i` / `engineer 1` needs the negative
# lookahead or it swallows "Engineer II" and "Engineer 10".
JUNIOR: Final[re.Pattern[str]] = re.compile(
    r"""(?ix)\b(
        junior | jr\.? | intern | interns | internship | trainee | apprentice\w*
      | entry\s*-?\s*level | new\s*grad(uate)? | fresher | campus | co-?op
      # Bare, because the words separate in the wild: "Graduate Software
      # Engineer Program" splits `graduate` from both `engineer` and `program`.
      # The word boundary keeps this off "postgraduate" and "undergraduate".
      | graduate
      | (engineer|developer|swe|sde)\s*(i|1)(?![a-z0-9])
      | level\s*(1|i)(?![a-z0-9])
      # Tier suffix ("Robotics Software Engineer - L1"). The leading \b of the
      # enclosing group keeps this off "SQL1" and "HTML1", where the preceding
      # character is a word character and no boundary exists. L3+ is deliberately
      # absent — at most large employers L3 is already a mid-level band.
      | l[12](?![a-z0-9])
    )\b"""
)

# Wrong level, upward — and mostly people-management rather than IC work.
LEADERSHIP: Final[re.Pattern[str]] = re.compile(
    r"""(?ix)\b(
        principal | distinguished | fellow | director | vp | vice\s*president
      | head\s*of | chief | cto | cio | svp | evp
    )\b"""
)

# Adjacent-to-engineering roles. Every entry anchors on a role HEAD: a bare
# qualifier ("product", "partner", "enterprise") names the team a role serves,
# not the role, and matching one rejects jobs worth seeing.
NON_ENGINEERING: Final[re.Pattern[str]] = re.compile(
    r"""(?ix)(
        \b(engineering|product|program|project|account|marketing|sales|community
          |people|talent|delivery|operations?)\s*manager\b
      | \bmanager\s*,\s*(engineering|software|product)
      | \btechnical\s*program\s*manager\b | \btpm\b
      | \baccount\s*(executive|director)\b
      | \b(recruiter|sourcer|strategist|specialist|counsel|paralegal)\b
      | \b(product|ux|ui|graphic|visual|brand)\s*designer\b | \bdesigner\b
      | \btechnical\s*writer\b | \bcontent\s*(writer|strategist)\b
      | \b(data|research|applied)\s*scientist\b
      | \bsolutions?\s*(architect|consultant|engineer)\b
      | \b(business|data|financial|operations|marketing)\s*analyst\b
      | \bdeployment\s*strategist\b
      # "Tech support" and "L3 Support" are the forms that actually appear;
      # spelling only "technical support" leaks both. The `l\d` arm catches
      # the support-tier naming ("Software Engineer - L3 Support").
      | \b(customer|technical|tech|it|l\d)\s*support\b
      | \bsupport\s*(engineer|analyst|specialist)\b
      | \b(accountant|controller|bookkeeper)\b
    )"""
)

# A years figure only counts when it is attached to an experience claim.
# The trailing window is bounded so it cannot leap across a sentence into an
# unrelated "experience" (e.g. "5 years ago ... we value experience").
_YEARS: Final[re.Pattern[str]] = re.compile(
    r"""(?ix)
    (\d{1,2}) \s* (?:\+|plus)? \s* (?:[-–—]|to)? \s* (\d{1,2})? \s* \+? \s*
    (?:years?|yrs?) \b [^.\n]{0,45}? (?:experience|exp\b)
    """
)

# Above this a figure is prose, not a requirement ("50 years in business").
_MAX_PLAUSIBLE_YEARS: Final[int] = 25
# Only the first slice of a JD is scanned. Requirements sit near the top;
# further down lie boilerplate, benefits and company history, all of which
# carry year counts that are not requirements.
_SCAN_CHARS: Final[int] = 20_000


class RoleRules(BaseModel):
    """The tunable half of this module — mirrors the `role:` block in YAML."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    families: list[str] = Field(default_factory=lambda: list(DEFAULT_FAMILIES))
    exclude_junior: bool = True
    exclude_leadership: bool = True
    exclude_non_engineering: bool = True
    # Inclusive bounds on a *stated* requirement. Unstated always passes.
    min_years: int = 2
    max_years: int = 8

    @property
    def patterns(self) -> list[tuple[str, re.Pattern[str]]]:
        return [(name, ROLE_FAMILIES[name]) for name in self.families if name in ROLE_FAMILIES]

    @property
    def unknown_families(self) -> list[str]:
        return [name for name in self.families if name not in ROLE_FAMILIES]


class RoleVerdict(BaseModel):
    """The role rule's decision for one posting, plus why."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    reasons: list[str] = Field(default_factory=list)
    family: str | None = None
    years: int | None = None


def years_required(text: str | None) -> int | None:
    """Largest stated years-of-experience requirement, or None if unstated.

    Maximum, not minimum: a JD's smaller figures are sub-clauses of its
    headline requirement, never alternatives to it. See the module docstring.
    """
    if not text:
        return None
    found = [
        int(match.group(1))
        for match in _YEARS.finditer(text[:_SCAN_CHARS])
        if match.group(1) and 0 <= int(match.group(1)) <= _MAX_PLAUSIBLE_YEARS
    ]
    return max(found) if found else None


def match_family(title: str, rules: RoleRules) -> str | None:
    """First enabled family whose pattern matches the title."""
    for name, pattern in rules.patterns:
        if pattern.search(title):
            return name
    return None


def classify(
    title: str,
    description_text: str | None = None,
    rules: RoleRules | None = None,
) -> RoleVerdict:
    """Decide whether one posting is the right role at the right level.

    Order matters. The three title rejections run *before* the family match so
    a failure names what is actually wrong: "Software Engineer, New Grad"
    reports a junior title rather than an unmatched family, which is the
    difference between a reason that tells you something and one that does not.
    """
    rules = rules or RoleRules()
    if not rules.enabled:
        return RoleVerdict(passed=True, reasons=["role_ok:rule disabled"])

    title = (title or "").strip()
    if not title:
        return RoleVerdict(passed=False, reasons=["role_blocked:posting has no title"])

    if rules.exclude_junior and JUNIOR.search(title):
        return RoleVerdict(
            passed=False, reasons=[f"role_junior:{title!r} is a junior-level title"]
        )

    if rules.exclude_leadership and LEADERSHIP.search(title):
        return RoleVerdict(
            passed=False, reasons=[f"role_too_senior:{title!r} is a leadership title"]
        )

    if rules.exclude_non_engineering and NON_ENGINEERING.search(title):
        return RoleVerdict(
            passed=False,
            reasons=[f"role_blocked:{title!r} is not a software-engineering role"],
        )

    family = match_family(title, rules)
    if family is None:
        return RoleVerdict(
            passed=False, reasons=[f"role_blocked:{title!r} matches no wanted role family"]
        )

    years = years_required(description_text)
    if years is None:
        # Same treatment as an unstated salary: most postings name no figure,
        # and holding them to one would discard the board.
        return RoleVerdict(
            passed=True,
            reasons=[f"role_ok:{family}, experience unstated"],
            family=family,
        )

    if years > rules.max_years:
        return RoleVerdict(
            passed=False,
            reasons=[
                f"role_over_experienced:wants {years}+ yrs,"
                f" above the {rules.max_years}-year cap"
            ],
            family=family,
            years=years,
        )
    if years < rules.min_years:
        return RoleVerdict(
            passed=False,
            reasons=[
                f"role_under_experienced:wants only {years} yrs,"
                f" below the {rules.min_years}-year floor"
            ],
            family=family,
            years=years,
        )

    return RoleVerdict(
        passed=True,
        reasons=[f"role_ok:{family}, wants {years} yrs"],
        family=family,
        years=years,
    )
