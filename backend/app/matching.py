"""Deterministic fit scoring — how well a posting matches `profile.yaml`.

Built like `eligibility.py`: config-driven, and it emits reasons for strong
matches as well as weak ones, so a ranking you disagree with can be argued
with instead of guessed at.

The line this module will not cross
----------------------------------
**This scores and ranks. It never drops a job.** That is not caution, it is a
measured result. A keyword-overlap scorer built against the live board
hard-dropped 45% of rows, and among them was Bjak's "Full Stack Software
Engineer - AI Neobank App" — full-stack, AI product, 3+ years wanted, and
"candidates must be based in India. We are hiring for this market
specifically". Close to the best-fitting row on the board, scored zero,
because the JD names no technology at all: it says "modern web frameworks,
APIs, databases" and never writes "Python" or "React".

The same run's *top* matches were an equity-only role and an "*Urgent* Gen AI
Engineer / Immediate-30-days" consultancy post, because counting keywords
rewards keyword-stuffed postings. Both failures point the same way: overlap is
a decent ranking signal and a terrible filter. Phase 1's eligibility rules
already did the dropping — 6,237 rows to 917 — and they did it on facts a
board states outright, not on prose.

Confidence, and why it exists
-----------------------------
A score computed from a JD that names three technologies means something. The
same score from a JD that names none is an artefact of the writing style, not
the fit. So every verdict carries a `confidence`, and a low-confidence row is
routed to the LLM *regardless of where it ranks* — the cheap pass is required
to know when it does not know. On the live board that rescues 157 rows,
including "Applied AI Engineer - India" and "Founding AI / Backend Engineer
(LLM Systems)", both under-ranked purely for describing the work in prose.

Scoring shape
-------------
Five positive components, capped so no single one can carry a row, minus
penalties:

    skill 0-40   weighted overlap with `profile.skills`
    years 0-15   stated JD requirement vs `profile.seniority.total_years`
    family 0-20  title against `profile.seniority.target_titles`
    india 0-10   JD explicitly names India / IST
    fresh 0-10   recency of `posted_at`

    penalties    evergreen phrasing, spam markers, and stacks in `profile.gaps`

Weights live in `config/filters.yaml` under `matching:` so they can be tuned
against real rows without editing code — the same arrangement the eligibility
rules use.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field

from app.profile import Profile, get_profile
from app.roles import years_required

log = logging.getLogger(__name__)

# A JD naming fewer than this many distinct technologies gives an unreliable
# overlap score — see the module docstring. 3 is where the live board stops
# producing obvious false negatives.
LOW_CONFIDENCE_TECH_TERMS = 3

# Vocabulary used only to judge *how technical* a JD's writing is. Deliberately
# not the profile's skill list: the question is "did this posting name concrete
# tools at all", which must not depend on whether they happen to be mine.
_TECH_VOCAB = (
    "python java javascript typescript react angular vue node django flask fastapi spring "
    "rails golang rust kotlin swift scala php ruby aws azure gcp kubernetes docker postgres "
    "mysql mongodb redis kafka spark terraform graphql pytorch tensorflow llm sql .net c#"
).split()

_EVERGREEN = [
    r"always (?:looking|hiring|accepting)",
    r"general application",
    r"talent (?:pool|pipeline|community)",
    r"future (?:opening|opportunit)",
    r"speculative application",
    r"keep your (?:resume|cv) on file",
    r"pipeline (?:role|req)",
]
_SPAM = [
    r"equity[\s-]only",
    r"\bunpaid\b",
    r"\*urgent\*",
    r"immediate join",
    r"immediate[\s-]\d+",
    r"commission only",
]
_INDIA = [r"\bindia\b", r"\bindian\b", r"\bIST\b"]

_FULLSTACK = re.compile(r"full[\s-]?stack", re.I)
_AI_TITLE = re.compile(r"\b(?:ai|ml|machine learning|gen\s?ai|llm|applied ai|nlp)\b", re.I)
_BACKEND = re.compile(r"back[\s-]?end|software engineer|software developer|\bsde\b", re.I)
_FRONTEND = re.compile(r"front[\s-]?end|\breact\b|\bui engineer\b", re.I)
_ADJACENT = re.compile(r"\bdata\b|platform|infra|devops|\bsre\b|security|mobile|\bqa\b", re.I)


class MatchWeights(BaseModel):
    """The `matching:` block in `config/filters.yaml`."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True

    skill_cap: int = 40
    years_cap: int = 15
    family_cap: int = 20
    india_bonus: int = 10
    fresh_cap: int = 10

    evergreen_penalty: int = 15
    spam_penalty: int = 25
    gap_penalty_cap: int = 12

    # Rows at or above this are worth an LLM read. Low-confidence rows are read
    # regardless — see `MatchVerdict.needs_llm`.
    llm_threshold: int = 65


class MatchVerdict(BaseModel):
    """One job's fit against one profile, plus why."""

    model_config = ConfigDict(frozen=True)

    score: int
    reasons: list[str] = Field(default_factory=list)
    # False when the JD is too vague for the overlap score to mean anything.
    confident: bool = True
    matched_skills: list[str] = Field(default_factory=list)
    missing_stacks: list[str] = Field(default_factory=list)
    years_required: int | None = None

    subscores: dict[str, int] = Field(default_factory=dict)

    # Carried on the verdict rather than read from config at call time, so a
    # persisted verdict still explains itself after the threshold is retuned.
    llm_threshold: int = 65

    @property
    def needs_llm(self) -> bool:
        """Read this row with the LLM?

        Either it ranked well enough to be worth the tokens, or the
        deterministic pass admits its own score is unreliable. The second
        clause is the one that rescues prose-written JDs.
        """
        return self.score >= self.llm_threshold or not self.confident


@lru_cache
def get_weights() -> MatchWeights:
    """Read the `matching:` block, falling back to the defaults above.

    Degrades rather than raises, matching `load_filters`: a typo in the weights
    must not stop the dashboard from ranking. A missing *profile* does raise —
    see `app/profile.py` for why the two differ.
    """
    from app.config import get_settings

    import yaml

    path = get_settings().filters_file
    if not path.exists():
        return MatchWeights()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return MatchWeights(**(data.get("matching") or {}))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "matching.invalid_weights",
            extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
        return MatchWeights()


def _term_pattern(term: str) -> str:
    """Word-boundary pattern for a skill name.

    `re.escape` first so `c++`, `.net` and `ci/cd` cannot inject regex syntax.
    A leading `\\b` is useless before a non-word character like `.net`, so it
    is only applied where it would actually anchor anything.
    """
    escaped = re.escape(term)
    prefix = r"\b" if term[:1].isalnum() else ""
    suffix = r"\b" if term[-1:].isalnum() else ""
    return f"{prefix}{escaped}{suffix}"


def _find(text: str, terms: dict[str, int]) -> dict[str, int]:
    return {t: w for t, w in terms.items() if re.search(_term_pattern(t), text)}


def _count_tech_terms(text: str) -> int:
    return sum(1 for t in _TECH_VOCAB if re.search(_term_pattern(t), text))


def _family_score(title: str, profile: Profile, cap: int) -> tuple[int, str]:
    """How close the title is to what I actually want to be doing."""
    wanted = " ".join(profile.seniority.target_titles).lower()
    if _FULLSTACK.search(title):
        return cap, "fullstack"
    if _AI_TITLE.search(title):
        return cap, "ai_ml"
    if _BACKEND.search(title):
        return int(cap * 0.85), "backend"
    if _FRONTEND.search(title):
        return int(cap * 0.6), "frontend"
    if _ADJACENT.search(title):
        return int(cap * 0.3), "adjacent"
    # An unrecognised title is not evidence of a bad fit — `roles.py` already
    # rejected the families we do not want, so anything still here is plausible.
    if wanted and any(w and w in title for w in wanted.split()):
        return int(cap * 0.5), "title_overlap"
    return int(cap * 0.5), "unclassified"


def _years_score(stated: int | None, mine: int, cap: int) -> tuple[int, str]:
    """Compare a stated JD requirement to my experience.

    An unstated requirement scores just below the ideal rather than zero, for
    the same reason `eligibility.py` passes unstated salaries: most postings
    name no figure, and treating silence as a negative would sink the board.
    """
    if stated is None:
        return int(cap * 0.8), "years_unstated"
    if stated > mine:
        # Over the bar. `roles.py` already dropped anything past `max_years`,
        # so this is a modest stretch, not a disqualification.
        return int(cap * 0.35), f"years_above_me:{stated}>{mine}"
    if mine - stated <= 3:
        return cap, f"years_ideal:{stated}<={mine}"
    return int(cap * 0.65), f"years_overqualified:{stated}vs{mine}"


def evaluate(
    *,
    title: str | None,
    description_text: str | None,
    posted_at: datetime | None = None,
    now: datetime | None = None,
    profile: Profile | None = None,
    weights: MatchWeights | None = None,
) -> MatchVerdict:
    """Score one posting against the profile. Never returns a drop decision."""
    profile = profile or get_profile()
    weights = weights or get_weights()

    title_raw = (title or "").strip()
    haystack = " ".join(f"{title_raw}\n{description_text or ''}".lower().split())

    reasons: list[str] = []

    # --- skill overlap ------------------------------------------------------
    matched = _find(haystack, profile.flat_skills)
    raw_skill = sum(matched.values()) * 2
    skill = min(weights.skill_cap, raw_skill)
    if matched:
        top = sorted(matched, key=lambda k: (-matched[k], k))[:6]
        reasons.append(f"skills_matched:{len(matched)} ({', '.join(top)})")
    else:
        reasons.append("skills_matched:none named in this posting")

    # --- experience ---------------------------------------------------------
    stated = years_required(haystack)
    years, years_reason = _years_score(stated, profile.seniority.total_years, weights.years_cap)
    reasons.append(years_reason)

    # --- role family --------------------------------------------------------
    family, family_reason = _family_score(title_raw.lower(), profile, weights.family_cap)
    reasons.append(f"family:{family_reason}")

    # --- explicitly India-friendly -----------------------------------------
    india = 0
    if any(re.search(p, haystack, re.I) for p in _INDIA):
        india = weights.india_bonus
        reasons.append("india_named:posting mentions India/IST directly")

    # --- freshness ----------------------------------------------------------
    fresh = 0
    if posted_at is not None and now is not None:
        age = (now - posted_at).days
        if age <= 14:
            fresh, label = weights.fresh_cap, f"fresh:{age}d"
        elif age <= 30:
            fresh, label = int(weights.fresh_cap * 0.7), f"recent:{age}d"
        elif age <= 60:
            fresh, label = int(weights.fresh_cap * 0.4), f"aging:{age}d"
        else:
            fresh, label = int(weights.fresh_cap * 0.1), f"stale:{age}d"
        reasons.append(label)

    # --- penalties ----------------------------------------------------------
    penalty = 0
    if any(re.search(p, haystack) for p in _EVERGREEN):
        penalty += weights.evergreen_penalty
        reasons.append("evergreen_language:reads like a talent-pool posting")
    if any(re.search(p, haystack) for p in _SPAM):
        penalty += weights.spam_penalty
        reasons.append("spam_markers:equity-only / urgent / immediate-joiner")

    missing = _find(haystack, profile.gaps)
    if missing:
        gap_pen = min(weights.gap_penalty_cap, sum(missing.values()) * 2)
        penalty += gap_pen
        reasons.append(f"stack_gaps:{', '.join(sorted(missing))}")

    total = max(0, min(100, skill + years + family + india + fresh - penalty))

    # --- confidence ---------------------------------------------------------
    named = _count_tech_terms(haystack)
    confident = named >= LOW_CONFIDENCE_TECH_TERMS
    if not confident:
        reasons.append(
            f"low_confidence:JD names {named} technologies — score is unreliable; "
            "not sent to the LLM automatically, deep-read it from the drawer if worth it"
        )

    return MatchVerdict(
        score=total,
        reasons=reasons,
        confident=confident,
        matched_skills=sorted(matched),
        missing_stacks=sorted(missing),
        years_required=stated,
        llm_threshold=weights.llm_threshold,
        subscores={
            "skill": skill,
            "years": years,
            "family": family,
            "india": india,
            "fresh": fresh,
            "penalty": -penalty,
        },
    )
