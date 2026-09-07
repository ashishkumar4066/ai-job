"""The eligibility filter.

Runs after `normalize` and before a job is marked active. A job is retained as
eligible only if all four hold:

  1. Location — `location_eligibility` includes an allowed country (`IN`) or
     `worldwide`. This is about who the employer will *hire*, not where it
     sits: a London company that accepts India-based remote candidates passes,
     because the field is derived from the posting's accepted-candidate
     locations rather than its office address.
  2. Remote — the role must be remote (or onsite-or-remote).
  3. Pay — an *unstated* salary passes and is flagged, so the UI can surface
     it. A *stated* salary must annualize to at least `min_annual_salary_inr`.
  4. Role — the title must name a wanted engineering family and must not be
     junior or leadership; a *stated* years-of-experience requirement must fall
     inside `role.min_years .. role.max_years`, while an *unstated* one passes.
     Rules 1-3 read structured fields; this one reads English, so it lives in
     its own module (`app/roles.py`) with the reasoning behind each pattern.

Jobs that fail are still stored, flagged `eligibility_pass = false` with the
reasons that decided it. Nothing is silently dropped: the rules stay auditable
against real data and tunable from `config/filters.yaml` without a code change.

Reasons are emitted for passes as well as failures. Knowing a job passed
*because* it stated no salary, rather than because it cleared the floor, is
exactly the signal needed to tell whether the rules are too tight or too loose.

On reading a salary
-------------------
`parse_salary_text` recovers numbers and a currency but not reliably a period,
so a period has to be inferred from magnitude before anything is compared to an
annual floor. Real rows that force this: Search Atlas posts "$25 - $30" (an
hourly rate, ~INR 55L/yr) and one Wellfound employer posts "INR 30,000 -
40,000" (monthly). Comparing either as an annual figure drops a good job for a
reason that never appears anywhere a human would look — the precise failure
mode this whole filter exists to make visible.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.geo import WORLDWIDE
from app.roles import RoleRules, classify

log = logging.getLogger(__name__)

HOURS_PER_YEAR = 2080  # 40h x 52w, the usual contractor convention
MONTHS_PER_YEAR = 12


class FilterConfig(BaseModel):
    """`config/filters.yaml`. Defaults match the file shipped in the repo."""

    model_config = ConfigDict(frozen=True)

    allowed_locations: list[str] = Field(default_factory=lambda: ["IN", WORLDWIDE])
    require_remote: bool = True
    min_annual_salary_inr: float = 2_000_000
    fx_to_inr: dict[str, float] = Field(
        default_factory=lambda: {
            "INR": 1.0,
            "USD": 88.0,
            "EUR": 95.0,
            "GBP": 112.0,
            "CAD": 64.0,
            "AUD": 58.0,
            "SGD": 66.0,
            "CHF": 100.0,
            "ZAR": 4.8,
            "PLN": 22.0,
        }
    )
    # Rule 4. Its own model, because it owns a regex library rather than a
    # handful of scalars — see `app/roles.py`.
    role: RoleRules = Field(default_factory=RoleRules)
    salary_period_bands: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "default": {"hourly_below": 500, "monthly_below": 20_000},
            "INR": {"hourly_below": 5_000, "monthly_below": 100_000},
        }
    )

    @field_validator("allowed_locations")
    @classmethod
    def _normalize_locations(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            token = str(item).strip()
            if not token:
                continue
            normalized = WORLDWIDE if token.casefold() == WORLDWIDE else token.upper()
            if normalized not in out:
                out.append(normalized)
        return out

    @field_validator("fx_to_inr")
    @classmethod
    def _normalize_fx(cls, value: dict[str, float]) -> dict[str, float]:
        return {str(k).strip().upper(): float(v) for k, v in value.items() if float(v) > 0}

    @property
    def allowed_set(self) -> frozenset[str]:
        return frozenset(self.allowed_locations)


class EligibilityResult(BaseModel):
    """The filter's verdict for one posting, plus why."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    reasons: list[str] = Field(default_factory=list)


class FilterConfigError(RuntimeError):
    pass


def load_filters(path: Path | None = None) -> FilterConfig:
    """Read `config/filters.yaml`.

    A missing or malformed file degrades to the built-in defaults with a
    warning rather than aborting: losing the config must not stop ingestion,
    and the defaults are the same rules the file ships with.
    """
    if path is None:
        from app.config import get_settings

        path = get_settings().filters_file

    if not path.exists():
        log.warning("filters.missing_file", extra={"path": str(path)})
        return FilterConfig()

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise FilterConfigError(f"filters file must be a mapping: {path}")
        config = FilterConfig(**data)
        if config.role.unknown_families:
            # Not fatal: a typo must not quietly shrink the filter to a
            # narrower set of families than the file asks for.
            log.warning(
                "filters.unknown_role_families",
                extra={"path": str(path), "families": config.role.unknown_families},
            )
        return config
    except Exception as exc:
        log.warning(
            "filters.invalid_file",
            extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
        return FilterConfig()


@lru_cache
def get_filters() -> FilterConfig:
    return load_filters()


def _lakh(inr: float) -> str:
    return f"INR {inr / 100_000:.1f}L"


def infer_period(value: float, currency: str, filters: FilterConfig) -> str:
    """Guess whether a bare number is hourly, monthly or annual pay.

    The comparison happens in the **native** currency. Converting first would
    destroy the signal: a monthly INR 40,000 is about $454, which is squarely
    in US hourly-rate territory, but is obviously not hourly in rupees.
    """
    bands = filters.salary_period_bands
    band = {**bands.get("default", {}), **bands.get(currency.upper(), {})}
    if value < band.get("hourly_below", 500):
        return "hourly"
    if value < band.get("monthly_below", 20_000):
        return "monthly"
    return "annual"


def annualize(value: float, currency: str, filters: FilterConfig) -> tuple[float | None, str]:
    """Convert one salary figure to INR per year.

    Returns `(annual_inr, basis)`. `annual_inr` is None when the currency is
    not in the FX table — an unconvertible figure must not be treated as zero
    and silently dropped.
    """
    rate = filters.fx_to_inr.get(currency.upper())
    if rate is None:
        return None, "unconvertible"

    basis = infer_period(value, currency, filters)
    multiplier = {"hourly": HOURS_PER_YEAR, "monthly": MONTHS_PER_YEAR, "annual": 1}[basis]
    return value * multiplier * rate, basis


def _location_verdict(
    location_eligibility: list[str], filters: FilterConfig
) -> tuple[bool, list[str]]:
    if not location_eligibility:
        return False, ["location_unknown:no eligibility data on the posting"]

    allowed = filters.allowed_set
    matched = [code for code in location_eligibility if code in allowed]
    if matched:
        return True, [f"location_ok:{'+'.join(matched)}"]

    shown = ",".join(location_eligibility[:6])
    if len(location_eligibility) > 6:
        shown += f",+{len(location_eligibility) - 6} more"
    return False, [f"location_blocked:restricted to {shown}"]


def _remote_verdict(remote: bool | None, filters: FilterConfig) -> tuple[bool, list[str]]:
    if not filters.require_remote:
        return True, ["remote_ok:rule disabled"]
    if remote:
        return True, ["remote_ok"]
    return False, ["remote_blocked:not a remote role"]


def _pay_verdict(
    salary_min: float | None,
    salary_max: float | None,
    salary_currency: str | None,
    filters: FilterConfig,
) -> tuple[bool, list[str]]:
    """Judge stated pay against the annual floor; pass anything unstated.

    The top of a range is what gets tested. "INR 15L - 30L" is worth surfacing
    even though its floor is under the threshold — only a job whose *maximum*
    falls short is genuinely below the bar.
    """
    floor = filters.min_annual_salary_inr
    top = salary_max if salary_max is not None else salary_min
    currency = (salary_currency or "").strip().upper() or None

    if top is None or currency is None:
        # Not a failure: ~84% of postings state no pay, and dropping them would
        # discard most of the board. Flagged so the UI can say "not stated".
        return True, ["pay_unstated:no salary on the posting"]

    annual, basis = annualize(top, currency, filters)
    if annual is None:
        # Unknown currency: pass rather than guess. Being unable to price a job
        # is not evidence that it pays badly.
        return True, [f"pay_unknown_currency:{currency} has no FX rate configured"]

    read_as = "" if basis == "annual" else f", read as {basis}"
    if annual < floor:
        return False, [
            f"pay_blocked:{currency} {top:,.0f} = {_lakh(annual)}/yr,"
            f" below the {_lakh(floor)} floor{read_as}"
        ]
    return True, [f"pay_ok:{currency} {top:,.0f} = {_lakh(annual)}/yr{read_as}"]


def _role_verdict(
    title: str | None,
    description_text: str | None,
    filters: FilterConfig,
) -> tuple[bool, list[str]]:
    """Judge the role family and seniority. See `app/roles.py` for the rules.

    A posting with no title at all fails: every other rule reads a field a
    board supplies structurally, but a missing title means we cannot tell what
    the job even is, and passing it would put an unidentifiable row in front of
    the alerting path.
    """
    verdict = classify(title or "", description_text, filters.role)
    return verdict.passed, verdict.reasons


def evaluate(
    *,
    location_eligibility: list[str],
    remote: bool | None = None,
    salary_min: float | None = None,
    salary_max: float | None = None,
    salary_currency: str | None = None,
    title: str | None = None,
    description_text: str | None = None,
    filters: FilterConfig | None = None,
) -> EligibilityResult:
    """Apply all four rules. Every one must hold for a job to be eligible.

    `title` and `description_text` default to None so the older three-rule call
    shape still type-checks, but a caller that omits them gets the empty-title
    rejection — the role rule cannot pass a posting it was never shown.
    """
    filters = filters or get_filters()

    location_ok, location_reasons = _location_verdict(location_eligibility, filters)
    remote_ok, remote_reasons = _remote_verdict(remote, filters)
    pay_ok, pay_reasons = _pay_verdict(salary_min, salary_max, salary_currency, filters)
    role_ok, role_reasons = _role_verdict(title, description_text, filters)

    return EligibilityResult(
        passed=location_ok and remote_ok and pay_ok and role_ok,
        reasons=[*location_reasons, *remote_reasons, *pay_reasons, *role_reasons],
    )
