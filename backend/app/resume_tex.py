"""The résumé template, addressable in pieces.

CLAUDE.md Stage 3 sets a hard constraint: the generator may only select,
re-order and re-word facts already in the profile. That is a promise about
*content*. This module adds the structural half of it — the LLM never sees
LaTeX and never writes LaTeX.

How it works
------------
`ResumeDocument.parse` finds the editable regions of `Ashish_AI_FullStack_v2.tex`
and gives each a stable id:

    summary                  the SUMMARY paragraph
    exp.asint.0 .. .7        bullets under Senior Full Stack Developer
    exp.incture.0            bullets under Associate Software Developer
    proj.0                   PROJECTS bullets
    skills.ai-machine-learning, skills.back-end, ...   SKILLS table rows

`render()` then splices edits back in, replacing regions from the end of the
file backwards so earlier offsets stay valid. Anything not edited is reused
**byte for byte** — that is the safety property. A section the model said
nothing about cannot change, and a malformed edit can damage one bullet, never
the document.

Bold, without letting the model write LaTeX
-------------------------------------------
The résumé leans on `\\textbf{}` heavily enough that dropping it would visibly
change the document, so rewritten text is accepted in a tiny marker dialect:
`**like this**` becomes `\\textbf{like this}`. Everything else is escaped first,
so a stray `%`, `&` or backslash from the model turns into literal characters
rather than a syntax error or a comment that eats the rest of the line.

Ids are derived from the source, not stored
-------------------------------------------
`exp.asint.3` is company slug plus position in the original file. Re-parsing an
unedited template always yields the same ids, and a generated document keeps
working after the template is re-read. Editing the *base* .tex can shift them —
which is correct, because a stored tailoring is keyed to `profile_version` and
is already stale by then.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"
# Where the dashboard's upload lands, and so the first place looked.
UPLOADED_TEMPLATE = DATA_DIR / "resume.tex"
# The original hand-placed template. Still honoured, so an existing checkout
# keeps working without re-uploading anything.
LEGACY_TEMPLATE = DATA_DIR / "Ashish_AI_FullStack_v2.tex"
DEFAULT_TEMPLATE = LEGACY_TEMPLATE


def resolve_template(path: Path | None = None) -> Path:
    """Which .tex is the base résumé.

    An explicit path wins, then an upload, then the legacy file, then a lone
    `.tex` sitting in `data/` under any name — `data/` is gitignored as PII, so
    a fresh checkout has whatever the user dropped in, named whatever they
    named it. `resume.cls` is excluded: it is the class, not a résumé.

    Returns the upload path when nothing exists, so the error names the place
    to put one.
    """
    if path is not None:
        return path
    for candidate in (UPLOADED_TEMPLATE, LEGACY_TEMPLATE):
        if candidate.is_file():
            return candidate
    found = sorted(p for p in DATA_DIR.glob("*.tex") if p.stem != "resume.cls")
    if len(found) == 1:
        return found[0]
    return UPLOADED_TEMPLATE


class ResumeTemplateError(RuntimeError):
    """The template is missing or does not look like the résumé we expect."""


# --------------------------------------------------------------------------
# LaTeX text escaping
# --------------------------------------------------------------------------

# One pass, not a chain of `.replace()`. Replacing sequentially re-escapes the
# braces this table itself inserts: `\fragile{}` came out as
# `\textbackslash\{\}fragile\{\}` before this was a single substitution.
_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
}
_ESCAPE_RE = re.compile("[" + re.escape("".join(_ESCAPES)) + "]")

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# "~90%" is the résumé's own idiom for "about 90%", written `$\sim$90\%`.
_APPROX = re.compile(r"~(?=\d)")


def latex_escape(text: str) -> str:
    """Escape model-written prose so it is literal text in LaTeX."""
    return _ESCAPE_RE.sub(lambda match: _ESCAPES[match.group()], text)


def to_latex(text: str) -> str:
    """Convert a rewritten line from the marker dialect into LaTeX."""
    text = " ".join(text.split())
    text = _APPROX.sub("\x00APPROX\x00", text)
    parts = _BOLD.split(text)
    # split() alternates literal, captured, literal, captured, ...
    out = [
        latex_escape(part) if index % 2 == 0 else rf"\textbf{{{latex_escape(part)}}}"
        for index, part in enumerate(parts)
    ]
    return "".join(out).replace("\x00APPROX\x00", r"$\sim$")


def from_latex(tex: str) -> str:
    """Render existing LaTeX back into the marker dialect, for the prompt.

    Lossy on purpose. It exists so the model reads a bullet the way a person
    would, and so a bullet it declines to rewrite can be compared against the
    text it was shown.
    """
    text = re.sub(r"\\textbf\{([^{}]*)\}", r"**\1**", tex)
    text = re.sub(r"\\(?:textit|emph)\{([^{}]*)\}", r"\1", text)
    text = text.replace(r"$\sim$", "~")
    text = re.sub(r"\\href\{[^{}]*\}\{([^{}]*)\}", r"\1", text)
    # Unescape before stripping macros, or `\%` loses its percent sign.
    for raw, escaped in _ESCAPES.items():
        if escaped.endswith("{}"):
            continue
        text = text.replace(escaped, raw)
    text = re.sub(r"\\[a-zA-Z]+\s*", "", text)
    text = text.replace("{", "").replace("}", "")
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Parsed template
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Region:
    """One editable span of the template."""

    id: str
    kind: str  # "summary" | "bullet" | "skills_row"
    group: str  # position slug, "projects", or "skills"
    label: str  # human heading for the UI ("AsInt — Senior Full Stack Developer")
    start: int
    end: int
    tex: str  # the original LaTeX of this span

    @property
    def text(self) -> str:
        return from_latex(self.tex)


class BulletEdit(BaseModel):
    """One decision about one region."""

    id: str
    include: bool = True
    text: str | None = None


class ResumeEdits(BaseModel):
    """Everything the tailoring pass is allowed to change."""

    summary: str | None = None
    bullets: list[BulletEdit] = Field(default_factory=list)
    # Ids in the order they should appear. Ids left out keep their original
    # position relative to each other, after the ones named here.
    order: list[str] = Field(default_factory=list)

    def by_id(self) -> dict[str, BulletEdit]:
        return {edit.id: edit for edit in self.bullets}


_SECTION = re.compile(
    r"\\begin\{rSection\}\{(?P<name>[^}]*)\}(?P<body>.*?)\\end\{rSection\}", re.DOTALL
)
_ITEMIZE = re.compile(r"\\begin\{itemize\}(?P<body>.*?)\\end\{itemize\}", re.DOTALL)
_ITEM = re.compile(r"\\item\s")
# `\textbf{Senior Full Stack Developer} \hfill Nov 2022 -- Present\\` then
# `AsInt \hfill \textit{Ahmedabad, India}`.
_POSITION = re.compile(
    r"\\textbf\{(?P<title>[^{}]+)\}\s*\\hfill\s*(?P<dates>[^\\]*)\\\\\s*\n"
    r"(?P<company>[^\\\n]+?)\s*\\hfill",
)
_TABULAR = re.compile(r"\\begin\{tabular\}[^\n]*\n(?P<body>.*?)\\end\{tabular\}", re.DOTALL)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


@dataclass(slots=True)
class ResumeDocument:
    """The base résumé, with its editable regions located."""

    tex: str
    regions: list[Region] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> ResumeDocument:
        path = resolve_template(path)
        if not path.is_file():
            raise ResumeTemplateError(
                f"résumé template not found at {path} — upload your résumé's .tex from the "
                "dashboard, or drop it in beside the PDF. Tailoring re-words regions of the "
                "real document, so it cannot start from nothing."
            )
        return cls.parse(path.read_text(encoding="utf-8"))

    @classmethod
    def parse(cls, tex: str) -> ResumeDocument:
        regions: list[Region] = []
        for section in _SECTION.finditer(tex):
            name = section.group("name").strip().upper()
            body = section.group("body")
            offset = section.start("body")
            if name == "SUMMARY":
                regions.extend(_parse_summary(body, offset))
            elif name in {"EXPERIENCE", "PROJECTS"}:
                regions.extend(_parse_bullets(body, offset, section=name))
            elif name == "SKILLS":
                regions.extend(_parse_skills(body, offset))

        if not regions:
            raise ResumeTemplateError(
                "no editable regions found — the template does not use the "
                "rSection/itemize structure this module parses"
            )
        regions.sort(key=lambda r: r.start)
        doc = cls(tex=tex, regions=regions)
        log.info(
            "resume_tex.parsed",
            extra={"regions": len(regions), "groups": len({r.group for r in regions})},
        )
        return doc

    # -- lookup -----------------------------------------------------------

    def region(self, region_id: str) -> Region | None:
        return next((r for r in self.regions if r.id == region_id), None)

    def groups(self) -> dict[str, list[Region]]:
        out: dict[str, list[Region]] = {}
        for region in self.regions:
            out.setdefault(region.group, []).append(region)
        return out

    # -- rendering --------------------------------------------------------

    def render(self, edits: ResumeEdits) -> str:
        """Apply `edits` and return the new .tex.

        Unknown ids are ignored rather than raising: a model that invents a
        bullet id should lose that one edit, not the whole document.
        """
        by_id = edits.by_id()
        known = {region.id for region in self.regions}
        for unknown in sorted(set(by_id) - known):
            log.info("resume_tex.unknown_region", extra={"id": unknown})

        out = self.tex
        # Bullets are reordered within their own group, so a position's
        # bullets can never migrate into another job.
        reordered = self._reordered(edits.order)

        # Last region first: every replacement leaves earlier offsets intact.
        for region in sorted(self.regions, key=lambda r: r.start, reverse=True):
            if region.kind == "summary":
                replacement = self._render_summary(region, edits.summary)
            else:
                replacement = self._render_bullet(region, by_id, reordered)
            if replacement is None:
                continue
            out = out[: region.start] + replacement + out[region.end :]
        return out

    def _reordered(self, order: list[str]) -> dict[str, str]:
        """Map each region id to the LaTeX that should occupy its slot."""
        if not order:
            return {}
        rank = {region_id: index for index, region_id in enumerate(order)}
        moved: dict[str, str] = {}
        for group, regions in self.groups().items():
            if group == "skills" or len(regions) < 2:
                continue
            ids = [r.id for r in regions]
            # Stable: named ids sort by the model's order, the rest keep theirs.
            ranked = sorted(ids, key=lambda i: (rank.get(i, len(rank) + ids.index(i)),))
            if ranked == ids:
                continue
            source = {r.id: r for r in regions}
            for slot, wanted in zip(ids, ranked, strict=True):
                moved[slot] = source[wanted].id
        return moved

    def _render_summary(self, region: Region, summary: str | None) -> str | None:
        return None if not summary or not summary.strip() else to_latex(summary)

    def _render_bullet(
        self, region: Region, by_id: dict[str, BulletEdit], reordered: dict[str, str]
    ) -> str | None:
        """The LaTeX for this slot, or None to leave the original alone."""
        source_id = reordered.get(region.id, region.id)
        source = self.region(source_id) or region
        edit = by_id.get(source_id)

        if edit is not None and not edit.include:
            if region.kind == "skills_row":
                # Dropping a whole skills row would leave a dangling `\\`.
                # Rows are reworded, never removed.
                return None
            return ""
        if edit is not None and edit.text and edit.text.strip():
            body = to_latex(edit.text)
            # Bullets carry their own `\item`; a skills cell must not gain one.
            return body if region.kind == "skills_row" else f"\\item {body}"
        # Reordered into this slot but not reworded: carry the original text.
        return source.tex if source_id != region.id else None


def _parse_summary(body: str, offset: int) -> list[Region]:
    """The SUMMARY paragraph.

    Taken as the longest non-command line in the section rather than by
    position: the paragraph opens with `\\textbf{...}`, so "a line that does not
    start with a backslash" does not find it, and counting `\\vspace` markers
    would break the first time one is added.
    """
    best: re.Match[str] | None = None
    for match in re.finditer(r"(?m)^(?P<text>\S.*\S)$", body):
        text = match.group("text")
        if re.match(r"\\(?:vspace|begin|end|item|hrule|smallskip|medskip)\b", text):
            continue
        if best is None or len(text) > len(best.group("text")):
            best = match
    if best is None:
        return []
    return [
        Region(
            id="summary",
            kind="summary",
            group="summary",
            label="Summary",
            start=offset + best.start("text"),
            end=offset + best.end("text"),
            tex=best.group("text"),
        )
    ]


def _parse_bullets(body: str, offset: int, *, section: str) -> list[Region]:
    """Every `\\item` inside every itemize block, grouped by its position."""
    regions: list[Region] = []
    for block in _ITEMIZE.finditer(body):
        block_body = block.group("body")
        block_offset = offset + block.start("body")

        if section == "PROJECTS":
            group, label = "projects", "Projects"
        else:
            headers = list(_POSITION.finditer(body, 0, block.start()))
            if headers:
                header = headers[-1]
                company = header.group("company").strip()
                group = f"exp.{_slug(company)}"
                label = f"{company} — {header.group('title').strip()}"
            else:
                group, label = f"exp.{len(regions)}", "Experience"

        # A region starts AT its `\item`, not after it. Dropping a bullet has
        # to take the marker with it, or the PDF renders an empty bullet — the
        # first cut of this did exactly that.
        items = list(_ITEM.finditer(block_body))
        for index, item in enumerate(items):
            start = item.start()
            # Up to the next bullet, or the end of the block. `end` is the
            # offset of the trimmed text, not of the whitespace after it: the
            # blank line between bullets belongs to the template and must
            # survive a replacement.
            limit = items[index + 1].start() if index + 1 < len(items) else len(block_body)
            end = start + len(block_body[start:limit].rstrip())
            regions.append(
                Region(
                    id=f"{group}.{index}" if group != "projects" else f"proj.{index}",
                    kind="bullet",
                    group=group,
                    label=label,
                    start=block_offset + start,
                    end=block_offset + end,
                    tex=block_body[start:end].rstrip(),
                )
            )
    return regions


def _parse_skills(body: str, offset: int) -> list[Region]:
    """Each row of the SKILLS tabular — its label, and the cell it heads.

    Split on the `\\\\` row terminator rather than matched with one regex: the
    row labels contain escaped ampersands ("AI \\& Machine Learning"), so any
    pattern that treats `&` as the column separator cuts the label in half.
    """
    table = _TABULAR.search(body)
    if not table:
        return []
    text = table.group("body")
    base = offset + table.start("body")

    regions: list[Region] = []
    cursor = 0
    for terminator in re.finditer(r"(?m)^\\\\\s*$", text):
        row_start, cursor = cursor, terminator.end()
        row = text[row_start : terminator.start()]
        split = re.search(r"\n&[ \t]*", row)
        if not split:
            continue
        label = " ".join(row[: split.start()].split())
        if not label:
            continue
        cell = row[split.end() :].rstrip()
        cell_start = base + row_start + split.end()
        regions.append(
            Region(
                id=f"skills.{_slug(from_latex(label))}",
                kind="skills_row",
                group="skills",
                label=from_latex(label),
                start=cell_start,
                end=cell_start + len(cell),
                tex=cell,
            )
        )
    return regions
