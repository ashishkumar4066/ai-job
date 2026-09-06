"""The eligibility + currency filter.

Runs after `normalize` and before a job is marked active. A job is retained as
eligible only if BOTH hold:

  1. Location — `location_eligibility` includes an allowed country (`IN`) or
     `worldwide`.
  2. Pay — `salary_currency` is the required currency (`USD`), or the currency
     is unknown *and* the employer is known to be US-based.

Jobs that fail are still stored, flagged `eligibility_pass = false` with the
reasons that decided it. Nothing is silently dropped: the rules stay auditable
against real data and tunable from `config/filters.yaml` without a code change.

Reasons are emitted for passes as well as failures. Knowing a job passed
*because* it was worldwide rather than explicitly India-eligible is exactly the
signal needed to tell whether the rules are too tight or too loose.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.geo import WORLDWIDE

log = logging.getLogger(__name__)


class FilterConfig(BaseModel):
    """`config/filters.yaml`. Defaults match the file shipped in the repo."""

    model_config = ConfigDict(frozen=True)

    allowed_locations: list[str] = Field(default_factory=lambda: ["IN", WORLDWIDE])
    required_currency: str = "USD"
    allow_us_employer_when_currency_unknown: bool = True

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

    @field_validator("required_currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

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
        return FilterConfig(**data)
    except Exception as exc:
        log.warning(
            "filters.invalid_file",
            extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
        return FilterConfig()


@lru_cache
def get_filters() -> FilterConfig:
    return load_filters()


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


def _pay_verdict(
    salary_currency: str | None, is_us_employer: bool | None, filters: FilterConfig
) -> tuple[bool, list[str]]:
    required = filters.required_currency
    currency = (salary_currency or "").strip().upper() or None

    if currency == required:
        return True, [f"pay_ok:{required}"]

    if currency is None:
        if filters.allow_us_employer_when_currency_unknown and is_us_employer is True:
            return True, ["pay_ok:currency unknown but employer is US-based"]
        if is_us_employer is None:
            return False, ["pay_blocked:currency unknown and employer origin unknown"]
        return False, ["pay_blocked:currency unknown and employer is not US-based"]

    return False, [f"pay_blocked:pays in {currency}, not {required}"]


def evaluate(
    *,
    location_eligibility: list[str],
    salary_currency: str | None,
    is_us_employer: bool | None,
    filters: FilterConfig | None = None,
) -> EligibilityResult:
    """Apply both rules. Both must hold for a job to be eligible."""
    filters = filters or get_filters()

    location_ok, location_reasons = _location_verdict(location_eligibility, filters)
    pay_ok, pay_reasons = _pay_verdict(salary_currency, is_us_employer, filters)

    return EligibilityResult(
        passed=location_ok and pay_ok,
        reasons=[*location_reasons, *pay_reasons],
    )
