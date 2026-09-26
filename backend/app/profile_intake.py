"""Stage 1 — bootstrap `profile.yaml` from an uploaded résumé, and save edits.

CLAUDE.md's Stage 1 asks for a profile "editable from the dashboard" while
`profile.yaml` on disk stays the source of truth. This module is both halves:
`draft_from_tex` reads an uploaded .tex and proposes a profile, and
`save_profile` writes one back atomically.

Why the draft is never saved directly
-------------------------------------
Three sections of the profile are not descriptions of the résumé, they are
*constraints on what may be written about it*:

* `gaps:` is the fact-checker's **denylist**. `factcheck.py` treats a claim
  touching a `gaps:` key as blocking, which is what stops a tailored résumé
  from saying "shipped it on Kubernetes". A gap the model fails to notice is a
  gap that silently stops being enforced, on every document generated
  afterwards.
* `evidence:` carries `depth` — production versus side-project. The résumé
  text alone rarely says which, and the fit prompt leans on the distinction.
* `unproven:` is prose the model cannot infer from what the résumé *contains*,
  because it is about what the résumé omits.

So `draft_from_tex` returns a draft and writes nothing. The dashboard shows it
as a form, the human fixes those three sections, and `save_profile` persists
what the human confirmed. The LLM removes the typing, not the judgement.

Why the schema uses arrays where the file uses maps
--------------------------------------------------
`skills:` is `{category: {skill: weight}}` and `gaps:` is `{skill: weight}` on
disk — free-form keys. Strict `json_schema` cannot describe those: every
object needs `additionalProperties: false` and a fixed `required` list, which
is exactly what an open-ended map is not. Cerebras rejects the looser forms
outright (see CLAUDE.md on `maxItems`). The model therefore answers with
arrays of `{category, name, weight}` and `_to_yaml_dict` folds them back into
the nested maps the file wants.

Cost
----
One call, ~5-7k tokens, on first setup only (and on an explicit re-parse).
Nothing here runs during a sweep, a scoring pass or a document generation.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.llm import LLMScreener
from app.profile import Profile, ProfileError, get_profile

log = logging.getLogger(__name__)

# Bump when the intake prompt changes meaning. Stored on the draft so a profile
# can be read back knowing which instructions proposed it.
INTAKE_PROMPT_VERSION: Final[str] = "2026-09-25.1"

# A résumé is one or two pages; this is slack, not a real bound.
MAX_TEX_CHARS: Final[int] = 40_000

DEFAULT_PROFILE_HEADER: Final[str] = """\
# profile.yaml — who I am. The source of truth for matching and for every
# generated document.
#
# Written by the dashboard's profile editor, and safe to hand-edit: the editor
# reads this file back, so a change made here is not second-class.
#
# Three sections carry rules rather than facts, and are worth checking by hand:
#   gaps:     the fact-checker's DENYLIST. A claim touching one of these keys
#             is blocked from any generated résumé or cover letter. Removing an
#             entry stops that protection.
#   evidence: `depth: production` vs `project` is the distinction the skill
#             list cannot carry, and the fit prompt reads it.
#   unproven: capabilities this résumé does NOT show. Prose, read by the LLM.
"""


# --------------------------------------------------------------------------
# LaTeX -> readable text
# --------------------------------------------------------------------------

_COMMENT = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
_HREF = re.compile(r"\\href\s*\{([^}]*)\}\s*\{([^}]*)\}")
_SECTION = re.compile(r"\\begin\{rSection\}\s*\{([^}]*)\}")
_SUBSECTION = re.compile(r"\\begin\{rSubsection\}((?:\s*\{[^}]*\}){2,4})")
_BRACED = re.compile(r"\{([^{}]*)\}")
# Text-bearing wrappers: keep the argument, drop the macro.
_KEEP_ARG = re.compile(
    r"\\(?:textbf|textit|emph|underline|texttt|textsc|mbox|text|hbox|bf|it)\s*\{([^{}]*)\}"
)
# Macros whose argument is layout, not content.
_DROP_WHOLE = re.compile(
    r"\\(?:documentclass|usepackage|input|include|vspace|hspace|setlength|hyphenation|"
    r"pagestyle|thispagestyle|geometry|renewcommand|newcommand|providecommand|def|"
    r"definecolor|hypersetup|raggedright|raggedbottom)"
    r"\s*(?:\[[^\]]*\])?\s*(?:\{[^{}]*\})*"
)
# Spacing parameters set as bare lengths: `\itemsep -3pt {}`. These have no
# braces, so stripping the macro alone leaves the dimension behind as text —
# which is how "-3pt" turned up mid-résumé on the first run.
_DROP_LENGTH = re.compile(
    r"\\(?:itemsep|parsep|topsep|partopsep|itemindent|labelsep|labelwidth|leftmargin|"
    r"rightmargin|baselineskip|parskip|parindent|tabcolsep|arraystretch|arrayrulewidth)"
    r"\s*=?\s*-?[\d.]*\s*(?:pt|em|ex|in|cm|mm|pc|bp|sp)?"
)
_ENV_NAME = re.compile(r"\\(?:begin|end)\s*\{([^}]*)\}")
_MACRO = re.compile(r"\\[a-zA-Z]+\s*\*?")
# Math-mode shorthands that carry meaning. Replaced before the generic macro
# strip, or `$\sim$90\%` degrades to "$$90%" instead of "~90%".
_MATH = {
    r"\sim": "~",
    r"\approx": "~",
    r"\times": "x",
    r"\to": "->",
    r"\rightarrow": "->",
    r"\ldots": "...",
    r"\dots": "...",
    r"\pm": "+/-",
    r"\geq": ">=",
    r"\leq": "<=",
}
# Stand-ins held while the bare forms of these characters — math delimiters and
# tabular column separators — are stripped, so a real "$120K" and a literal
# "AI & Machine Learning" both survive.
_DOLLAR = "\x00DOLLAR\x00"
_AMP = "\x00AMP\x00"
_ESCAPED = {
    r"\&": _AMP,
    r"\%": "%",
    r"\$": _DOLLAR,
    r"\#": "#",
    r"\_": "_",
    r"\{": "{",
    r"\}": "}",
    r"\textbackslash": "\\",
    "~": " ",
    r"\\": "\n",
    "--": "-",
    "``": '"',
    "''": '"',
}


def _skip_args(text: str, index: int) -> int:
    """Index just past the balanced `{...}` / `[...]` groups starting at `index`.

    `\\begin{tabular}{@{} >{\\bfseries}l @{\\hspace{6ex}} p{2in}}` has a column
    spec three braces deep, so a non-greedy `\\{[^}]*\\}` stops inside it and
    spills ">p1.5in @ p-1.8in @" into the text. That is what this avoids.
    """
    pairs = {"{": "}", "[": "]"}
    while index < len(text):
        while index < len(text) and text[index] in " \t\n":
            index += 1
        if index >= len(text) or text[index] not in pairs:
            return index
        closer, depth, index = pairs[text[index]], 1, index + 1
        while index < len(text) and depth:
            char = text[index]
            if char == "\\":  # An escaped brace is not a delimiter.
                index += 2
                continue
            if char in pairs:
                depth += 1
            elif char in {"}", "]"}:
                depth -= 1
            index += 1
    return index


def _strip_environments(text: str) -> str:
    """Drop every `\\begin{...}` / `\\end{...}` with its arguments, whole."""
    out: list[str] = []
    cursor = 0
    while (match := _ENV_NAME.search(text, cursor)) is not None:
        out.append(text[cursor : match.start()])
        # `itemize`/`enumerate` take no arguments; `tabular` and the résumé
        # class's own rSection/rSubsection do. Skipping balanced groups handles
        # both, since a following `{` that belongs to content is rare here.
        cursor = _skip_args(text, match.end()) if match.group(1) != "document" else match.end()
    out.append(text[cursor:])
    return "".join(out)


def tex_to_text(tex: str) -> str:
    """A .tex résumé as readable plain text, for the model to read.

    Deliberately lossy and deliberately *not* `resume_tex.ResumeDocument`:
    that parser addresses the 16 editable regions of one known template, and
    an uploaded résumé may not be that template at all. Intake has to cope
    with any reasonable `\\documentclass{resume}`-style file, so it strips
    markup rather than locating structure. Only the reading is approximate —
    nothing here is ever written back as LaTeX.
    """
    text = tex[:MAX_TEX_CHARS]
    text = _COMMENT.sub("", text)

    # Everything before \begin{document} is packages and lengths.
    body = text.split(r"\begin{document}", 1)
    text = body[1] if len(body) == 2 else text

    text = _HREF.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = _SECTION.sub(lambda m: f"\n\n## {m.group(1).strip()}\n", text)
    # rSubsection{title}{dates}{...} — keep the arguments as a header line.
    text = _SUBSECTION.sub(
        lambda m: "\n### " + " | ".join(a.strip() for a in _BRACED.findall(m.group(1)) if a.strip()),
        text,
    )
    text = _DROP_WHOLE.sub("", text)
    text = _DROP_LENGTH.sub("", text)
    text = re.sub(r"\\item\b", "\n- ", text)
    # Nested wrappers (\textbf{\emph{x}}) need more than one pass.
    for _ in range(3):
        text, n = _KEEP_ARG.subn(lambda m: m.group(1), text)
        if not n:
            break
    text = _strip_environments(text)
    for token, plain in _ESCAPED.items():
        text = text.replace(token, plain)
    for token, plain in _MATH.items():
        text = text.replace(token, plain)
    text = _MACRO.sub("", text)
    # Bare `$` and `&` are markup by now; the escaped forms are behind sentinels.
    # `&` becomes ": " because the résumé's skills table is `category & list`,
    # which reads as a labelled line rather than two orphaned fragments.
    text = text.replace("$", "")
    text = re.sub(r"\s*&\s*", ": ", text)
    text = text.replace(_DOLLAR, "$").replace(_AMP, "&")
    text = text.replace("{", "").replace("}", "")
    # Column separators left "@" and stray table punctuation behind.
    text = re.sub(r"(?m)^[\s@|:>]+$", "", text)
    # Collapse whitespace without losing the paragraph and bullet structure.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


# --------------------------------------------------------------------------
# The drafted profile
# --------------------------------------------------------------------------


class DraftLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""
    url: str = ""


class DraftSkill(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str = ""
    name: str = ""
    weight: int = 2


class DraftGap(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""
    weight: int = 2


class DraftEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    area: str = ""
    depth: str = "production"
    proof: str = ""


class DraftRole(BaseModel):
    model_config = ConfigDict(frozen=True)

    company: str = ""
    title: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)


class DraftProject(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""
    description: str = ""


class DraftEducation(BaseModel):
    model_config = ConfigDict(frozen=True)

    degree: str = ""
    institution: str = ""
    year: str = ""


class ProfileDraft(BaseModel):
    """What the model read out of the résumé. Mirrors `INTAKE_SCHEMA`."""

    model_config = ConfigDict(frozen=True)

    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: list[DraftLink] = Field(default_factory=list)
    summary: str = ""
    total_years: int = 0
    ai_years: int = 0
    current_title: str = ""
    target_titles: list[str] = Field(default_factory=list)
    skills: list[DraftSkill] = Field(default_factory=list)
    gaps: list[DraftGap] = Field(default_factory=list)
    evidence: list[DraftEvidence] = Field(default_factory=list)
    unproven: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    experience: list[DraftRole] = Field(default_factory=list)
    projects: list[DraftProject] = Field(default_factory=list)
    education: list[DraftEducation] = Field(default_factory=list)


_WEIGHT_RULE: Final[str] = (
    "Weight 3 = built with it in production, repeatedly. 2 = used it in production once, or "
    "heavily in a side project. 1 = touched it."
)

INTAKE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "full_name",
        "email",
        "phone",
        "location",
        "links",
        "summary",
        "total_years",
        "ai_years",
        "current_title",
        "target_titles",
        "skills",
        "gaps",
        "evidence",
        "unproven",
        "domains",
        "experience",
        "projects",
        "education",
    ],
    "properties": {
        "full_name": {"type": "string", "description": "The candidate's full name, as written."},
        "email": {"type": "string", "description": "Email address, or empty string if absent."},
        "phone": {"type": "string", "description": "Phone number as written, or empty string."},
        "location": {
            "type": "string",
            "description": "Where the candidate lives, e.g. 'Bihar, India'. Empty if absent.",
        },
        "links": {
            "type": "array",
            "description": "Profile links found in the résumé. Up to 6.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "url"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Lowercase key: linkedin, github, portfolio, website.",
                    },
                    "url": {"type": "string", "description": "The full URL, including https://."},
                },
            },
        },
        "summary": {
            "type": "string",
            "description": (
                "The candidate's professional summary in 2-4 sentences, using ONLY facts the "
                "résumé states. Third person, no 'I'. If the résumé has its own summary, "
                "tighten that rather than writing a new one."
            ),
        },
        "total_years": {
            "type": "integer",
            "description": (
                "Total years of professional experience, computed from the employment dates. "
                "Round to the nearest whole year. 0 if the dates do not support a figure."
            ),
        },
        "ai_years": {
            "type": "integer",
            "description": (
                "Of those, the years spent on AI/ML work specifically. 0 if none. Never more "
                "than total_years."
            ),
        },
        "current_title": {
            "type": "string",
            "description": "The job title of the most recent role.",
        },
        "target_titles": {
            "type": "array",
            "description": (
                "Up to 6 job titles this candidate should be matched against, based on what "
                "they actually do. Concrete titles ('AI Engineer', 'Backend Engineer'), not "
                "seniority words alone."
            ),
            "items": {"type": "string"},
        },
        "skills": {
            "type": "array",
            "description": (
                "Every technology, tool and capability the résumé EVIDENCES, up to 80. Group "
                "them with `category`. Lowercase names. " + _WEIGHT_RULE
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "name", "weight"],
                "properties": {
                    "category": {
                        "type": "string",
                        "description": (
                            "One lowercase group name, reused across skills: ai, backend, "
                            "frontend, data, cloud, languages, tools."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "The skill, lowercase, as a JD would write it: 'fastapi'.",
                    },
                    "weight": {
                        "type": "integer",
                        "description": "1, 2 or 3. " + _WEIGHT_RULE,
                    },
                },
            },
        },
        "gaps": {
            "type": "array",
            "description": (
                "COMMON job-requirement technologies this résumé shows NO evidence of, up to "
                "30. This is a denylist: anything here is BLOCKED from appearing in a generated "
                "résumé, so it protects the candidate from overclaiming. Think about what "
                "engineering postings routinely ask for and this résumé does not demonstrate — "
                "major clouds, orchestration, infrastructure-as-code, other mainstream "
                "languages, mobile, big-data frameworks. Never list something the résumé does "
                "evidence. Lowercase."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "weight"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The missing technology, lowercase: 'kubernetes'.",
                    },
                    "weight": {
                        "type": "integer",
                        "description": (
                            "2 when a posting asking for it would be a real problem, 1 when it "
                            "is minor or adjacent to something the candidate does know."
                        ),
                    },
                },
            },
        },
        "evidence": {
            "type": "array",
            "description": (
                "The résumé's strongest capabilities, up to 10, each with the concrete "
                "achievement that PROVES it. This is what a reader is shown instead of a bare "
                "skill list, so keep the numbers the résumé states."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["area", "depth", "proof"],
                "properties": {
                    "area": {
                        "type": "string",
                        "description": "The capability, e.g. 'RAG and text-to-SQL'. A few words.",
                    },
                    "depth": {
                        "type": "string",
                        "enum": ["production", "project"],
                        "description": (
                            "'production' when the résumé places this in paid, shipped work. "
                            "'project' when it appears only under personal or academic "
                            "projects. When the résumé is ambiguous, answer 'project' — a "
                            "human corrects this, and overstating depth is the costlier error."
                        ),
                    },
                    "proof": {
                        "type": "string",
                        "description": (
                            "One or two sentences quoting the résumé's own achievement and its "
                            "metrics. Never add a number the résumé does not state."
                        ),
                    },
                },
            },
        },
        "unproven": {
            "type": "array",
            "description": (
                "Up to 8 short phrases naming capabilities this résumé does NOT demonstrate, "
                "written as prose for a human to read: 'no people management (mentors, no "
                "direct reports stated)', 'no high-traffic consumer scale'. About what is "
                "ABSENT, so do not repeat the skills list."
            ),
            "items": {"type": "string"},
        },
        "domains": {
            "type": "array",
            "description": (
                "Up to 5 industries or problem domains the candidate has worked in: "
                "'enterprise B2B SaaS', 'document intelligence'."
            ),
            "items": {"type": "string"},
        },
        "experience": {
            "type": "array",
            "description": "Every employer in the résumé, most recent first. Up to 10.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["company", "title", "dates", "bullets"],
                "properties": {
                    "company": {"type": "string", "description": "The employer's name."},
                    "title": {"type": "string", "description": "The role title held there."},
                    "dates": {
                        "type": "string",
                        "description": "The dates as the résumé writes them: 'Jan 2023 - Present'.",
                    },
                    "bullets": {
                        "type": "array",
                        "description": (
                            "That role's achievement bullets, VERBATIM from the résumé. Up to "
                            "10. Do not rewrite, shorten or improve them: this is the record "
                            "every generated document is fact-checked against."
                        ),
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "projects": {
            "type": "array",
            "description": "Personal or academic projects, up to 8. Empty if the résumé has none.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description"],
                "properties": {
                    "name": {"type": "string", "description": "The project's name."},
                    "description": {
                        "type": "string",
                        "description": "What it does and the stack, from the résumé's own words.",
                    },
                },
            },
        },
        "education": {
            "type": "array",
            "description": "Degrees, most recent first. Up to 5.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["degree", "institution", "year"],
                "properties": {
                    "degree": {
                        "type": "string",
                        "description": "e.g. 'B.Tech, Computer Science'.",
                    },
                    "institution": {"type": "string", "description": "The school's name."},
                    "year": {
                        "type": "string",
                        "description": "Graduation year or range, as written. Empty if absent.",
                    },
                },
            },
        },
    },
}

_SYSTEM_PROMPT: Final[str] = (
    "You read one candidate's résumé and extract a structured profile from it. "
    "Two rules govern everything you write. "
    "First, you may only record what the résumé states: never invent an employer, a date, a "
    "title, a technology or a metric, and never round a number up. This profile becomes the "
    "allowlist that later checks every generated document, so a fact you add here is a fact "
    "the candidate will be assumed to have proven. "
    "Second, `gaps` and `unproven` are about what the résumé does NOT show. They protect the "
    "candidate from overclaiming, so be thorough and honest there rather than flattering. "
    "Follow each field's description exactly."
)


def build_messages(resume_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"RÉSUMÉ\n{resume_text}"},
    ]


# --------------------------------------------------------------------------
# Draft -> the shape `profile.yaml` holds
# --------------------------------------------------------------------------

_KNOWN_DEPTHS: Final[frozenset[str]] = frozenset({"production", "project"})


def draft_to_profile_data(
    draft: ProfileDraft, *, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Fold a draft into the nested mapping `profile.yaml` stores.

    `existing` supplies the blocks intake cannot read off a résumé —
    `work_authorization`, `compensation`, `answers` and `resume_files` — so a
    re-parse of an updated résumé does not wipe the pay floor or the Phase 3
    answers. Absent, `Profile`'s own defaults apply.
    """
    base = dict(existing or {})

    skills: dict[str, dict[str, int]] = {}
    for skill in draft.skills:
        name = skill.name.strip().lower()
        category = skill.category.strip().lower() or "other"
        if not name:
            continue
        weight = min(3, max(1, skill.weight))
        bucket = skills.setdefault(category, {})
        bucket[name] = max(bucket.get(name, 0), weight)

    gaps: dict[str, int] = {}
    for gap in draft.gaps:
        name = gap.name.strip().lower()
        if not name:
            continue
        # A term cannot be both evidenced and a gap: `factcheck` would block a
        # claim the résumé actually supports. The skill list wins.
        if any(name in bucket for bucket in skills.values()):
            log.info("profile_intake.gap_is_also_a_skill", extra={"term": name})
            continue
        gaps[name] = min(2, max(1, gap.weight))

    identity: dict[str, Any] = dict(base.get("identity") or {})
    identity.update(
        {
            "full_name": draft.full_name.strip(),
            "email": draft.email.strip(),
            "phone": draft.phone.strip(),
            "location": draft.location.strip(),
        }
    )
    links = {
        link.name.strip().lower(): link.url.strip()
        for link in draft.links
        if link.name.strip() and link.url.strip()
    }
    if links:
        identity["links"] = links
    identity.setdefault("country", "IN")
    identity.setdefault("timezone", "Asia/Kolkata")

    total = max(0, draft.total_years)
    data: dict[str, Any] = {
        **base,
        "identity": identity,
        "summary": draft.summary.strip(),
        "seniority": {
            "total_years": total,
            "ai_years": min(total, max(0, draft.ai_years)),
            "current_title": draft.current_title.strip(),
            "target_titles": [t.strip() for t in draft.target_titles if t.strip()],
        },
        "skills": skills,
        "gaps": gaps,
        "evidence": [
            {
                "area": e.area.strip(),
                "depth": e.depth if e.depth in _KNOWN_DEPTHS else "project",
                "proof": e.proof.strip(),
            }
            for e in draft.evidence
            if e.area.strip() and e.proof.strip()
        ],
        "unproven": [u.strip() for u in draft.unproven if u.strip()],
        "domains": [d.strip() for d in draft.domains if d.strip()],
        "experience": [
            {
                "company": r.company.strip(),
                "title": r.title.strip(),
                "dates": r.dates.strip(),
                "bullets": [b.strip() for b in r.bullets if b.strip()],
            }
            for r in draft.experience
            if r.company.strip() or r.title.strip()
        ],
        "projects": [
            {"name": p.name.strip(), "description": p.description.strip()}
            for p in draft.projects
            if p.name.strip()
        ],
        "education": [
            {
                "degree": e.degree.strip(),
                "institution": e.institution.strip(),
                "year": e.year.strip(),
            }
            for e in draft.education
            if e.degree.strip() or e.institution.strip()
        ],
    }
    return data


async def draft_from_tex(
    tex: str, *, settings: Settings | None = None, existing: dict[str, Any] | None = None
) -> tuple[dict[str, Any], int]:
    """Read an uploaded .tex and propose a profile. Writes nothing.

    Returns the proposed mapping and the tokens it cost.
    """
    settings = settings or get_settings()
    resume_text = tex_to_text(tex)
    if len(resume_text.split()) < 40:
        raise ProfileError(
            "that .tex yielded almost no readable text — is it a résumé, and does it have a "
            "\\begin{document} body?"
        )

    async with LLMScreener(settings) as client:
        draft: ProfileDraft = await client.complete(
            build_messages(resume_text),
            schema=INTAKE_SCHEMA,
            name="profile_intake",
            parse=lambda content: ProfileDraft.model_validate(json.loads(content)),
            max_completion_tokens=settings.llm_intake_completion_tokens,
        )
        tokens = client.tokens_spent

    data = draft_to_profile_data(draft, existing=existing)
    log.info(
        "profile_intake.drafted",
        extra={
            "tokens": tokens,
            "skills": sum(len(v) for v in data["skills"].values()),
            "gaps": len(data["gaps"]),
            "evidence": len(data["evidence"]),
        },
    )
    return data, tokens


# --------------------------------------------------------------------------
# Persisting
# --------------------------------------------------------------------------


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def store_resume_files(
    tex: str | None,
    *,
    resume_bytes: bytes | None = None,
    resume_name: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, str]:
    """Write the uploaded résumé into `data/`, returning `resume_files` entries.

    The .tex always lands on `resume_tex.UPLOADED_TEMPLATE`, which
    `resolve_template` looks at first — an upload has to take effect, and a
    user-supplied filename in a path we then load is not worth the risk. The
    PDF keeps a sanitized version of its own name so a downloads folder stays
    legible.

    Paths come back relative to `backend/`, matching what `profile.yaml`
    already documents for `resume_files`.
    """
    from app.resume_tex import BACKEND_DIR, UPLOADED_TEMPLATE

    data_dir = data_dir or (BACKEND_DIR / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}

    if tex is not None:
        target = data_dir / UPLOADED_TEMPLATE.name
        tmp = target.with_suffix(".tex.tmp")
        # `newline=""` disables the platform newline translation. An uploaded
        # .tex from a Windows machine already contains CRLF, and the default
        # would rewrite each of its `\n` again, turning every line ending into
        # `\r\r\n` — measured live: a 7,404-byte template stored as 7,557.
        tmp.write_text(tex, encoding="utf-8", newline="")
        tmp.replace(target)
        out["tex"] = _relative(target, BACKEND_DIR)

    if resume_bytes:
        stem = Path(resume_name or "resume.pdf").name
        safe = _SAFE_NAME.sub("_", stem).strip("._") or "resume.pdf"
        if not safe.lower().endswith((".pdf", ".doc", ".docx")):
            safe += ".pdf"
        target = data_dir / safe
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(resume_bytes)
        tmp.replace(target)
        out["base"] = _relative(target, BACKEND_DIR)

    if out:
        log.info("profile_intake.stored", extra=out)
    return out


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:  # Outside the backend tree (a test's tmp_path).
        return path.as_posix()


def validate_profile_data(data: dict[str, Any]) -> Profile:
    """Parse a candidate mapping into a `Profile`, or raise `ProfileError`.

    The same gate `load_profile` applies, so the editor cannot save a profile
    the loader would then refuse to read — which would leave the app unable to
    boot with no obvious cause.
    """
    if not isinstance(data, dict):
        raise ProfileError("profile must be a mapping")
    try:
        profile = Profile(**data)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the editor
        raise ProfileError(f"invalid profile: {exc}") from exc
    if not profile.flat_skills:
        raise ProfileError("no skills listed — every job would score zero. Add at least one.")
    if not profile.identity.full_name.strip():
        raise ProfileError("full name is required — generated documents are signed with it.")
    return profile


def save_profile(data: dict[str, Any], path: Path | None = None) -> Profile:
    """Validate and write `profile.yaml` atomically, then invalidate the cache.

    Mirrors `prefs.save_prefs`: a half-written profile would be worse than no
    write at all, since every score and document keys off it.
    """
    profile = validate_profile_data(data)
    if path is None:
        path = get_settings().profile_file

    body = yaml.safe_dump(
        _ordered(data), sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
    )
    stamp = f"# Last written by the dashboard: {datetime.now(UTC).isoformat(timespec='seconds')}\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(DEFAULT_PROFILE_HEADER + stamp + "\n" + body, encoding="utf-8")
    tmp.replace(path)

    get_profile.cache_clear()
    log.info(
        "profile.saved",
        extra={"path": str(path), "version": profile.version, "skills": len(profile.flat_skills)},
    )
    return profile


# The order `profile.yaml` reads best in — identity first, the rules that
# govern generation last. `yaml.safe_dump(sort_keys=False)` preserves it.
_KEY_ORDER: Final[tuple[str, ...]] = (
    "identity",
    "summary",
    "work_authorization",
    "seniority",
    "compensation",
    "skills",
    "gaps",
    "evidence",
    "unproven",
    "domains",
    "experience",
    "projects",
    "education",
    "resume_files",
    "answers",
)


def _ordered(data: dict[str, Any]) -> dict[str, Any]:
    known = {k: data[k] for k in _KEY_ORDER if k in data}
    return {**known, **{k: v for k, v in data.items() if k not in known}}


def read_profile_data(path: Path | None = None) -> dict[str, Any]:
    """The raw mapping on disk, for the editor to round-trip.

    Distinct from `load_profile`, which returns a validated `Profile` and drops
    keys the model does not declare. The editor must not silently delete a
    hand-added key it does not know about, so it edits the raw mapping.
    """
    if path is None:
        path = get_settings().profile_file
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise ProfileError(f"could not parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"profile file must be a mapping: {path}")
    return data
