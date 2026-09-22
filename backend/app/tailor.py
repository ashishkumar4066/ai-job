"""Stage 3 — tailor the résumé to one job description.

One LLM call per document, on request, never in bulk. CLAUDE.md is explicit
that this is the expensive stage and that most matches never get applied to, so
nothing here runs from a sweep or a ranking pass.

What the model is allowed to do
-------------------------------
Select, re-order and re-word. It is handed the résumé's bullets as **plain
text with `**bold**` markers** and returns decisions keyed by bullet id. It
never sees LaTeX, never writes LaTeX, and cannot add a bullet — `resume_tex.py`
only knows how to fill slots that already exist. The worst a bad completion can
do is reword one line badly.

Three guards, in order
----------------------
1. **The schema.** Ids are echoed back, not invented; an unknown id is dropped
   with a log line rather than raising.
2. **`factcheck.py`.** Any rewrite carrying a number absent from the profile is
   discarded and the original bullet kept. Unknown terms are kept and flagged
   amber. See that module for why the two differ.
3. **The compile.** A tailored document that will not build never reaches a
   download, because the PDF *is* the artifact.

Cost
----
Much bigger than a deep read: the JD, the profile brief and all 16 regions go
up, and up to 16 rewritten lines come back. Measured live on Cerebras qwen
against "AI Engineer - India" (a 5,981-char JD): **8,823 tokens**, versus
~1,800 for a screen. That is why it goes through `LLMScreener.complete` and its
rate limiter rather than its own client — the tokens bill the same daily cap.

It also needs a bigger completion ceiling than the screen: at the screen's
4,096 the answer truncated mid-JSON. See `llm_tailor_completion_tokens`.

On Groq's free 200K/day that is ~22 documents a day. It is a per-application
action taken a few times a week, so the cap has not been the binding limit —
but it is a real cost, which is why nothing generates one automatically.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.factcheck import FactCorpus, FactIssue, screen_edits
from app.llm import LLMScreener, profile_brief
from app.profile import Profile
from app.resume_tex import BulletEdit, Region, ResumeDocument, ResumeEdits

log = logging.getLogger(__name__)

# Bump when the tailoring prompt changes meaning, the same discipline
# `FIT_PROMPT_VERSION` follows in `profile.py`: a stored document must not keep
# looking current after the instructions that produced it changed.
TAILOR_PROMPT_VERSION: Final[str] = "2026-09-20.1"


class BulletDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    include: bool = True
    rewritten: str = ""


class ResumeTailoring(BaseModel):
    """One tailoring. Mirrors `TAILOR_SCHEMA`."""

    model_config = ConfigDict(frozen=True)

    bullets: list[BulletDecision] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    skills: list[BulletDecision] = Field(default_factory=list)
    summary: str = ""
    jd_keywords: list[str] = Field(default_factory=list)
    tailoring_notes: list[str] = Field(default_factory=list)


_MARKER_RULE: Final[str] = (
    "Write plain text. Mark emphasis as **bold** exactly as the input does. Never write "
    "LaTeX, backslashes or braces."
)

TAILOR_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    # Generation order is the reasoning order: decide each bullet, then rank
    # them, then the skills rows, and only then write the summary — so the
    # summary describes a résumé that has already been assembled rather than
    # setting an angle the bullets are then bent to fit.
    "required": ["bullets", "order", "skills", "summary", "jd_keywords", "tailoring_notes"],
    "properties": {
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "include", "rewritten"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Copy an id from the BULLETS list exactly. Never invent one.",
                    },
                    "include": {
                        "type": "boolean",
                        "description": (
                            "false ONLY when the bullet is irrelevant to this posting AND the "
                            "résumé is stronger without it. Default true. Never drop more than "
                            "two bullets in total, and never drop the last bullet of a job."
                        ),
                    },
                    "rewritten": {
                        "type": "string",
                        "description": (
                            "The bullet re-worded to lead with what THIS posting asks for. "
                            "EMPTY STRING to keep the original unchanged — prefer that unless "
                            "re-wording genuinely helps. Keep every number, employer, product "
                            "and technology exactly as given: you may reorder and rephrase the "
                            "facts, never add one. Do not invent a metric. Max 45 words. "
                            + _MARKER_RULE
                        ),
                    },
                },
            },
            "maxItems": 20,
            "description": (
                "One entry per bullet id you are changing or dropping. Bullets you leave out "
                "are kept as they are."
            ),
        },
        "order": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 20,
            "description": (
                "Bullet ids, most relevant to this posting first. Reordering happens WITHIN "
                "each job only, so list ids from the same job together. Ids you leave out keep "
                "their existing order after the ones you name. Empty to keep the résumé's order."
            ),
        },
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "include", "rewritten"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Copy an id from the SKILLS list exactly.",
                    },
                    "include": {
                        "type": "boolean",
                        "description": "Always true. Skills rows are re-ordered, never removed.",
                    },
                    "rewritten": {
                        "type": "string",
                        "description": (
                            "The same comma-separated list, re-ordered so the technologies this "
                            "posting names come first. You may DROP items that are irrelevant "
                            "here, but you may NOT add any the row does not already contain. "
                            "EMPTY STRING to leave the row alone. " + _MARKER_RULE
                        ),
                    },
                },
            },
            "maxItems": 8,
            "description": "Up to 8 skills rows, only the ones worth re-ordering.",
        },
        "summary": {
            "type": "string",
            "description": (
                "The SUMMARY paragraph rewritten to open with the candidate's strongest "
                "evidence FOR THIS POSTING, 45-70 words, third-person-implied like the "
                "original. Every claim must already appear in the profile or the original "
                "summary. EMPTY STRING to keep the original. " + _MARKER_RULE
            ),
        },
        "jd_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
            "description": (
                "Up to 10 terms this posting asks for that the tailored résumé now leads with. "
                "Only terms the candidate's evidence actually supports."
            ),
        },
        "tailoring_notes": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
            "description": (
                "Up to 6 short notes on what you changed and why, one clause each "
                "('led with Text-to-SQL: the posting centres on SQL generation'). Name any "
                "requirement you could NOT address from the profile."
            ),
        },
    },
}

_SYSTEM_PROMPT: Final[str] = (
    "You tailor one candidate's résumé to one job posting. You may only select, re-order and "
    "re-word facts the candidate's profile already states. You must never invent an employer, "
    "a date, a title, a technology or a metric — a résumé that overstates is worse than one "
    "that under-sells, because the candidate has to defend every line of it in an interview. "
    "Prefer keeping a bullet unchanged over a rewrite that adds nothing. Follow each field's "
    "description exactly."
)


@dataclass(slots=True)
class TailoredResume:
    """The result of one tailoring pass."""

    tex: str
    edits: ResumeEdits
    tailoring: ResumeTailoring
    issues: list[FactIssue] = field(default_factory=list)
    tokens: int = 0

    @property
    def rejected(self) -> list[FactIssue]:
        return [issue for issue in self.issues if issue.severity == "blocking"]

    @property
    def warnings(self) -> list[FactIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]


def _regions_block(regions: list[Region]) -> str:
    """The editable résumé, as ids and plain text, grouped by where they sit."""
    lines: list[str] = []
    current = ""
    for region in regions:
        if region.kind == "summary":
            continue
        if region.label != current:
            current = region.label
            lines.append(f"\n[{current}]")
        lines.append(f"{region.id}: {region.text}")
    return "\n".join(lines).strip()


def build_messages(
    *,
    title: str,
    company: str,
    description_text: str | None,
    profile_text: str,
    document: ResumeDocument,
    max_jd_chars: int,
) -> list[dict[str, str]]:
    """The tailoring prompt: the posting, the candidate, the résumé's slots."""
    jd = " ".join((description_text or "").split())[:max_jd_chars] or "(no description provided)"
    summary = document.region("summary")
    bullets = [r for r in document.regions if r.kind == "bullet"]
    skills = [r for r in document.regions if r.kind == "skills_row"]

    user = (
        f"POSTING\nTitle: {title}\nCompany: {company}\n\n{jd}\n\n"
        f"CANDIDATE\n{profile_text}\n\n"
        f"CURRENT SUMMARY\n{summary.text if summary else '(none)'}\n\n"
        f"BULLETS (id: text)\n{_regions_block(bullets)}\n\n"
        f"SKILLS ROWS (id: text)\n{_regions_block(skills)}\n\n"
        "Tailor this résumé to the posting."
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


def apply_tailoring(
    tailoring: ResumeTailoring,
    document: ResumeDocument,
    corpus: FactCorpus,
) -> tuple[str, ResumeEdits, list[FactIssue]]:
    """Fact-check the model's rewrites, then render them into the template.

    Split out of `tailor_resume` so the fixture test can drive it with a canned
    completion and no network — which is what CLAUDE.md asks for: *this needs a
    fixture test, not just a prompt instruction*.
    """
    decisions = list(tailoring.bullets) + list(tailoring.skills)
    candidates = {d.id: d.rewritten for d in decisions if d.rewritten.strip()}
    if tailoring.summary.strip():
        candidates["summary"] = tailoring.summary

    accepted, issues = screen_edits(corpus, candidates)

    bullets = [
        BulletEdit(
            id=decision.id,
            # A skills row is never dropped; `resume_tex` enforces it too, but
            # saying so here keeps the reason next to the decision.
            include=decision.include or decision.id.startswith("skills."),
            text=accepted.get(decision.id),
        )
        for decision in decisions
    ]
    edits = ResumeEdits(
        summary=accepted.get("summary"),
        bullets=bullets,
        order=[i for i in tailoring.order if document.region(i) is not None],
    )
    return document.render(edits), edits, issues


async def tailor_resume(
    *,
    title: str,
    company: str,
    description_text: str | None,
    profile: Profile,
    document: ResumeDocument | None = None,
    screener: LLMScreener | None = None,
    settings: Settings | None = None,
) -> TailoredResume:
    """Tailor the base résumé to one posting. One LLM call.

    `screener` is injectable so a caller that is already inside an
    `LLMScreener` context reuses its client, its limiter and its usage ledger —
    the tokens spent here bill the same daily cap as the deep read.
    """
    settings = settings or get_settings()
    document = document or ResumeDocument.load()

    messages = build_messages(
        title=title,
        company=company,
        description_text=description_text,
        profile_text=profile_brief(profile),
        document=document,
        max_jd_chars=settings.llm_max_jd_chars,
    )

    async def run(client: LLMScreener) -> tuple[ResumeTailoring, int]:
        before = client.tokens_spent
        result = await client.complete(
            messages,
            schema=TAILOR_SCHEMA,
            name="resume_tailoring",
            parse=lambda content: ResumeTailoring.model_validate(json.loads(content)),
            max_completion_tokens=settings.llm_tailor_completion_tokens,
        )
        return result, client.tokens_spent - before

    if screener is not None:
        tailoring, tokens = await run(screener)
    else:
        async with LLMScreener(settings) as client:
            tailoring, tokens = await run(client)

    corpus = FactCorpus.build(profile, document.tex)
    tex, edits, issues = apply_tailoring(tailoring, document, corpus)

    log.info(
        "tailor.done",
        extra={
            "company": company,
            "tokens": tokens,
            "rewritten": len(edits.bullets),
            "rejected": sum(1 for i in issues if i.severity == "blocking"),
            "warnings": sum(1 for i in issues if i.severity == "warning"),
        },
    )
    return TailoredResume(
        tex=tex, edits=edits, tailoring=tailoring, issues=issues, tokens=tokens
    )
