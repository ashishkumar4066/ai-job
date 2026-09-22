"""Stage 3 service layer — generate, store, edit and compile a tailored résumé.

Sits between the API and the three modules that do the work: `tailor.py` (the
LLM pass), `resume_tex.py` (the template) and `latex.py` (the compile). The API
handlers stay thin, and the whole flow is testable without HTTP.

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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.factcheck import FactCorpus
from app.latex import CompileResult, compile_tex
from app.models import GeneratedDocument, JobPosting
from app.profile import Profile, get_profile
from app.resume_tex import ResumeDocument
from app.tailor import TAILOR_PROMPT_VERSION, TailoredResume, tailor_resume

log = logging.getLogger(__name__)

RESUME = "resume"

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
    return bool(document.prompt_version) and document.prompt_version != TAILOR_PROMPT_VERSION


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
    document.issues = [
        {
            "region_id": issue.region_id,
            "kind": issue.kind,
            "token": issue.token,
            "severity": issue.severity,
            "message": issue.message,
            "text": issue.text,
        }
        for issue in tailored.issues
    ]
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
