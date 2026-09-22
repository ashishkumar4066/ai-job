"""Check generated résumé text against the profile, before it reaches a PDF.

CLAUDE.md's Stage 3 constraint, verbatim: *the generator may only select,
re-order and re-word facts stated in the profile. It must never invent an
employer, a date, a title or a metric. A résumé that hallucinates experience is
worse than no résumé — it is a lie with my name on it, and if it reads well I
will not catch it at review time. This needs a fixture test, not just a prompt
instruction.*

So this module is that test's subject, and the prompt's backstop. It compares
every rewritten line against a corpus built from `profile.yaml` plus the base
résumé, and reports what it cannot find there.

Two severities, deliberately
----------------------------
**Numbers are blocking.** A metric is the thing an interviewer checks and the
thing a reader cannot smell. "80% mAP@0.5" becoming "92%" survives any amount
of proofreading because it reads exactly as well as the truth. A rewrite
carrying an unsupported number is discarded and the original bullet is kept.

**Unknown terms only warn.** Tailoring *is* mirroring the JD's vocabulary:
calling the same work "distributed systems" because that is the phrase the
posting uses is the feature, not a fault. Blocking those would reject most
legitimate rewrites. They surface in the UI as amber, next to the text, where
a human decides.

The corpus is deliberately generous — every string in the profile, plus the
whole base résumé. A false *alarm* costs a glance. A false *pass* ships a lie.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

log = logging.getLogger(__name__)

Severity = Literal["blocking", "warning"]


@dataclass(slots=True, frozen=True)
class FactIssue:
    """One claim in generated text with no support in the profile."""

    region_id: str
    kind: Literal["number", "term"]
    token: str
    severity: Severity
    text: str

    @property
    def message(self) -> str:
        if self.kind == "number":
            return f"metric {self.token!r} is not in your profile"
        if self.severity == "blocking":
            return f"{self.token!r} is listed under gaps in your profile — you have not shipped it"
        return f"term {self.token!r} is not in your profile"


# "1,000+", "80%", "2.5", "20s", "mAP@0.5" -> the numeric part of each.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# CLAIMS are capitalised or all-caps tokens: product names, stacks, employers.
# Allows the punctuation inside real names — C++, Node.js, CI/CD, BGE-M3.
_TERM = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[+#./-][A-Za-z0-9+#]+)*\b")
# The CORPUS indexes every word, not only capitalised ones. Indexing only
# proper nouns made the corpus asymmetric with the claim side, so any word the
# résumé writes lower-case and a rewrite happens to start a sentence with was
# reported as invented — "Schema-grounded" was, on the first run of this.
_WORD = re.compile(r"[a-z0-9][a-z0-9+#./-]*")

# Capitalised words that carry no claim. A sentence-initial "Built" is not a
# proper noun, and flagging it teaches the reader to ignore the warnings.
_STOPWORDS = frozenset(
    """
    a an and the of for to in on at by with from as is are was were be been being
    built build designed design architected led leads lead owned owns own shipped
    ship delivered deliver drove drive created create developed develop implemented
    implement improved improve reduced reduce increased increase trained train
    deployed deploy maintained maintain managed manage wrote write scaled scale
    this that these those it its their there then than when where which who whom
    i we they he she you my our your first second third new used using use over
    across within via per each all most more less other another same both any
    every no not only also such via while during after before between about
    against under above through into out up down off again further once here
    how what why whether if else so because but or nor yes
    """.split()
)


def _strings(value: Any) -> Iterable[str]:
    """Every string anywhere in a nested YAML/JSON structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


def _numbers(text: str) -> set[str]:
    """Normalized numeric tokens: "1,000" and "1000" are the same claim."""
    return {match.group().replace(",", "").rstrip(".") for match in _NUMBER.finditer(text)}


def _terms(text: str) -> set[str]:
    """The claim side: capitalised tokens only."""
    return {match.group().lower() for match in _TERM.finditer(text)}


def _words(text: str) -> set[str]:
    """The corpus side: every word."""
    return {match.group() for match in _WORD.finditer(text.lower())}


# Sections of `profile.yaml` that list what the candidate CANNOT claim.
# Feeding these into the allowlist is backwards: `gaps:` exists precisely to
# name the stacks a résumé must never assert, and on the first run of this it
# was letting "Shipped it on Kubernetes" through, because "kubernetes" is a
# key in it. `unproven:` is deliberately NOT here — it is free prose ("people
# management (leads and mentors, no direct reports stated)") and harvesting
# its words would forbid "leads" and "mentors", which the résumé states
# truthfully.
_NEGATIVE_KEYS = ("gaps",)


@dataclass(slots=True)
class FactCorpus:
    """Everything the résumé is allowed to say, flattened for lookup."""

    numbers: frozenset[str]
    terms: frozenset[str]
    # Stacks named in `gaps:` — asserting one is a fabrication, not a rewording.
    forbidden: frozenset[str]
    text: str

    @classmethod
    def build(cls, *sources: Any) -> FactCorpus:
        """From any mix of profile dicts, models and raw text."""
        chunks: list[str] = []
        negative: list[str] = []
        for source in sources:
            if source is None:
                continue
            if hasattr(source, "model_dump"):
                source = source.model_dump()
            elif isinstance(source, Path):
                source = source.read_text(encoding="utf-8") if source.is_file() else ""
            if isinstance(source, dict):
                source = dict(source)
                for key in _NEGATIVE_KEYS:
                    negative.extend(_strings(source.pop(key, None)))
            chunks.extend(_strings(source))

        joined = "\n".join(chunks)
        forbidden = {
            word for word in _words("\n".join(negative)) if word not in _STOPWORDS and len(word) > 1
        }
        return cls(
            numbers=frozenset(_numbers(joined)),
            terms=frozenset(_words(joined)),
            forbidden=frozenset(forbidden),
            text=joined,
        )

    def check(self, region_id: str, text: str) -> list[FactIssue]:
        """Every claim in `text` the corpus does not support."""
        issues: list[FactIssue] = []
        for number in sorted(_numbers(text)):
            # Small integers are ordinary prose ("2 systems", "3 tenants") and
            # appear everywhere; they are not the metric risk this guards.
            if number in self.numbers or (number.isdigit() and len(number) == 1):
                continue
            issues.append(
                FactIssue(
                    region_id=region_id,
                    kind="number",
                    token=number,
                    severity="blocking",
                    text=text,
                )
            )
        for term in sorted(_terms(text)):
            if term in self.forbidden:
                # Checked BEFORE the allowlist on purpose: the résumé's own
                # evidence says "No AWS, Azure or Kubernetes in production", so
                # these words are present in the corpus as denials. Reading
                # that as permission to claim them is the exact inversion this
                # ordering prevents.
                issues.append(
                    FactIssue(
                        region_id=region_id,
                        kind="term",
                        token=term,
                        severity="blocking",
                        text=text,
                    )
                )
                continue
            if term in self.terms or term in _STOPWORDS or len(term) < 2:
                continue
            issues.append(
                FactIssue(
                    region_id=region_id, kind="term", token=term, severity="warning", text=text
                )
            )
        return issues


def screen_edits(
    corpus: FactCorpus, candidates: dict[str, str]
) -> tuple[dict[str, str], list[FactIssue]]:
    """Split rewritten text into what may ship and what may not.

    Returns the accepted rewrites (blocking issues removed) and every issue
    found, warnings included — the caller stores all of them, because "this
    bullet was rejected" is exactly what the user needs to see.
    """
    accepted: dict[str, str] = {}
    issues: list[FactIssue] = []
    for region_id, text in candidates.items():
        found = corpus.check(region_id, text)
        issues.extend(found)
        if any(issue.severity == "blocking" for issue in found):
            log.warning(
                "factcheck.rejected",
                extra={
                    "region": region_id,
                    "tokens": [i.token for i in found if i.severity == "blocking"],
                },
            )
            continue
        accepted[region_id] = text
    return accepted, issues
