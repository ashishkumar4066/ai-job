"""The deterministic validity layer — is this posting worth believing?

Runs over every stored row, costs nothing, and answers a different question
from every other scoring layer in this codebase:

  * `eligibility.py` asks "may I hold this job?"  — a hard binary gate.
  * `matching.py`    asks "how well do I fit it?" — a ranking.
  * this module      asks "is this posting real?" — a confidence discount.

The distinction matters because a ghost posting can be perfectly eligible and
score an excellent fit. Nothing here filters; it produces a 0-100 score plus
reasons, so a stale row sorts down instead of vanishing.

Why `updated_at` is not used
----------------------------
The obvious staleness signal is unavailable. Measured on the live board: five
of six sources never populate `updated_at` — ashby, himalayas, lever, remotive
and wellfound, 3,266 of 5,491 open rows. Only Greenhouse sets it. So staleness
keys off `posted_at` (100% coverage) plus our own `first_seen_at` /
`last_seen_at`, which we control and which exist for every row.

Why the duplicate check is a footnote, and which number is which
----------------------------------------------------------------
It ships because CLAUDE.md asks for it and it is nearly free, not because it
finds much — but "how much" depends entirely on which slice you measure, and
the two numbers look like a contradiction until you know that.

On the **open + eligible** slice (929 rows), which is what CLAUDE.md's
footnote measured: 2 same-company duplicates, 1 cross-company. Effectively
nothing, exactly as recorded there.

On the **whole board** (6,317 rows), which is what this module actually runs
over: 223 same-company, 3 cross-company. The gap is not a bug in either
measurement — it is almost entirely Databricks, on a now-disabled board,
posting one role across nine cities as nine rows with a byte-identical JD.

That is duplication worth flagging, so the key here is deliberately
(company, title, JD) and does **not** include location. Collapsing across
cities is the right call for this tool specifically: the candidate is
India-remote-only, so "the same job, but in Austin" is not a distinct
opportunity. A tool for a relocating candidate would have to key on location
too, and would then report ~2 instead of ~223.

Note that `content_hash` finds none of this — 0 exact collisions across open
rows — because it covers title, company and locations as well as the JD. And
Databricks' "Solutions Architect" x23 is the opposite trap: it looks like
duplication and is not, carrying 23 genuinely distinct JD hashes.

Scoring shape
-------------
Start at 100 and subtract. Penalties are capped individually so no single
signal can zero a row on its own, and the floor is 0. A penalty always emits
a reason naming the number, because a bare 62 is not auditable and the whole
point of this layer is that its mistakes can be argued with.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from app.models import JobPosting, utcnow

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"

# Phrases that mark a posting as a perpetual funnel rather than a live opening.
# Deliberately narrow: each one is a stock recruiting phrase that means the
# posting is not attached to a specific headcount. Broader patterns ("join
# us", "we are growing") match ordinary enthusiastic JDs.
_EVERGREEN_PATTERNS = re.compile(
    r"\b("
    r"always (?:looking|hiring|accepting)"
    r"|talent (?:pool|pipeline|community|network)"
    r"|future (?:openings|opportunities|roles)"
    r"|general application"
    r"|speculative application"
    r"|expression of interest"
    r"|no specific (?:role|opening|vacancy)"
    r"|keep your (?:cv|resume) on file"
    r"|we.{0,12}ll reach out when"
    r")\b",
    re.IGNORECASE,
)

# Contentless boilerplate — a "description" that describes nothing. A JD made
# only of these is a ghost regardless of its length.
_CONTENTLESS_PATTERNS = re.compile(
    r"\b("
    r"job description will be (?:shared|provided)"
    r"|details will be (?:shared|discussed)"
    r"|to be discussed"
    r"|contact us for (?:more )?details"
    r"|apply to know more"
    r")\b",
    re.IGNORECASE,
)


class ValidityConfig(BaseModel):
    """The `validity:` block of `config/filters.yaml`."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True

    # Age bands in days, read off `posted_at`. The penalty ramps rather than
    # cliff-edges, because a 45-day-old posting is a weaker signal than a
    # 20-day-old one but is not yet evidence of anything.
    fresh_days: int = 14
    aging_days: int = 45
    stale_days: int = 90
    age_penalty_cap: int = 30

    # A row still listed by its board is alive by definition — closure is by
    # disappearance. This penalty is for rows the sweep has NOT confirmed:
    # `last_seen_at` older than the last successful run for that board.
    missed_sweep_penalty: int = 35

    # A row that has sat open for this long without ever changing its JD is
    # suspicious even while the board keeps listing it.
    never_changed_days: int = 120
    never_changed_penalty: int = 10

    evergreen_penalty: int = 25
    contentless_penalty: int = 30
    duplicate_penalty: int = 15

    # Missing-field penalties. `apply_url` is structural — a posting we cannot
    # apply to is worthless no matter how good it looks.
    missing_description_penalty: int = 25
    thin_description_chars: int = 400
    thin_description_penalty: int = 10
    missing_apply_url_penalty: int = 40
    missing_posted_at_penalty: int = 8

    # Below this, the dashboard shows the row as suspect.
    suspect_below: int = 60


@lru_cache(maxsize=1)
def get_validity_config(path: Path | None = None) -> ValidityConfig:
    target = path or CONFIG_PATH
    if not target.exists():
        return ValidityConfig()
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("validity.config_unreadable", extra={"error": str(exc)})
        return ValidityConfig()
    return ValidityConfig.model_validate(raw.get("validity") or {})


@dataclass
class ValidityVerdict:
    """One posting's validity, with the reasoning that produced it."""

    score: int = 100
    reasons: list[str] = field(default_factory=list)

    @property
    def suspect(self) -> bool:
        return self.score < get_validity_config().suspect_below

    def penalize(self, amount: int, reason: str) -> None:
        """Subtract and record. A penalty with no reason is not allowed."""
        if amount <= 0:
            return
        self.score = max(0, self.score - amount)
        self.reasons.append(f"{reason}:-{amount}")

    def note(self, reason: str) -> None:
        """Record something that decided nothing — a pass, for the audit."""
        self.reasons.append(reason)


def jd_fingerprint(text: str | None) -> str:
    """A hash of the JD's words, insensitive to markup and whitespace.

    Not `content_hash`: that one covers title, locations and company too, so
    two different companies posting a byte-identical JD do not collide on it.
    This is deliberately JD-only, because "the same description under two
    employers" is exactly the duplicate shape worth finding.
    """
    if not text:
        return ""
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < 20:  # too short to fingerprint meaningfully
        return ""
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()


def find_duplicates(rows: Sequence[JobPosting]) -> dict[int, str]:
    """Map job id -> the duplicate reason, for rows that share a JD.

    Two keys, reported differently because they mean different things:

      * same (company, title, JD)  — a board listing one role twice.
      * same JD, different company — an agency reposting, or a JD copied
        between employers. The more interesting of the two, and the rarer.

    The first row of each group is left unflagged; only the extras are, so a
    genuine role is never penalized for having a twin.
    """
    by_company: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    by_jd: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for row in rows:
        fp = jd_fingerprint(row.description_text)
        if not fp:
            continue
        company = (row.company or "").strip().lower()
        title = (row.title or "").strip().lower()
        by_company[(company, title, fp)].append(row.id)
        by_jd[fp].append((row.id, company))

    flagged: dict[int, str] = {}
    for ids in by_company.values():
        for dup in sorted(ids)[1:]:
            flagged[dup] = "duplicate_same_company"

    for entries in by_jd.values():
        companies = {c for _, c in entries}
        if len(companies) > 1:
            for dup, _ in sorted(entries)[1:]:
                flagged.setdefault(dup, "duplicate_cross_company")

    return flagged


def evaluate(
    row: JobPosting,
    *,
    now: datetime | None = None,
    board_last_success: datetime | None = None,
    duplicate_of: str | None = None,
    config: ValidityConfig | None = None,
) -> ValidityVerdict:
    """Score one posting's believability.

    `board_last_success` is the `started_at` of the most recent completed,
    successful run that covered this row's board. A row whose `last_seen_at`
    predates it was not in that fetch and should have been closed — this is
    the ghost signal, and it is the reason the check needs run history rather
    than only the posting.

    It is deliberately the run's START, not its finish: rows are stamped as
    their own board is swept, and a full sweep takes minutes, so comparing
    against the finish flags every row fetched before the run ended. See
    `validation_runner.board_last_success`.
    """
    cfg = config or get_validity_config()
    v = ValidityVerdict()
    moment = now or utcnow()

    # --- 1. Presence in the latest successful fetch ------------------------
    if row.status != "open":
        v.note("closed")
    elif board_last_success is not None:
        # No slack needed — the bound is the run's start, so any row seen at
        # or after it was in that run. See the docstring.
        if row.last_seen_at < board_last_success:
            missed_days = max(0, (moment - row.last_seen_at).days)
            v.penalize(
                cfg.missed_sweep_penalty,
                f"missed_last_sweep:{missed_days}d",
            )
        else:
            v.note("seen_in_last_sweep")

    # --- 2. Age, off `posted_at` (never `updated_at`) ----------------------
    if row.posted_at is None:
        v.penalize(cfg.missing_posted_at_penalty, "no_posted_date")
    else:
        age = max(0, (moment - row.posted_at).days)
        if age <= cfg.fresh_days:
            v.note(f"fresh:{age}d")
        elif age <= cfg.aging_days:
            # Linear ramp across the aging band, a third of the cap at most.
            span = max(1, cfg.aging_days - cfg.fresh_days)
            share = (age - cfg.fresh_days) / span
            v.penalize(int(cfg.age_penalty_cap * 0.34 * share) or 1, f"aging:{age}d")
        elif age <= cfg.stale_days:
            span = max(1, cfg.stale_days - cfg.aging_days)
            share = (age - cfg.aging_days) / span
            v.penalize(
                int(cfg.age_penalty_cap * (0.34 + 0.33 * share)), f"stale:{age}d"
            )
        else:
            v.penalize(cfg.age_penalty_cap, f"very_stale:{age}d")

    # A row the board has listed unchanged for months. Distinct from age: the
    # posting may be recent and still never have moved.
    if row.status == "open" and row.first_seen_at is not None:
        tracked_days = max(0, (moment - row.first_seen_at).days)
        if tracked_days >= cfg.never_changed_days:
            v.penalize(cfg.never_changed_penalty, f"unchanged_since_first_seen:{tracked_days}d")

    # --- 3. Evergreen / contentless prose ----------------------------------
    text = row.description_text or ""
    if _EVERGREEN_PATTERNS.search(text) or _EVERGREEN_PATTERNS.search(row.title or ""):
        v.penalize(cfg.evergreen_penalty, "evergreen_language")
    if _CONTENTLESS_PATTERNS.search(text):
        v.penalize(cfg.contentless_penalty, "contentless_description")

    # --- 4. Missing critical fields ----------------------------------------
    if not text.strip():
        v.penalize(cfg.missing_description_penalty, "no_description")
    elif len(text) < cfg.thin_description_chars:
        v.penalize(cfg.thin_description_penalty, f"thin_description:{len(text)}c")
    if not (row.apply_url or "").strip():
        v.penalize(cfg.missing_apply_url_penalty, "no_apply_url")

    # --- 5. Duplicate (computed across the set, passed in) -----------------
    if duplicate_of:
        v.penalize(cfg.duplicate_penalty, duplicate_of)

    return v


def apply_llm_validity(v: ValidityVerdict, validity: dict | None) -> ValidityVerdict:
    """Fold a cached LLM validity read into a deterministic verdict.

    Only applied when a verdict exists — this layer must produce a usable
    score for every row with or without the deep read, since only ~440 of
    5,514 rows ever get one.

    The LLM's `posting_status` is trusted over the prose regexes above because
    it read the whole JD and they matched a phrase. Its penalties replace
    rather than stack with `evergreen_language`, so a row is not charged twice
    for the same observation.
    """
    if not validity:
        return v
    status = str(validity.get("posting_status") or "").lower()
    if status == "ghost":
        v.penalize(40, "llm:ghost")
    elif status == "evergreen":
        already = any(r.startswith("evergreen_language") for r in v.reasons)
        if not already:
            v.penalize(25, "llm:evergreen")
        else:
            v.note("llm:evergreen_confirmed")
    elif status == "active":
        v.note("llm:active")

    inconsistencies = validity.get("inconsistencies") or []
    if isinstance(inconsistencies, list) and inconsistencies:
        # Capped: a model that lists four contradictions has usually found one
        # real one and three restatements of it.
        v.penalize(min(15, 5 * len(inconsistencies)), f"llm:inconsistent:{len(inconsistencies)}")

    return v


def evaluate_all(
    rows: Sequence[JobPosting],
    *,
    now: datetime | None = None,
    board_last_success: dict[str, datetime] | None = None,
    config: ValidityConfig | None = None,
    use_llm: bool = True,
) -> dict[int, ValidityVerdict]:
    """Score a whole set, so the duplicate pass can see across rows.

    Returns job id -> verdict. The duplicate check is the reason this takes a
    collection rather than one row: "is this a duplicate" is not answerable
    from a single posting.
    """
    cfg = config or get_validity_config()
    duplicates = find_duplicates(rows)
    successes = board_last_success or {}
    out: dict[int, ValidityVerdict] = {}
    for row in rows:
        verdict = evaluate(
            row,
            now=now,
            board_last_success=successes.get(row.source_id),
            duplicate_of=duplicates.get(row.id),
            config=cfg,
        )
        if use_llm and row.llm_validity and row.llm_validity_hash == row.content_hash:
            verdict = apply_llm_validity(verdict, row.llm_validity)
        out[row.id] = verdict
    return out


def reasons_summary(verdicts: Iterable[ValidityVerdict]) -> dict[str, int]:
    """Histogram of reason kinds, for a run's log line."""
    counts: dict[str, int] = defaultdict(int)
    for v in verdicts:
        for reason in v.reasons:
            counts[reason.split(":")[0]] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
