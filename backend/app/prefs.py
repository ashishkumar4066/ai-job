"""Match preferences — the gate the Matches surface runs behind.

What this is, and what it is not
--------------------------------
There are now three configurable layers, and keeping them straight is the
whole reason this module is separate:

  * `config/filters.yaml` — **eligibility**, at ingest time. Facts about
    whether a job is holdable at all: India-eligible, not junior, not
    leadership. Doctrine, rarely touched, and it gates Telegram alerts.
  * `profile.yaml`        — **who I am**. Skills, gaps, years, comp floor.
    Drives the fit *score*.
  * this module           — **what I want right now**. A cheap, re-runnable
    filter over already-eligible rows: remote-only, full-time, this pay
    floor, this experience band.

The distinction that matters: eligibility is about the world, preferences are
about my mood this week. Tuning "full-time only" should cost a ten-second
re-rank, not a re-ingest, and must not silently rewrite which jobs trigger an
alert. That is why preferences live here and gate the *scoring* pass rather
than editing `eligibility_pass`.

The Jobs tile ignores this file entirely — it shows the whole board. Nothing
here can hide a row from Jobs, only from Matches.

Unstated passes
---------------
Every rule here treats "the board never said" as a PASS, governed by
`include_unstated`. This is not leniency, it is the measured behaviour of the
data: Greenhouse publishes no employment type at all (0 of 2,224 rows) and
~84% of postings state no pay. A strict reading of silence drops most of the
board for facts nobody ever published. `eligibility.py` reached the same
conclusion independently for pay and years; this follows it deliberately.

Set `include_unstated: false` to invert that, which is occasionally what you
want — "only show me jobs that actually state a salary" is a legitimate
question, just not the default one.

Versioning
----------
`prefs_version` is a hash of the filtering-relevant fields, the same trick
`profile.version` uses. It is stored on nothing: preferences select rows, they
do not change any row's score, so a pref edit never invalidates a cached
verdict. That is the entire payoff of gating scoring rather than eligibility —
changing your mind about employment type costs zero LLM calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.job_filters import MAX_POSTING_AGE_DAYS, JobFilterSet

log = logging.getLogger(__name__)

# Vocabularies, shared with `normalize.py`'s output. A value outside these is
# a config error worth surfacing, not a silent no-op: a typo'd "fulltime" in
# `employment_types` would otherwise match nothing and empty the Matches list
# with no explanation.
WORKPLACE_TYPES = ("remote", "hybrid", "onsite")
EMPLOYMENT_TYPES = (
    "full_time",
    "part_time",
    "contract",
    "internship",
    "temporary",
    "volunteer",
)


class PrefsError(RuntimeError):
    """Raised rather than degrading to a silently-wrong filter."""


class WorkPrefs(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Empty list means "no constraint", which is different from listing all
    # three: an empty list also admits rows whose type is NULL, whereas
    # listing all three still depends on `include_unstated` for the NULLs.
    workplace_types: list[str] = Field(default_factory=lambda: ["remote"])
    employment_types: list[str] = Field(default_factory=lambda: ["full_time"])

    @field_validator("workplace_types")
    @classmethod
    def _check_workplace(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip().lower() for v in value if v and v.strip()]
        bad = [v for v in cleaned if v not in WORKPLACE_TYPES]
        if bad:
            raise ValueError(
                f"unknown workplace_types {bad}; valid: {list(WORKPLACE_TYPES)}"
            )
        return cleaned

    @field_validator("employment_types")
    @classmethod
    def _check_employment(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip().lower() for v in value if v and v.strip()]
        bad = [v for v in cleaned if v not in EMPLOYMENT_TYPES]
        if bad:
            raise ValueError(
                f"unknown employment_types {bad}; valid: {list(EMPLOYMENT_TYPES)}"
            )
        return cleaned


class ExperiencePrefs(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Bounds on a posting's STATED requirement, read by `roles.years_required`
    # (which takes the maximum figure a JD names — see `roles.py`).
    #
    # The floor is 5, and it is a floor on what the POSTING ASKS, not on what I
    # have. At 6-8 the band dropped 372 eligible jobs and every one of them
    # asked for less than 6 years (162 asked 5+); none was dropped for asking
    # more than 8. A 5+ floor keeps "5+ years" roles and drops the 2-4 year
    # ones, which are pitched below my level.
    min_years: int = Field(default=5, ge=0, le=40)
    max_years: int = Field(default=8, ge=0, le=40)

    @field_validator("max_years")
    @classmethod
    def _ordered(cls, value: int, info: Any) -> int:
        lo = (info.data or {}).get("min_years")
        if lo is not None and value < lo:
            raise ValueError(f"max_years ({value}) is below min_years ({lo})")
        return value


class CompensationPrefs(BaseModel):
    model_config = ConfigDict(frozen=True)

    # An INT, not a float, and that is load-bearing rather than pedantic.
    # A rupee floor with a fractional part is meaningless, and as a float the
    # value round-trips unstably: `3_000_000` set in Python and `3000000.0`
    # read back from YAML are equal but serialize differently, so the hash in
    # `Prefs.version` changed on every save. That made the dashboard report a
    # stale Matches list after a no-op save — the exact failure the version
    # exists to detect. `_canonical` below is the general guard; this is the
    # specific one.
    min_annual_inr: int = Field(default=3_000_000, ge=0)
    # When true, a posting that states no pay is EXCLUDED. Off by default:
    # ~84% of the board states nothing, so this empties the list.
    require_stated: bool = False


class ValidityPrefs(BaseModel):
    model_config = ConfigDict(frozen=True)

    # 0 disables the gate. Rows never scored for validity (NULL) always pass —
    # an unrun validity pass must not empty the Matches list.
    min_score: int = Field(default=0, ge=0, le=100)


class FreshnessPrefs(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Posted within this many days. Capped at `MAX_POSTING_AGE_DAYS` (30): a
    # posting older than a month is no use, so Matches cannot be widened past
    # it — the choice is only how much tighter than a month to go.
    max_age_days: int = Field(default=MAX_POSTING_AGE_DAYS, ge=1, le=MAX_POSTING_AGE_DAYS)


class ScopePrefs(BaseModel):
    model_config = ConfigDict(frozen=True)

    exclude_companies: list[str] = Field(default_factory=list)
    exclude_ats: list[str] = Field(default_factory=list)

    @field_validator("exclude_companies", "exclude_ats")
    @classmethod
    def _lower(cls, value: list[str]) -> list[str]:
        return [v.strip().lower() for v in value if v and v.strip()]


class TransferPrefs(BaseModel):
    """The Jobs filters sent to Matches with "Send to Matches".

    Stored as the FILTERS, not a list of job ids. A frozen id list goes stale
    the moment the next sweep lands: new postings that match the same filters
    would never reach Matches, and closed ones would linger. Re-applying the
    filters on every Run keeps the scope meaning what it meant when sent.
    """

    model_config = ConfigDict(frozen=True)

    filters: JobFilterSet = Field(default_factory=JobFilterSet)
    # Bookkeeping for the banner ("740 jobs sent 2h ago"); not in `version`.
    count_at_transfer: int = 0
    transferred_at: datetime | None = None


def _canonical(value: Any) -> Any:
    """Fold a value to a form that hashes by content, not by construction.

    The only real work is collapsing integral floats to ints. A YAML
    round-trip turns `3000000` into `3000000.0`; the two are `==` but
    `json.dumps` writes them differently, so a hash over the raw values
    changed every time preferences were saved and reloaded unchanged.

    Applied recursively so a nested section added later cannot quietly
    reintroduce the same bug.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


class Prefs(BaseModel):
    """`prefs.yaml` — what I want out of the board right now."""

    model_config = ConfigDict(frozen=True)

    work: WorkPrefs = Field(default_factory=WorkPrefs)
    experience: ExperiencePrefs = Field(default_factory=ExperiencePrefs)
    compensation: CompensationPrefs = Field(default_factory=CompensationPrefs)
    validity: ValidityPrefs = Field(default_factory=ValidityPrefs)
    freshness: FreshnessPrefs = Field(default_factory=FreshnessPrefs)
    scope: ScopePrefs = Field(default_factory=ScopePrefs)
    # None = nothing sent from Jobs yet, so Matches works on every eligible job.
    transfer: TransferPrefs | None = None

    # Silence is a pass. See the module docstring — this is the single switch
    # behind every "unstated" decision in `gate.py`.
    include_unstated: bool = True

    # Bookkeeping, deliberately excluded from `version`: saving the same
    # preferences twice must not invalidate anything.
    updated_at: datetime | None = None

    @property
    def version(self) -> str:
        """Stable 16-hex hash of the filtering-relevant fields only.

        Everything goes through `_canonical` first. Without it the hash is
        sensitive to how a value was *constructed* rather than what it is: a
        YAML round-trip turns an integral `3000000` into `3000000.0`, which
        json-serializes differently and produced a fresh version on every
        save of unchanged preferences.
        """
        payload = json.dumps(
            _canonical({
                "work": {
                    "workplace_types": sorted(self.work.workplace_types),
                    "employment_types": sorted(self.work.employment_types),
                },
                "experience": self.experience.model_dump(),
                "compensation": self.compensation.model_dump(),
                "validity": self.validity.model_dump(),
                "freshness": self.freshness.model_dump(),
                "transfer": (
                    self.transfer.filters.model_dump(mode="json") if self.transfer else None
                ),
                "scope": {
                    "exclude_companies": sorted(self.scope.exclude_companies),
                    "exclude_ats": sorted(self.scope.exclude_ats),
                },
                "include_unstated": self.include_unstated,
            }),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_yaml_dict(self) -> dict[str, Any]:
        """The shape written back to disk. Drops nothing and adds nothing."""
        data = self.model_dump(mode="json", exclude={"updated_at"})
        data["updated_at"] = (self.updated_at or datetime.now(UTC)).isoformat()
        return data


DEFAULT_PREFS_HEADER = """\
# Match preferences — what I want out of the board RIGHT NOW.
#
# Distinct from the other two config layers, and the distinction is the point:
#
#   config/filters.yaml -> ELIGIBILITY. Can I hold this job at all?
#                          India-eligible, not junior, not leadership.
#                          Doctrine. Gates Telegram alerts. Rarely edited.
#   profile.yaml        -> WHO I AM. Skills, gaps, years, comp floor.
#                          Drives the fit SCORE.
#   this file           -> WHAT I WANT. A cheap filter over already-eligible
#                          rows. Edit freely; it costs a ~10s re-rank and
#                          ZERO LLM calls, because preferences select rows
#                          rather than changing any row's score.
#
# The Jobs tile ignores this file completely — it always shows the whole
# board. Nothing here can hide a row from Jobs, only from Matches.
#
# Every rule treats "the board never said" as a PASS. That is measured, not
# lenient: Greenhouse publishes no employment type at all (0 of 2,224 rows)
# and ~84% of postings state no pay, so reading silence strictly drops most
# of the board for facts nobody ever published. Flip `include_unstated` to
# false to invert it.
#
# Edited from the dashboard, but hand-editing here stays first-class.
"""


def load_prefs(path: Path | None = None) -> Prefs:
    """Read `prefs.yaml`, or return defaults if it does not exist yet.

    Unlike `load_profile`, a missing file is NOT an error: preferences have
    sensible defaults (remote, full-time, 2-8 years) and an absent file just
    means "I have not expressed a view". A malformed file *is* an error,
    because silently falling back to defaults would show a filtered list that
    does not match what the file says.
    """
    if path is None:
        from app.config import get_settings

        path = get_settings().prefs_file

    if not path.exists():
        log.info("prefs.defaults", extra={"path": str(path)})
        return Prefs()

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim
        raise PrefsError(f"could not parse {path}: {type(exc).__name__}: {exc}") from exc

    if not isinstance(data, dict):
        raise PrefsError(f"preferences file must be a mapping: {path}")

    try:
        prefs = Prefs(**data)
    except Exception as exc:  # noqa: BLE001
        raise PrefsError(f"invalid preferences in {path}: {exc}") from exc

    log.info("prefs.loaded", extra={"path": str(path), "version": prefs.version})
    return prefs


def save_prefs(prefs: Prefs, path: Path | None = None) -> Prefs:
    """Write `prefs.yaml` and return what was stored, `updated_at` stamped.

    Writes via a temp file and an atomic replace: a half-written YAML file
    would make `load_prefs` raise on the next read, and the dashboard's own
    save is the likeliest moment for that to happen.
    """
    if path is None:
        from app.config import get_settings

        path = get_settings().prefs_file

    stamped = prefs.model_copy(update={"updated_at": datetime.now(UTC)})
    body = yaml.safe_dump(
        stamped.to_yaml_dict(), sort_keys=False, allow_unicode=True, default_flow_style=False
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(DEFAULT_PREFS_HEADER + "\n" + body, encoding="utf-8")
    tmp.replace(path)

    get_prefs.cache_clear()
    log.info("prefs.saved", extra={"path": str(path), "version": stamped.version})
    return stamped


@lru_cache
def get_prefs() -> Prefs:
    return load_prefs()
