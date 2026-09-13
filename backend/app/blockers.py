"""Hard blockers read out of JD prose — the free half of what the deep read did.

Why this exists
---------------
The deep read's most valuable output was `blocked`: a JD that says "must be
authorized to work in the US" or "remote within the EU" rules the candidate out
no matter how well the stack matches, and eligibility rule 1 cannot see it
because it reads the board's *structured* location fields. Paying ~1,800
tokens per job to learn that is how a pass reached 1.26M tokens.

Most of those sentences are formulaic, so they are matched here instead, and
a job carrying any blocker never reaches the LLM shortlist.

Measured against the 427 jobs the LLM had already read (2026-09-13)
-------------------------------------------------------------------
The LLM labels are not ground truth. It marked "Applied AI Engineer - India"
and "... Remote India" as `excludes_india`, and it answered
`sponsorship_required: yes` for GitLab, SingleStore and Pinterest postings
whose text names no sponsorship rule at all. So the patterns were tuned
against the JD sentences themselves, not the verdicts: every pattern below
was added for a real sentence on the board, quoted beside it.

A pattern must stay conservative. A false blocker silently hides a job from
the shortlist, while a missed blocker costs one LLM call. So:

  * Every region pattern is vetoed when the same sentence also names India,
    APAC, Asia or a worldwide term — "candidates in Europe or India" is open.
  * Pay sentences ("salaries outside the U.S. will vary") are never blockers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A sentence that names any of these is open to an India-based candidate, even
# if it also names a region that would otherwise read as exclusive.
_OPEN_TO_INDIA = re.compile(
    r"\b(india|indian|apac|asia|asia[-\s]pacific|worldwide|anywhere|global(ly)?|"
    r"any\s+country|all\s+countries)\b",
    re.IGNORECASE,
)

_REGION = (
    r"(the\s+)?(u\.?s\.?a?|united\s+states|us|canada|north\s+america|latam|"
    r"latin\s+america|the\s+americas|americas|europe|european\s+union|eu|emea|"
    r"uk|united\s+kingdom|european|u\.?s\.?)"
)


@dataclass(frozen=True)
class _Rule:
    key: str
    pattern: re.Pattern[str]
    # Region rules are vetoed by an India/worldwide mention in the same
    # sentence. Legal-status rules are not: "US citizens only" stays a blocker
    # even in a posting that also mentions a Bangalore office.
    region: bool = False


_RULES: tuple[_Rule, ...] = (
    # "must be legally authorized to work in the United States"
    _Rule(
        "us_work_authorization",
        re.compile(
            r"(authori[sz]ed|eligible|permitted|right)\s+to\s+work\s+in\s+the\s+"
            r"(u\.?s\.?a?\b|united\s+states)",
            re.IGNORECASE,
        ),
    ),
    # "No immigration or work visa sponsorship provided" / "***No visa
    # sponsorship***" / "We are unable to sponsor ... Visas" / "without
    # current or future sponsorship"
    _Rule(
        "no_visa_sponsorship",
        re.compile(
            r"\bno\s+(immigration\s+or\s+)?(work\s+)?visa\s+sponsorship"
            r"|\b(unable|not\s+able)\s+to\s+(offer\s+|provide\s+)?sponsor"
            r"|\b(cannot|can\s*not|will\s+not|won'?t|do(es)?\s+not)\s+"
            r"(offer\s+|provide\s+|support\s+)?(visa\s+|employment\s+|work\s+)?sponsor(ship)?\b"
            r"|\bwithout\s+(the\s+need\s+for\s+)?(current\s+or\s+future\s+)?"
            r"(visa\s+|employer\s+|employment\s+)?sponsorship"
            r"|\bsponsorship\s+(is\s+)?not\s+(available|offered|provided)",
            re.IGNORECASE,
        ),
    ),
    # "U.S. Person status is required" / "must be a US citizen"
    _Rule(
        "us_citizenship",
        re.compile(
            r"\bu\.?\s?s\.?\s+(citizen(ship)?|person(\s+status)?)\b"
            r"|\bgreen\s+card\s+holders?\b",
            re.IGNORECASE,
        ),
    ),
    # "active Secret / TS/SCI clearance"
    _Rule(
        "security_clearance",
        re.compile(
            r"\b(security|secret|top\s+secret)\s+clearance\b|\bts\s*/\s*sci\b",
            re.IGNORECASE,
        ),
    ),
    # "the position is fully remote in the EU" / "a remote role based in the
    # EMEA region"
    _Rule(
        "remote_region_only",
        re.compile(
            r"\bremote\s+(role\s+|position\s+)?(based\s+)?(with)?in\s+" + _REGION + r"\b",
            re.IGNORECASE,
        ),
        region=True,
    ),
    # "available to candidates located in European and U.S. time zones" /
    # "Open to candidates based in the United States, Canada, and LATAM" /
    # "You are based in Europe or the Americas" / "must reside in the US"
    _Rule(
        "candidate_region_only",
        re.compile(
            r"\b(candidates|applicants|you\s+are|you're|must\s+be|must|need\s+to\s+be)\s+"
            r"(currently\s+)?(located|based|residing|reside|living|live)\s+(with)?in\s+"
            + _REGION
            + r"\b",
            re.IGNORECASE,
        ),
        region=True,
    ),
)

# Sentence splitter: good enough for JD prose, where bullets are newlines. A
# full stop only ends a sentence after a lowercase word, because "U.S. Person
# status is required" split at "U.S." hid Vannevar's blocker in the first cut.
_SENTENCES = re.compile(r"(?<=[a-z]{2}[.!?])\s+|\n+|•")


def find_blockers(text: str | None) -> list[str]:
    """Blocker keys this JD states, each once, in rule order.

    Returns `key:evidence` strings, where evidence is the matched phrase, so a
    hidden job can always say *which sentence* hid it.
    """
    if not text:
        return []
    found: dict[str, str] = {}
    for sentence in _SENTENCES.split(text):
        if not sentence or len(sentence) < 8:
            continue
        for rule in _RULES:
            if rule.key in found:
                continue
            match = rule.pattern.search(sentence)
            if match is None:
                continue
            if rule.region and _OPEN_TO_INDIA.search(sentence):
                continue
            found[rule.key] = " ".join(match.group(0).split())[:60]
    return [f"{rule.key}:{found[rule.key]}" for rule in _RULES if rule.key in found]
