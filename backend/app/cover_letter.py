"""Stage 3 — draft a cover letter for one job description.

The résumé's sibling (`tailor.py`), with one structural difference: a résumé
is a template with slots, so the model only ever *re-words* lines that already
exist. A cover letter has no original to fall back to — it is prose written
from the profile. The guarantee therefore moves from "the slot keeps its
original text" to **"a sentence that fails the fact check is removed"**.

What the model is allowed to do
-------------------------------
Write 3-4 body paragraphs as plain text with `**bold**` markers. It never sees
or writes LaTeX: the header (name, contact links), the date, the addressee,
the greeting and the sign-off are all rendered here from `profile.yaml` and
the posting, so it cannot get the candidate's own phone number wrong.

The fact check, per sentence
----------------------------
The corpus is the profile plus the base résumé (`FactCorpus`), exactly as for
tailoring, with two additions that only a letter needs:

* **The posting's words are allowed as TERMS.** A letter says what it admires
  about the employer ("your work on payments infrastructure"), and those words
  come from the JD. Flagging them would bury the one warning that matters.
* **The posting's numbers are NOT.** "5+ years of Rust" is a number the JD
  states and the candidate does not; letting the JD vouch for it would launder
  a requirement into a claim.

`gaps:` stacks stay blocking — including when the sentence is about the
employer. Dropping a true sentence costs a re-read; keeping a false one ships
a lie with the candidate's name on it.

Cost
----
The JD, the profile brief and the base summary go up, ~350 words come back.
Measured live on Cerebras qwen against "AI Engineer - India" (Pulsora):
**7,499 tokens** — close to a résumé tailoring, because the input dominates.
See `llm_cover_completion_tokens`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.factcheck import FactCorpus, FactIssue, _words
from app.llm import LLMScreener, profile_brief
from app.profile import Profile
from app.resume_tex import (
    ResumeDocument,
    ResumeTemplateError,
    from_latex,
    latex_escape,
    to_latex,
)

log = logging.getLogger(__name__)

# Bump when the letter prompt or the template changes meaning, the same
# discipline `TAILOR_PROMPT_VERSION` follows.
COVER_PROMPT_VERSION: Final[str] = "2026-09-22.1"

# The body sits between these, so a hand-edited letter can still be
# fact-checked without re-reading the header (whose date is not in the profile).
BODY_START: Final[str] = "% --- BODY (paragraphs) ---"
BODY_END: Final[str] = "% --- END BODY ---"

_MARKER_RULE: Final[str] = (
    "Write plain text. Mark emphasis as **bold**, sparingly. Never write LaTeX, backslashes "
    "or braces."
)


class CoverLetterDraft(BaseModel):
    """One letter as the model wrote it. Mirrors `COVER_SCHEMA`."""

    model_config = ConfigDict(frozen=True)

    paragraphs: list[str] = Field(default_factory=list)
    jd_keywords: list[str] = Field(default_factory=list)
    tailoring_notes: list[str] = Field(default_factory=list)


COVER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraphs", "jd_keywords", "tailoring_notes"],
    "properties": {
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "string",
                "description": (
                    "One body paragraph, 50-110 words. First person. " + _MARKER_RULE
                ),
            },
            "maxItems": 5,
            "description": (
                "The letter's BODY only, 3 or 4 paragraphs (never more than 5) — no greeting, "
                "no sign-off, no name, no date, no address; those are added for you. "
                "Paragraph 1: the role, and the one piece of the candidate's evidence that "
                "best answers what this posting centres on. Middle paragraph(s): two or three "
                "concrete pieces of evidence mapped to the posting's requirements, with the "
                "profile's own numbers. Last paragraph: why this company specifically, from "
                "what the posting says about it, and a short close. Every claim about the "
                "candidate must already be stated in the CANDIDATE section or the RÉSUMÉ "
                "SUMMARY: never invent an employer, title, date, technology or metric, and "
                "never claim a stack listed under gaps. Do not state a years-of-experience "
                "figure the profile does not state. Plain, direct, no clichés ('I am writing "
                "to express my interest', 'passionate', 'perfect fit')."
            ),
        },
        "jd_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
            "description": (
                "Up to 8 terms from the posting the letter addresses, only ones the "
                "candidate's evidence supports."
            ),
        },
        "tailoring_notes": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
            "description": (
                "Up to 5 short notes, one clause each, on what the letter leads with and why. "
                "Name any requirement of the posting the profile could NOT support, so the "
                "candidate can decide how to address it."
            ),
        },
    },
}

_SYSTEM_PROMPT: Final[str] = (
    "You write one candidate's cover letter for one job posting. You may only use facts the "
    "candidate's profile and résumé summary already state. You must never invent an "
    "employer, a date, a title, a technology or a metric — the candidate has to defend every "
    "sentence in an interview, so under-selling is better than overstating. Follow each "
    "field's description exactly."
)


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


def _base_summary() -> str:
    """The résumé's own summary paragraph, if the template is on this machine."""
    try:
        summary = ResumeDocument.load().region("summary")
    except ResumeTemplateError:
        return ""
    return summary.text if summary else ""


def build_messages(
    *,
    title: str,
    company: str,
    description_text: str | None,
    profile_text: str,
    resume_summary: str,
    max_jd_chars: int,
) -> list[dict[str, str]]:
    jd = " ".join((description_text or "").split())[:max_jd_chars] or "(no description provided)"
    user = (
        f"POSTING\nTitle: {title}\nCompany: {company}\n\n{jd}\n\n"
        f"CANDIDATE\n{profile_text}\n\n"
        f"RÉSUMÉ SUMMARY\n{resume_summary or '(none)'}\n\n"
        "Write the body of the cover letter for this posting."
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------
# Fact check
# --------------------------------------------------------------------------

# Sentence ends: ., ! or ? followed by whitespace and a capital, digit, quote
# or bold marker. Deliberately not splitting on "e.g. " or "Node.js".
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(*])")
# A piece ending in one of these was not a sentence end ("e.g. SSO").
_ABBREVIATION = re.compile(r"(?:\b(?:e\.g|i\.e|etc|vs|incl|approx)\.|\b[A-Z]\.)$", re.IGNORECASE)


def split_sentences(paragraph: str) -> list[str]:
    sentences: list[str] = []
    for piece in _SENTENCE_END.split(" ".join(paragraph.split())):
        if not piece.strip():
            continue
        if sentences and _ABBREVIATION.search(sentences[-1]):
            sentences[-1] = f"{sentences[-1]} {piece}"
        else:
            sentences.append(piece)
    return sentences


def build_corpus(
    profile: Profile,
    *,
    company: str,
    title: str,
    description_text: str | None,
    resume_tex: str | None = None,
) -> FactCorpus:
    """What a letter for THIS posting may say. See the module docstring."""
    corpus = FactCorpus.build(profile, resume_tex, company, title)
    jd_words = _words(description_text or "")
    # The posting's vocabulary is allowed; its numbers are not.
    return FactCorpus(
        numbers=corpus.numbers,
        terms=corpus.terms | jd_words | _words(f"{company} {title}"),
        forbidden=corpus.forbidden,
        text=corpus.text,
    )


def screen_paragraphs(
    paragraphs: list[str], corpus: FactCorpus
) -> tuple[list[str], list[FactIssue]]:
    """Remove every sentence carrying a blocking claim; keep the rest.

    Returns the surviving paragraphs (empty ones dropped) and every issue,
    warnings included — "this sentence was removed" is what the user needs to
    see, next to the sentence.
    """
    kept: list[str] = []
    issues: list[FactIssue] = []
    for index, paragraph in enumerate(paragraphs):
        region_id = f"para.{index}"
        survivors: list[str] = []
        for sentence in split_sentences(paragraph):
            found = corpus.check(region_id, sentence)
            issues.extend(found)
            if any(issue.severity == "blocking" for issue in found):
                log.warning(
                    "cover_letter.sentence_removed",
                    extra={
                        "region": region_id,
                        "tokens": [i.token for i in found if i.severity == "blocking"],
                    },
                )
                continue
            survivors.append(sentence)
        if survivors:
            kept.append(" ".join(survivors))
    return kept, issues


# --------------------------------------------------------------------------
# LaTeX
# --------------------------------------------------------------------------


def _display_url(url: str) -> str:
    """`https://www.linkedin.com/in/x/` -> `linkedin.com/in/x`."""
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.")
    return f"{host}{parsed.path}".rstrip("/") or url


def _href(url: str, label: str) -> str:
    # `\href`'s URL argument is not escaped text: `%` and `#` need escaping,
    # braces and backslashes simply must not be there.
    safe = re.sub(r"[{}\\\s]", "", url).replace("%", r"\%").replace("#", r"\#")
    return rf"\href{{{safe}}}{{{latex_escape(label)}}}"


def _resume_link_labels() -> dict[str, str]:
    """How the résumé itself displays each link (`linkedin.com/in/ashish-kumar`).

    Reused so the pair reads as one set, and because the profile's full URL
    carries an id suffix that wraps the contact line onto two.
    """
    try:
        tex = ResumeDocument.load().tex
    except ResumeTemplateError:
        return {}
    return {
        url.rstrip("/"): label
        for url, label in re.findall(r"\\href\{([^{}]+)\}\{([^{}]+)\}", tex)
    }


def _contact_line(profile: Profile) -> str:
    who = profile.identity
    labels = _resume_link_labels()
    parts: list[str] = []
    if who.phone:
        parts.append(latex_escape(who.phone))
    if who.email:
        parts.append(_href(f"mailto:{who.email}", who.email))
    ordered = [k for k in ("linkedin", "github") if k in who.links]
    ordered += [k for k in who.links if k not in ("linkedin", "github")]
    for key in ordered:
        if url := who.links[key]:
            label = labels.get(url.rstrip("/")) or _display_url(url)
            parts.append(_href(url, label))
    return r" \textbar{} ".join(parts)


def format_date(day: date) -> str:
    return f"{day.day} {day.strftime('%B %Y')}"


def render_letter(
    paragraphs: list[str],
    *,
    profile: Profile,
    company: str,
    title: str,
    dated: str,
) -> str:
    """The whole letter as LaTeX. Every model-written string goes through `to_latex`."""
    name = latex_escape(profile.identity.full_name or "")
    company_tex = latex_escape(company.strip() or "Hiring Team")
    title_tex = latex_escape(title.strip())
    body = "\n\n".join(to_latex(p) for p in paragraphs if p.strip())
    location = latex_escape(profile.identity.location) if profile.identity.location else ""

    return (
        "\\documentclass[11pt]{article}\n"
        "\n"
        "\\usepackage[left=0.9in,right=0.9in,top=0.7in,bottom=0.7in]{geometry}\n"
        "\\usepackage[colorlinks=true,urlcolor=blue,linkcolor=blue]{hyperref}\n"
        "\\setlength{\\parindent}{0pt}\n"
        "\\setlength{\\parskip}{9pt}\n"
        "\\pagestyle{empty}\n"
        "\n"
        "\\begin{document}\n"
        "\n"
        "\\begin{center}\n"
        f"{{\\LARGE\\bfseries {name}}}\\\\[5pt]\n"
        f"{{\\small {_contact_line(profile)}}}\n"
        "\\end{center}\n"
        "\n"
        "\\vspace{6pt}\n"
        + (f"{location}\\\\\n" if location else "")
        + f"{latex_escape(dated)}\n"
        "\n"
        "Hiring Team\\\\\n"
        f"{company_tex}\n"
        "\n"
        + (f"\\textbf{{Re: {title_tex}}}\n\n" if title_tex else "")
        + f"Dear {company_tex} hiring team,\n"
        "\n"
        f"{BODY_START}\n"
        f"{body}\n"
        f"{BODY_END}\n"
        "\n"
        "Sincerely,\\\\\n"
        f"{name}\n"
        "\n"
        "\\end{document}\n"
    )


def body_paragraphs(tex: str) -> list[str] | None:
    """The body of a (possibly hand-edited) letter, back in plain text.

    None when the markers are gone — the caller says the check was skipped
    rather than fact-checking the header, whose date is never in the profile.
    """
    start, end = tex.find(BODY_START), tex.find(BODY_END)
    if start < 0 or end < start:
        return None
    body = tex[start + len(BODY_START) : end]
    return [from_latex(chunk) for chunk in re.split(r"\n\s*\n", body) if chunk.strip()]


def check_letter(tex: str, corpus: FactCorpus) -> list[str]:
    """Blocking findings over a hand-edited letter. Reported, never enforced."""
    paragraphs = body_paragraphs(tex)
    if paragraphs is None:
        return ["the body markers were removed, so the letter was not fact-checked"]
    messages: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        for sentence in split_sentences(paragraph):
            for issue in corpus.check(f"para.{index}", sentence):
                if issue.severity == "blocking":
                    messages.append(f"paragraph {index + 1}: {issue.message}")
    return messages


# --------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------


@dataclass(slots=True)
class DraftedLetter:
    tex: str
    paragraphs: list[str]
    draft: CoverLetterDraft
    dated: str
    issues: list[FactIssue] = field(default_factory=list)
    tokens: int = 0


def apply_draft(
    draft: CoverLetterDraft,
    corpus: FactCorpus,
    *,
    profile: Profile,
    company: str,
    title: str,
    dated: str,
) -> tuple[str, list[str], list[FactIssue]]:
    """Fact-check the model's paragraphs, then render what survives.

    Split out of `write_cover_letter` so the fixture test drives it with a
    canned completion and no network.
    """
    paragraphs, issues = screen_paragraphs(list(draft.paragraphs), corpus)
    tex = render_letter(paragraphs, profile=profile, company=company, title=title, dated=dated)
    return tex, paragraphs, issues


async def write_cover_letter(
    *,
    title: str,
    company: str,
    description_text: str | None,
    profile: Profile,
    screener: LLMScreener | None = None,
    settings: Settings | None = None,
    today: date | None = None,
) -> DraftedLetter:
    """Draft the letter for one posting. One LLM call."""
    settings = settings or get_settings()
    try:
        resume_tex: str | None = ResumeDocument.load().tex
    except ResumeTemplateError:
        resume_tex = None

    messages = build_messages(
        title=title,
        company=company,
        description_text=description_text,
        profile_text=profile_brief(profile),
        resume_summary=_base_summary(),
        max_jd_chars=settings.llm_max_jd_chars,
    )

    async def run(client: LLMScreener) -> tuple[CoverLetterDraft, int]:
        before = client.tokens_spent
        result = await client.complete(
            messages,
            schema=COVER_SCHEMA,
            name="cover_letter",
            parse=lambda content: CoverLetterDraft.model_validate(json.loads(content)),
            max_completion_tokens=settings.llm_cover_completion_tokens,
        )
        return result, client.tokens_spent - before

    if screener is not None:
        draft, tokens = await run(screener)
    else:
        async with LLMScreener(settings) as client:
            draft, tokens = await run(client)

    corpus = build_corpus(
        profile,
        company=company,
        title=title,
        description_text=description_text,
        resume_tex=resume_tex,
    )
    dated = format_date(today or date.today())
    tex, paragraphs, issues = apply_draft(
        draft, corpus, profile=profile, company=company, title=title, dated=dated
    )
    if not paragraphs:
        raise ValueError("every sentence of the drafted letter failed the fact check")

    log.info(
        "cover_letter.done",
        extra={
            "company": company,
            "tokens": tokens,
            "paragraphs": len(paragraphs),
            "removed": sum(1 for i in issues if i.severity == "blocking"),
        },
    )
    return DraftedLetter(
        tex=tex, paragraphs=paragraphs, draft=draft, dated=dated, issues=issues, tokens=tokens
    )
