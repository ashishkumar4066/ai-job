"""Stage 3 service layer — generate, store, edit and compile tailored documents.

Two kinds share one table and one flow: the **résumé** (`tailor.py` over the
`resume_tex.py` template) and the **cover letter** (`cover_letter.py`, prose
rendered into its own LaTeX). Both compile through `latex.py`. The API handlers
stay thin, and the whole flow is testable without HTTP.

The PDF cache
-------------
PDFs are not stored in the database — the LaTeX is the source of truth and a
compile is ~3.5s. But the editor's preview and its Download button must serve
the *same bytes*, and compiling twice for one save would be both slow and, in
principle, divergent. So a compile keeps its output in a small in-process cache
keyed by (document id, hash of the .tex that produced it).

In-process is the right scope here and not laziness: the cache is a memo of a
pure function, a restart costs one recompile, and this is a single-user tool
with one server. It is bounded so a long editing session cannot grow it without
limit.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cover_letter
from app.config import Settings, get_settings
from app.factcheck import FactCorpus, FactIssue
from app.latex import CompileResult, compile_tex
from app.models import GeneratedDocument, JobPosting
from app.profile import Profile, get_profile
from app.resume_tex import ResumeDocument, ResumeTemplateError
from app.tailor import TAILOR_PROMPT_VERSION, TailoredResume, tailor_resume

log = logging.getLogger(__name__)

RESUME = "resume"
COVER_LETTER = "cover_letter"
KINDS = (RESUME, COVER_LETTER)

# Which prompt revision a current document of each kind must carry.
_PROMPT_VERSIONS = {
    RESUME: TAILOR_PROMPT_VERSION,
    COVER_LETTER: cover_letter.COVER_PROMPT_VERSION,
}

# Each entry is one compiled résumé — ~30KB. 24 of them is under a megabyte
# and covers far more documents than one sitting touches.
_PDF_CACHE_SIZE = 24
_pdf_cache: OrderedDict[tuple[int, str], bytes] = OrderedDict()


class DocumentError(RuntimeError):
    """The document could not be produced. The message is shown to the user."""


def tex_hash(tex: str) -> str:
    return hashlib.sha256(tex.encode("utf-8")).hexdigest()[:16]


def cache_pdf(doc_id: int, tex: str, pdf: bytes) -> str:
    key = (doc_id, tex_hash(tex))
    _pdf_cache[key] = pdf
    _pdf_cache.move_to_end(key)
    while len(_pdf_cache) > _PDF_CACHE_SIZE:
        _pdf_cache.popitem(last=False)
    return key[1]


def cached_pdf(doc_id: int, tex: str) -> bytes | None:
    key = (doc_id, tex_hash(tex))
    pdf = _pdf_cache.get(key)
    if pdf is not None:
        _pdf_cache.move_to_end(key)
    return pdf


def drop_cached(doc_id: int) -> None:
    for key in [k for k in _pdf_cache if k[0] == doc_id]:
        del _pdf_cache[key]


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


async def get_document(
    session: AsyncSession, job_id: int, *, profile_version: str, kind: str = RESUME
) -> GeneratedDocument | None:
    return await session.scalar(
        select(GeneratedDocument).where(
            GeneratedDocument.job_id == job_id,
            GeneratedDocument.profile_version == profile_version,
            GeneratedDocument.kind == kind,
        )
    )


async def get_job(session: AsyncSession, job_id: int) -> JobPosting:
    job = await session.get(JobPosting, job_id)
    if job is None:
        raise DocumentError(f"job {job_id} not found")
    return job


def is_stale(document: GeneratedDocument, job: JobPosting) -> bool:
    """The JD moved, or the prompt did, since this was generated.

    Not an error and not auto-fixed — the user is told, and regenerates if they
    want to. Silently re-billing an LLM call because a board reformatted its
    HTML is exactly the behaviour the cache keys exist to prevent.
    """
    if document.content_hash and document.content_hash != (job.content_hash or ""):
        return True
    current = _PROMPT_VERSIONS.get(document.kind, TAILOR_PROMPT_VERSION)
    return bool(document.prompt_version) and document.prompt_version != current


# --------------------------------------------------------------------------
# Generate
# --------------------------------------------------------------------------


@dataclass(slots=True)
class GenerateResult:
    document: GeneratedDocument
    tailored: TailoredResume | None
    reused: bool


async def generate_resume(
    session: AsyncSession,
    job_id: int,
    *,
    force: bool = False,
    profile: Profile | None = None,
    settings: Settings | None = None,
    template: ResumeDocument | None = None,
) -> GenerateResult:
    """Tailor the résumé for one job, or hand back the one already stored.

    Cache-correct by CLAUDE.md's rule: unchanged JD *and* unchanged profile
    means zero API calls. `profile_version` is part of the row's key, so a
    profile edit naturally misses; `content_hash` is checked here.
    """
    settings = settings or get_settings()
    profile = profile or get_profile()
    job = await get_job(session, job_id)

    existing = await get_document(session, job_id, profile_version=profile.version)
    if existing is not None and not force and not is_stale(existing, job):
        log.info("documents.reused", extra={"job_id": job_id, "doc_id": existing.id})
        return GenerateResult(document=existing, tailored=None, reused=True)

    if existing is not None and existing.hand_edited and not force:
        # Regenerating would silently discard the user's own edits.
        raise DocumentError(
            "this résumé has been edited by hand — pass force=true to regenerate "
            "and discard those edits"
        )

    template = template or ResumeDocument.load()
    tailored = await tailor_resume(
        title=job.title,
        company=job.company,
        description_text=job.description_text,
        profile=profile,
        document=template,
        settings=settings,
    )

    document = existing or GeneratedDocument(
        job_id=job_id, profile_version=profile.version, kind=RESUME
    )
    document.tex = tailored.tex
    document.content_hash = job.content_hash or ""
    document.prompt_version = TAILOR_PROMPT_VERSION
    document.edits = tailored.edits.model_dump()
    document.tailoring = tailored.tailoring.model_dump()
    document.issues = _issue_rows(tailored.issues)
    document.llm_used = True
    document.llm_tokens = tailored.tokens
    document.hand_edited = False
    document.is_draft = True
    # A new résumé starts a new conversation: the old proposals name regions
    # and "before" texts of a document that no longer exists.
    document.chat = []
    document.updated_at = datetime.now(UTC)
    session.add(document)
    await session.commit()
    await session.refresh(document)
    drop_cached(document.id)

    log.info(
        "documents.generated",
        extra={
            "job_id": job_id,
            "doc_id": document.id,
            "tokens": tailored.tokens,
            "rejected": len(tailored.rejected),
        },
    )
    return GenerateResult(document=document, tailored=tailored, reused=False)


def _issue_rows(issues: list[FactIssue]) -> list[dict[str, str]]:
    return [
        {
            "region_id": issue.region_id,
            "kind": issue.kind,
            "token": issue.token,
            "severity": issue.severity,
            "message": issue.message,
            "text": issue.text,
        }
        for issue in issues
    ]


async def generate_cover_letter(
    session: AsyncSession,
    job_id: int,
    *,
    force: bool = False,
    profile: Profile | None = None,
    settings: Settings | None = None,
) -> GenerateResult:
    """Draft the cover letter for one job, or hand back the one already stored.

    Same cache rule and same hand-edit guard as `generate_resume`.
    """
    settings = settings or get_settings()
    profile = profile or get_profile()
    job = await get_job(session, job_id)

    existing = await get_document(
        session, job_id, profile_version=profile.version, kind=COVER_LETTER
    )
    if existing is not None and not force and not is_stale(existing, job):
        log.info("documents.reused", extra={"job_id": job_id, "doc_id": existing.id})
        return GenerateResult(document=existing, tailored=None, reused=True)

    if existing is not None and existing.hand_edited and not force:
        raise DocumentError(
            "this cover letter has been edited by hand — pass force=true to regenerate "
            "and discard those edits"
        )

    try:
        letter = await cover_letter.write_cover_letter(
            title=job.title,
            company=job.company,
            description_text=job.description_text,
            profile=profile,
            settings=settings,
        )
    except ValueError as exc:
        raise DocumentError(str(exc)) from exc

    document = existing or GeneratedDocument(
        job_id=job_id, profile_version=profile.version, kind=COVER_LETTER
    )
    document.tex = letter.tex
    document.content_hash = job.content_hash or ""
    document.prompt_version = cover_letter.COVER_PROMPT_VERSION
    # What was rendered, and the header facts it was rendered with, so Revert
    # can rebuild the exact letter with no LLM call.
    document.edits = {
        "paragraphs": letter.paragraphs,
        "company": job.company,
        "title": job.title,
        "dated": letter.dated,
    }
    document.tailoring = letter.draft.model_dump()
    document.issues = _issue_rows(letter.issues)
    document.llm_used = True
    document.llm_tokens = letter.tokens
    document.hand_edited = False
    document.is_draft = True
    document.chat = []
    document.updated_at = datetime.now(UTC)
    session.add(document)
    await session.commit()
    await session.refresh(document)
    drop_cached(document.id)

    log.info(
        "documents.generated",
        extra={"job_id": job_id, "doc_id": document.id, "kind": COVER_LETTER, "tokens": letter.tokens},
    )
    return GenerateResult(document=document, tailored=None, reused=False)


async def generate(
    session: AsyncSession, job_id: int, *, kind: str = RESUME, force: bool = False
) -> GenerateResult:
    if kind == COVER_LETTER:
        return await generate_cover_letter(session, job_id, force=force)
    return await generate_resume(session, job_id, force=force)


# --------------------------------------------------------------------------
# Hand edits
# --------------------------------------------------------------------------


async def save_tex(session: AsyncSession, document: GeneratedDocument, tex: str) -> GeneratedDocument:
    """Store the user's own LaTeX. Marks the row hand-edited."""
    if not tex.strip():
        raise DocumentError("refusing to save an empty document")
    changed = tex != document.tex
    document.tex = tex
    if changed:
        document.hand_edited = True
        document.updated_at = datetime.now(UTC)
        drop_cached(document.id)
    await session.commit()
    await session.refresh(document)
    return document


async def revert(session: AsyncSession, document: GeneratedDocument) -> GeneratedDocument:
    """Throw away hand edits and re-apply the stored tailoring. No LLM call.

    Possible because `edits` is persisted beside the text: the tailoring can be
    replayed against the template for free, which is the whole reason it is
    stored separately from `tex`.
    """
    from app.resume_tex import ResumeEdits

    if not document.edits:
        raise DocumentError("nothing to revert to — this document was never generated")
    if document.kind == COVER_LETTER:
        edits = document.edits
        document.tex = cover_letter.render_letter(
            list(edits.get("paragraphs") or []),
            profile=get_profile(),
            company=str(edits.get("company") or ""),
            title=str(edits.get("title") or ""),
            dated=str(edits.get("dated") or ""),
        )
    else:
        template = ResumeDocument.load()
        document.tex = template.render(ResumeEdits.model_validate(document.edits))
    document.hand_edited = False
    document.updated_at = datetime.now(UTC)
    drop_cached(document.id)
    await session.commit()
    await session.refresh(document)
    return document


# --------------------------------------------------------------------------
# Compile
# --------------------------------------------------------------------------


async def compile_document(document: GeneratedDocument, *, use_cache: bool = True) -> CompileResult:
    """Compile the document's current LaTeX, serving a cache hit when there is one."""
    if use_cache and (pdf := cached_pdf(document.id, document.tex)) is not None:
        return CompileResult(ok=True, pdf=pdf, log="(cached)", duration_s=0.0)

    result = await compile_tex(document.tex)
    if result.ok and result.pdf is not None:
        cache_pdf(document.id, document.tex, result.pdf)
    return result


def _similarity(left: str, right: str) -> float:
    """Token overlap of two bullets, 0..1. Used to pair a rewrite with its original."""
    a = set(left.lower().split())
    b = set(right.lower().split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

# Below this, two bullets are different bullets rather than a rewrite of one.
# A tailored bullet keeps its employer, stack and metrics and re-words the
# connective tissue, so a genuine rewrite scores far higher than this; 0.25 only
# has to beat "these two bullets both contain 'and' and 'the'".
_REWRITE_THRESHOLD = 0.25


def diff_against_base(
    current_tex: str, template: ResumeDocument
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Region-by-region comparison of a tailored résumé against the template.

    CLAUDE.md asks for "a diff against the base résumé so I can see exactly
    what was changed". Done on **regions rather than lines**, because the
    tailored file is rendered from the template by splicing: a line diff of the
    LaTeX reports brace and wrapping noise around every touched bullet, which
    buries the one thing being looked for — whether a claim changed.

    **Bullets are matched by content, not by region id.** A bullet's id is its
    *position* (`exp.asint.3`), so dropping one renumbers every bullet after it
    and reordering a pair swaps their ids outright. Matching on id therefore
    reported a drop as "the last bullet was deleted plus five rewrites", and
    reported a reorder as two rewrites. Within each group, identical texts pair
    up first (giving `unchanged` or `moved`), then the leftovers pair by token
    overlap (`reworded`), then whatever is still unmatched is `dropped` on the
    base side and `added` on the current side.

    Non-bullet regions (`summary`, `skills.*`) do have stable ids and are
    matched on them.

    Pure, so the classification is testable without a database or a compile.
    """
    current = ResumeDocument.parse(current_tex)
    rows: list[dict[str, Any]] = []
    counts = {"reworded": 0, "dropped": 0, "moved": 0, "unchanged": 0, "added": 0}

    def add(status: str, **row: Any) -> None:
        counts[status] += 1
        rows.append({"status": status, **row})

    # --- Stable-id regions ------------------------------------------------
    base_named = {r.id: r for r in template.regions if r.kind != "bullet"}
    current_named = {r.id: r for r in current.regions if r.kind != "bullet"}
    for region_id, base in base_named.items():
        live = current_named.get(region_id)
        if live is None:
            status = "dropped"
        elif live.text != base.text:
            status = "reworded"
        else:
            status = "unchanged"
        add(
            status,
            region_id=region_id,
            label=base.label,
            kind=base.kind,
            group=base.group,
            base=base.text,
            current=live.text if live is not None else "",
            base_index=None,
            current_index=None,
        )
    for region_id, live in current_named.items():
        if region_id in base_named:
            continue
        add(
            "added",
            region_id=region_id,
            label=live.label,
            kind=live.kind,
            group=live.group,
            base="",
            current=live.text,
            base_index=None,
            current_index=None,
        )

    # --- Bullets, grouped and aligned by content --------------------------
    def by_group(doc: ResumeDocument) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for region in doc.regions:
            if region.kind == "bullet":
                out.setdefault(region.group, []).append(region)
        return out

    base_groups, current_groups = by_group(template), by_group(current)
    for group in list(base_groups) + [g for g in current_groups if g not in base_groups]:
        base_list = base_groups.get(group, [])
        current_list = current_groups.get(group, [])
        taken: set[int] = set()
        pairs: dict[int, int] = {}  # base index -> current index

        # Identical text first, nearest position preferred, so a duplicated
        # bullet pairs with the copy it most likely came from.
        for bi, base in enumerate(base_list):
            candidates = [
                ci
                for ci, live in enumerate(current_list)
                if ci not in taken and live.text == base.text
            ]
            if candidates:
                ci = min(candidates, key=lambda c: abs(c - bi))
                pairs[bi] = ci
                taken.add(ci)

        # Then rewrites, best overlap first so the strongest pairing wins even
        # when two bullets both resemble the same original.
        scored = sorted(
            (
                (_similarity(base.text, current_list[ci].text), bi, ci)
                for bi, base in enumerate(base_list)
                if bi not in pairs
                for ci in range(len(current_list))
                if ci not in taken
            ),
            key=lambda s: (-s[0], s[1], s[2]),
        )
        for score, bi, ci in scored:
            if score < _REWRITE_THRESHOLD or bi in pairs or ci in taken:
                continue
            pairs[bi] = ci
            taken.add(ci)

        for bi, base in enumerate(base_list):
            ci = pairs.get(bi)
            if ci is None:
                status = "dropped"
            elif current_list[ci].text != base.text:
                status = "reworded"
            elif ci != bi:
                status = "moved"
            else:
                status = "unchanged"
            add(
                status,
                region_id=base.id,
                label=base.label,
                kind=base.kind,
                group=base.group,
                base=base.text,
                current=current_list[ci].text if ci is not None else "",
                base_index=bi,
                current_index=ci,
            )
        for ci, live in enumerate(current_list):
            if ci in taken:
                continue
            # Tailoring has no slot to add a bullet, so this is a structural
            # hand edit. Hiding it would make the diff a partial account.
            add(
                "added",
                region_id=live.id,
                label=live.label,
                kind=live.kind,
                group=live.group,
                base="",
                current=live.text,
                base_index=None,
                current_index=ci,
            )

    # Read top to bottom like the document, not grouped by verdict.
    order = {r.id: i for i, r in enumerate(template.regions)}
    rows.sort(
        key=lambda r: (
            order.get(r["region_id"], len(order) + (r["current_index"] or 0)),
            r["status"] == "added",
        )
    )
    return rows, counts


def check_facts(tex: str, profile: Profile, template: ResumeDocument) -> list[str]:
    """Re-run the fact check over arbitrary LaTeX — used after a hand edit.

    The guarantee CLAUDE.md asks for is about the *document*, not about the
    model, so it has to survive the user editing the text themselves. Returns
    plain messages; a hand edit is never blocked, only reported.
    """
    corpus = FactCorpus.build(profile, template.tex)
    parsed = ResumeDocument.parse(tex)
    messages: list[str] = []
    for region in parsed.regions:
        for issue in corpus.check(region.id, region.text):
            if issue.severity == "blocking":
                messages.append(f"{region.label}: {issue.message}")
    return messages


def hand_edit_warnings(document: GeneratedDocument, job: JobPosting, profile: Profile) -> list[str]:
    """Blocking fact-check findings over a document's CURRENT text, by kind."""
    if document.kind == COVER_LETTER:
        try:
            resume_tex: str | None = ResumeDocument.load().tex
        except ResumeTemplateError:
            resume_tex = None
        corpus = cover_letter.build_corpus(
            profile,
            company=job.company,
            title=job.title,
            description_text=job.description_text,
            resume_tex=resume_tex,
        )
        return cover_letter.check_letter(document.tex, corpus)
    return check_facts(document.tex, profile, ResumeDocument.load())
