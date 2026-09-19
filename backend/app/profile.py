"""The candidate profile — `profile.yaml`, modelled and versioned.

This is the store CLAUDE.md's Phase 2C Stage 1 describes, landed early because
matching needs it. Phase 3 autofill consumes the same file; there is one
profile, not two.

On `profile_version`
--------------------
Every read computes a hash of the *normalized* content. Scores and generated
documents are keyed to it, so editing the résumé cannot leave yesterday's
numbers on screen looking current. The hash is taken over the model dump
rather than the raw file bytes, so reordering keys or reflowing a comment does
not invalidate 741 cached scores for no reason.

On failure behaviour
--------------------
`load_filters` degrades to built-in defaults when `filters.yaml` is broken,
because losing the filter config must not stop ingestion. This module does the
opposite and raises. An empty profile is not a safe default: it would score
every job as a zero-skill match, write 741 confidently-wrong rows, and look
like a working feature. Better to fail where the mistake is visible.
"""

from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger(__name__)


class ProfileError(RuntimeError):
    """`profile.yaml` is missing, malformed, or unusable for scoring."""


class Identity(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    country: str = "IN"
    timezone: str = "Asia/Kolkata"
    links: dict[str, str] = Field(default_factory=dict)


class WorkAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    citizenship: str = "IN"
    authorized_in: list[str] = Field(default_factory=lambda: ["IN"])
    needs_sponsorship: bool = True
    open_to_relocation: bool = False
    remote_only: bool = True


class Seniority(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_years: int = 0
    ai_years: int = 0
    current_title: str = ""
    target_titles: list[str] = Field(default_factory=list)


class Compensation(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_annual_inr: float = 0.0
    preferred_currency: str = "USD"


class Evidence(BaseModel):
    """One capability and the résumé fact that proves it — what the LLM reads.

    `depth` is the distinction the skill list cannot carry: "used an LLM API
    in a side project" and "built LLM infrastructure in production" both put
    `llm` in `skills`, and a JD asking for production experience must be able
    to tell them apart.
    """

    model_config = ConfigDict(frozen=True)

    area: str
    depth: Literal["production", "project"] = "production"
    proof: str


# Bump when `llm.py`'s fit prompt or schema changes meaning. It is hashed into
# `Profile.version`, so every fit verdict read under the old wording goes stale
# instead of sitting on screen next to verdicts read under the new one.
FIT_PROMPT_VERSION = "2"


class Profile(BaseModel):
    """`profile.yaml`, whole."""

    model_config = ConfigDict(frozen=True)

    identity: Identity = Field(default_factory=Identity)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    seniority: Seniority = Field(default_factory=Seniority)
    compensation: Compensation = Field(default_factory=Compensation)

    # Nested `{category: {skill: weight}}` in the file; matching wants it flat.
    # Kept nested on disk because a flat 80-entry list is unreadable to edit.
    skills: dict[str, dict[str, int]] = Field(default_factory=dict)
    gaps: dict[str, int] = Field(default_factory=dict)

    # The LLM's view of the résumé: depth-tagged proof, plus capabilities the
    # résumé does not show. `skills`/`gaps` above feed the regex ranker.
    evidence: list[Evidence] = Field(default_factory=list)
    unproven: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)

    experience: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    resume_files: dict[str, str] = Field(default_factory=dict)
    answers: dict[str, Any] = Field(default_factory=dict)

    @field_validator("skills")
    @classmethod
    def _lower_skills(cls, value: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
        return {
            str(cat).strip().lower(): {
                str(k).strip().lower(): int(v) for k, v in (skills or {}).items()
            }
            for cat, skills in value.items()
        }

    @field_validator("gaps")
    @classmethod
    def _lower_gaps(cls, value: dict[str, int]) -> dict[str, int]:
        return {str(k).strip().lower(): int(v) for k, v in value.items()}

    @property
    def flat_skills(self) -> dict[str, int]:
        """`{skill: weight}` across every category, highest weight wins.

        A term appearing in two categories (`sql` under both backend and data)
        keeps its strongest weight rather than whichever category sorted last.
        """
        out: dict[str, int] = {}
        for skills in self.skills.values():
            for name, weight in skills.items():
                out[name] = max(out.get(name, 0), weight)
        return out

    @property
    def version(self) -> str:
        """Stable 16-hex-char hash of the normalized profile.

        Only the fields matching actually reads are hashed. Editing a phone
        number or a Phase 3 free-text answer must not invalidate every cached
        score — those cannot change any job's fit.
        """
        payload = json.dumps(
            {
                "skills": {k: self.flat_skills[k] for k in sorted(self.flat_skills)},
                "gaps": {k: self.gaps[k] for k in sorted(self.gaps)},
                "seniority": self.seniority.model_dump(),
                "work_authorization": self.work_authorization.model_dump(),
                "compensation": self.compensation.model_dump(),
                "country": self.identity.country,
                "evidence": [e.model_dump() for e in self.evidence],
                "unproven": self.unproven,
                "domains": self.domains,
                "education": [
                    e.get("degree") for e in self.education if isinstance(e, dict)
                ],
                "fit_prompt": FIT_PROMPT_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_profile(path: Path | None = None) -> Profile:
    """Read `profile.yaml`. Raises `ProfileError` rather than degrading."""
    if path is None:
        from app.config import get_settings

        path = get_settings().profile_file

    if not path.exists():
        raise ProfileError(
            f"profile file not found: {path}. Matching cannot score without one."
        )

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim below
        raise ProfileError(f"could not parse {path}: {type(exc).__name__}: {exc}") from exc

    if not isinstance(data, dict):
        raise ProfileError(f"profile file must be a mapping: {path}")

    try:
        profile = Profile(**data)
    except Exception as exc:  # noqa: BLE001
        raise ProfileError(f"invalid profile in {path}: {exc}") from exc

    if not profile.flat_skills:
        raise ProfileError(
            f"{path} lists no skills — every job would score zero. Populate `skills:`."
        )

    log.info(
        "profile.loaded",
        extra={
            "path": str(path),
            "version": profile.version,
            "skills": len(profile.flat_skills),
            "gaps": len(profile.gaps),
        },
    )
    return profile


@lru_cache
def get_profile() -> Profile:
    return load_profile()
