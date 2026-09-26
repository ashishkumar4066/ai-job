"""Stage 3 — refine a cover letter by chatting about it.

`resume_chat.py`'s sibling, and deliberately the same *contract*: a reply is a
proposal, never an edit; the model sees region ids and plain text, never LaTeX;
and every change is fact-checked before it is offered. The API routes and the
dashboard's chat panel therefore work on either kind without branching — this
module exposes `send_message`, `apply_proposal`, `dismiss_proposal`,
`clear_chat` and `suggestions` with matching signatures, and builds proposals in
the same shape.

What differs, and why
---------------------
The résumé is a template with 16 fixed slots, so chat addresses *regions of a
document that already exists*. A letter is prose: its regions are simply its
body paragraphs, `para.0 … para.N`, read back out of the stored LaTeX by
`cover_letter.body_paragraphs`. That has three consequences:

* **A paragraph can be added.** The résumé has no slot to add a bullet to,
  which is a guarantee worth keeping. A letter has no such structure, so
  `para.N` where N is one past the end appends — the fact check is what
  constrains it, not the absence of a slot.
* **The fact check is per SENTENCE**, as everywhere in `cover_letter.py`, and a
  paragraph carrying a blocked sentence is offered as `blocked` **whole**
  rather than silently shortened. Generation removes an offending sentence
  because there is no alternative to fall back on; a chat *does* have one — the
  paragraph as it stands — and quietly returning a shorter paragraph than the
  user asked for would hide the refusal.
* **Only the body is ever written.** `cover_letter.replace_body` splices between
  the body markers, so the header, date and sign-off cannot move.

The corpus is `cover_letter.build_corpus`: profile + base résumé + the
posting's words but not its numbers, with `gaps:` blocking. Built from the
BASE résumé, never from the current letter, or a hand edit would launder an
invented claim into "already stated".
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cover_letter
from app.config import Settings, get_settings
from app.documents import DocumentError, drop_cached, tex_hash
from app.factcheck import FactCorpus
from app.llm import LLMScreener, profile_brief
from app.models import GeneratedDocument, JobMatch, JobPosting
from app.profile import Profile, get_profile
from app.resume_tex import ResumeDocument, ResumeTemplateError

log = logging.getLogger(__name__)

CHAT_PROMPT_VERSION: Final[str] = f"2026-09-25.1+{cover_letter.COVER_PROMPT_VERSION}"

_HISTORY_TURNS: Final[int] = 8
_MAX_MESSAGE_CHARS: Final[int] = 2_000
# A letter is 3-4 paragraphs. The ceiling stops a model that has decided to
# restructure everything from proposing a twelve-paragraph essay.
_MAX_PARAGRAPHS: Final[int] = 6


class ChatChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    include: bool = True
    rewritten: str = ""
    reason: str = ""


class ChatAnswer(BaseModel):
    """One assistant turn. Mirrors `CHAT_SCHEMA`."""

    model_config = ConfigDict(frozen=True)

    changes: list[ChatChange] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    reply: str = ""


_MARKER_RULE: Final[str] = (
    "Write plain text. Mark emphasis as **bold**, sparingly. Never write LaTeX, backslashes "
    "or braces."
)

CHAT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    # Changes first, reply last, so the reply describes decisions already made.
    "required": ["changes", "order", "reply"],
    "properties": {
        "changes": {
            "type": "array",
            "maxItems": 6,
            "description": (
                "Up to 6 edits to the letter's paragraphs that do what the user asked. EMPTY "
                "when they asked a question, asked for something the profile cannot support, or "
                "no edit is needed. Only change the paragraphs the request is about."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "include", "rewritten", "reason"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": (
                            "Copy a paragraph id from the LETTER list exactly ('para.0', "
                            "'para.2'). To ADD a paragraph, use the next id after the last one "
                            "listed. Never skip a number."
                        ),
                    },
                    "include": {
                        "type": "boolean",
                        "description": (
                            "false to DELETE this paragraph from the letter. true to rewrite "
                            "or add it."
                        ),
                    },
                    "rewritten": {
                        "type": "string",
                        "description": (
                            "The full new text of this paragraph, 2-5 sentences. EMPTY STRING "
                            "when deleting it. Every claim must already be stated in the "
                            "candidate's profile or résumé: name their real employers, "
                            "technologies and metrics, and never add a number or a technology "
                            "they have not shown. " + _MARKER_RULE
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "One short clause: why this edit answers the request.",
                    },
                },
            },
        },
        "order": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string"},
            "description": (
                "Paragraph ids in their new order, ONLY when the user asked to reorder or to "
                "lead with something. Must list every paragraph that is staying. EMPTY "
                "otherwise."
            ),
        },
        "reply": {
            "type": "string",
            "description": (
                "Your answer to the user, 1-4 sentences, plain text, addressed to them as "
                "'you'. Say what the edits do. If they asked for something the profile does "
                "not support (a technology listed under gaps, a metric the résumé never "
                "states), say so plainly and suggest they add it to profile.yaml if it is "
                "true — do not make the edit."
            ),
        },
    },
}

_SYSTEM_PROMPT: Final[str] = (
    "You are helping one candidate refine their cover letter for one job posting, in a chat. "
    "Every claim in the letter must already be stated in the candidate's profile or résumé: "
    "never invent an employer, a date, a title, a technology or a metric, even when the user "
    "asks you to — a letter that overstates is a lie the candidate has to defend in an "
    "interview. Refuse those requests in the reply and explain why. You may freely describe "
    "what the employer does, using the posting's own words. "
    "Every edit you propose is shown to the user as a suggestion they accept or reject. "
    "Follow each field's description exactly."
)


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


def _paragraphs(document: GeneratedDocument) -> list[str]:
    paragraphs = cover_letter.body_paragraphs(document.tex)
    if paragraphs is None:
        raise DocumentError(
            "this letter's body markers were removed, so chat cannot address its paragraphs "
            "— revert it, or put the `% --- BODY` comments back"
        )
    return paragraphs


def _regions_block(paragraphs: list[str]) -> str:
    return "\n\n".join(f"para.{i}: {text}" for i, text in enumerate(paragraphs)) or "(empty)"


def _history_line(message: dict[str, Any]) -> str:
    if message.get("role") == "user":
        return f"USER: {message.get('text', '')}"
    text = f"ASSISTANT: {message.get('text', '')}"
    proposal = message.get("proposal") or {}
    changes = proposal.get("changes") or []
    if changes:
        ids = ", ".join(c["region_id"] for c in changes)
        text += f" [proposed edits to {ids} — {proposal.get('status', 'pending')}]"
    return text


def build_messages(
    *,
    job: JobPosting,
    profile_text: str,
    paragraphs: list[str],
    history: list[dict[str, Any]],
    message: str,
    max_jd_chars: int,
) -> list[dict[str, str]]:
    jd = " ".join((job.description_text or "").split())[:max_jd_chars] or "(no description)"
    past = "\n".join(_history_line(m) for m in history[-_HISTORY_TURNS:]) or "(none)"
    user = (
        f"POSTING\nTitle: {job.title}\nCompany: {job.company}\n\n{jd}\n\n"
        f"CANDIDATE\n{profile_text}\n\n"
        f"LETTER AS IT STANDS NOW (id: text)\n{_regions_block(paragraphs)}\n\n"
        f"CONVERSATION SO FAR\n{past}\n\n"
        f"USER'S NEW MESSAGE\n{message}"
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------


def _label(index: int) -> str:
    return f"Paragraph {index + 1}"


def _index_of(region_id: str) -> int | None:
    if not region_id.startswith("para."):
        return None
    try:
        index = int(region_id.split(".", 1)[1])
    except ValueError:
        return None
    return index if index >= 0 else None


def build_proposal(
    answer: ChatAnswer, paragraphs: list[str], corpus: FactCorpus
) -> dict[str, Any]:
    """Turn the model's answer into a reviewable, fact-checked proposal.

    Pure — no database, no network — so the guarantees are testable with a
    canned answer, exactly like `cover_letter.apply_draft`. The returned shape
    matches `resume_chat.build_proposal`'s, so one UI renders both.
    """
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for change in answer.changes:
        index = _index_of(change.id)
        if index is None or change.id in seen:
            log.info("cover_chat.unknown_region", extra={"id": change.id})
            continue
        # One past the end appends; further out is the model losing count, and
        # accepting it would leave a gap in the numbering.
        if index > len(paragraphs):
            log.info("cover_chat.region_out_of_range", extra={"id": change.id})
            continue
        existing = paragraphs[index] if index < len(paragraphs) else ""
        seen.add(change.id)

        drop = not change.include and bool(existing)
        after = "" if drop else " ".join(change.rewritten.split())
        if not drop and (not after or after == existing):
            continue  # Says "change" but changes nothing.
        if not drop and not existing and len(paragraphs) >= _MAX_PARAGRAPHS:
            log.info("cover_chat.too_many_paragraphs", extra={"id": change.id})
            continue

        blocked: list[str] = []
        warnings: list[str] = []
        if not drop:
            # Per sentence, as everywhere in `cover_letter`: a blocking claim in
            # one sentence must not be excused by a paragraph that is otherwise
            # well supported.
            for sentence in cover_letter.split_sentences(after):
                for issue in corpus.check(change.id, sentence):
                    if issue.severity == "blocking":
                        blocked.append(f"{issue.message} (in: “{sentence[:80]}”)")
                    else:
                        warnings.append(issue.message)

        changes.append(
            {
                "region_id": change.id,
                "label": _label(index),
                "kind": "paragraph",
                "action": "drop" if drop else ("add" if not existing else "rewrite"),
                "before": existing,
                "after": after,
                "reason": change.reason,
                "status": "blocked" if blocked else "proposed",
                "blocked": blocked,
                "warnings": warnings,
            }
        )

    current_ids = [f"para.{i}" for i in range(len(paragraphs))]
    order = [i for i in answer.order if i in set(current_ids)]
    # A partial ordering would silently drop the paragraphs it omits.
    if order and sorted(order) != sorted(current_ids):
        log.info("cover_chat.partial_order_ignored", extra={"order": order})
        order = []
    if order == current_ids:
        order = []

    return {
        "base_hash": tex_hash(_body_text(paragraphs)),
        "status": "pending" if (changes or order) else "none",
        "changes": changes,
        "order": order,
        "order_preview": [
            {"region_id": i, "text": paragraphs[_index_of(i) or 0][:90]} for i in order
        ],
    }


def _body_text(paragraphs: list[str]) -> str:
    """The body alone, for hashing.

    Hashing the *body* rather than the whole .tex is what lets a proposal
    survive an edit to the header or a re-render: chat only ever writes the
    body, so only the body's movement can invalidate a suggestion.
    """
    return "\n\n".join(paragraphs)


def apply_to_paragraphs(
    paragraphs: list[str], proposal: dict[str, Any], accept: list[str] | None
) -> list[str]:
    """The new paragraph list. Pure.

    `accept` names the region ids to apply (plus `"order"`); None means every
    change that was not blocked.
    """
    wanted = {c["region_id"] for c in proposal["changes"] if c["status"] == "proposed"}
    if proposal.get("order"):
        wanted.add("order")
    if accept is not None:
        wanted &= set(accept)
    if not wanted:
        raise DocumentError("nothing to apply — every change was blocked or deselected")

    if tex_hash(_body_text(paragraphs)) != proposal["base_hash"]:
        # The letter moved since this was suggested. Still safe to apply if
        # every paragraph we touch still reads as it did then.
        for change in proposal["changes"]:
            if change["region_id"] not in wanted:
                continue
            index = _index_of(change["region_id"])
            current = paragraphs[index] if index is not None and index < len(paragraphs) else ""
            if current != change["before"]:
                raise DocumentError(
                    f"{change['label']} changed since this was suggested — ask again"
                )
        if "order" in wanted and len(proposal["order"]) != len(paragraphs):
            raise DocumentError("the paragraphs moved since this was suggested — ask again")

    # Keyed by id through the rewrite so `order` still refers to the right
    # paragraph after one has been dropped or appended.
    keyed: dict[str, str] = {f"para.{i}": text for i, text in enumerate(paragraphs)}
    for change in proposal["changes"]:
        region_id = change["region_id"]
        if region_id not in wanted:
            continue
        if change["action"] == "drop":
            keyed.pop(region_id, None)
        else:
            keyed[region_id] = change["after"]

    if "order" in wanted:
        ordered = [i for i in proposal["order"] if i in keyed]
        ordered += [i for i in keyed if i not in set(ordered)]
    else:
        ordered = sorted(keyed, key=lambda i: _index_of(i) or 0)

    out = [keyed[i] for i in ordered if keyed[i].strip()]
    if not out:
        raise DocumentError("that would delete the whole letter")
    return out


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _message(role: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex[:12], "role": role, "text": text, "created_at": _now(), **extra}


def _corpus(profile: Profile, job: JobPosting) -> FactCorpus:
    try:
        resume_tex: str | None = ResumeDocument.load().tex
    except ResumeTemplateError:
        # The letter's own corpus still has the profile; the résumé only widens
        # it. Missing template means a stricter check, not a skipped one.
        resume_tex = None
    return cover_letter.build_corpus(
        profile,
        company=job.company,
        title=job.title,
        description_text=job.description_text,
        resume_tex=resume_tex,
    )


async def send_message(
    session: AsyncSession,
    document: GeneratedDocument,
    text: str,
    *,
    profile: Profile | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """One chat turn: ask the model, store both messages, return the history."""
    text = text.strip()[:_MAX_MESSAGE_CHARS]
    if not text:
        raise DocumentError("empty message")
    settings = settings or get_settings()
    profile = profile or get_profile()
    job = await session.get(JobPosting, document.job_id)
    if job is None:
        raise DocumentError(f"job {document.job_id} not found")

    paragraphs = _paragraphs(document)
    history = list(document.chat or [])
    messages = build_messages(
        job=job,
        profile_text=profile_brief(profile),
        paragraphs=paragraphs,
        history=history,
        message=text,
        max_jd_chars=settings.llm_max_jd_chars,
    )

    async with LLMScreener(settings) as client:
        answer: ChatAnswer = await client.complete(
            messages,
            schema=CHAT_SCHEMA,
            name="cover_chat",
            parse=lambda content: ChatAnswer.model_validate(json.loads(content)),
            max_completion_tokens=settings.llm_cover_completion_tokens,
        )
        tokens = client.tokens_spent

    proposal = build_proposal(answer, paragraphs, _corpus(profile, job))
    reply = answer.reply.strip() or (
        "Here are the edits." if proposal["status"] == "pending" else "No changes."
    )

    history.append(_message("user", text))
    history.append(
        _message(
            "assistant",
            reply,
            proposal=proposal,
            tokens=tokens,
            prompt_version=CHAT_PROMPT_VERSION,
        )
    )
    # Reassign, don't mutate: SQLAlchemy's JSON type only notices a new value.
    document.chat = history
    await session.commit()
    await session.refresh(document)
    log.info(
        "cover_chat.turn",
        extra={
            "doc_id": document.id,
            "tokens": tokens,
            "changes": len(proposal["changes"]),
            "blocked": sum(1 for c in proposal["changes"] if c["status"] == "blocked"),
        },
    )
    return history


def _find(
    document: GeneratedDocument, message_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    history = [dict(m) for m in (document.chat or [])]
    for message in history:
        if message["id"] == message_id and message.get("proposal"):
            return history, message
    raise DocumentError("no such suggestion")


async def apply_proposal(
    session: AsyncSession,
    document: GeneratedDocument,
    message_id: str,
    accept: list[str] | None = None,
) -> GeneratedDocument:
    """Write the accepted edits into the letter's body. Free — no LLM call."""
    history, message = _find(document, message_id)
    proposal = dict(message["proposal"])
    if proposal.get("status") != "pending":
        raise DocumentError(f"this suggestion is already {proposal.get('status')}")

    paragraphs = apply_to_paragraphs(_paragraphs(document), proposal, accept)
    try:
        document.tex = cover_letter.replace_body(document.tex, paragraphs)
    except ValueError as exc:
        raise DocumentError(str(exc)) from exc

    # Keep `edits.paragraphs` in step with the LaTeX, or Revert would replay a
    # letter that no longer matches what was on screen.
    edits = dict(document.edits or {})
    edits["paragraphs"] = paragraphs
    document.edits = edits

    applied = {c["region_id"] for c in proposal["changes"] if c["status"] == "proposed"} | (
        {"order"} if proposal.get("order") else set()
    )
    proposal["applied"] = sorted(applied if accept is None else applied & set(accept))
    proposal["status"] = "applied"
    message["proposal"] = proposal

    document.chat = history
    # Chat edits sit outside the stored draft, so Revert (which re-renders it)
    # undoes them — the same contract as any other hand edit.
    document.hand_edited = True
    document.updated_at = datetime.now(UTC)
    drop_cached(document.id)
    await session.commit()
    await session.refresh(document)
    return document


async def dismiss_proposal(
    session: AsyncSession, document: GeneratedDocument, message_id: str
) -> list[dict[str, Any]]:
    history, message = _find(document, message_id)
    proposal = dict(message["proposal"])
    if proposal.get("status") == "pending":
        proposal["status"] = "dismissed"
        message["proposal"] = proposal
        document.chat = history
        await session.commit()
        await session.refresh(document)
    return list(document.chat or [])


async def clear_chat(session: AsyncSession, document: GeneratedDocument) -> None:
    document.chat = []
    await session.commit()


async def suggestions(
    session: AsyncSession, document: GeneratedDocument, *, limit: int = 6
) -> list[str]:
    """Starter prompts. Free: built from what is already stored.

    Gaps become questions rather than edit requests, for the same reason as in
    `resume_chat`: "how should I handle X" can be answered honestly, "add X"
    for a stack the profile lacks cannot.
    """
    out: list[str] = []
    keywords = list((document.tailoring or {}).get("jd_keywords") or [])
    for keyword in keywords[:2]:
        out.append(f"Say more about my {keyword} work")

    match = await session.scalar(
        select(JobMatch).where(
            JobMatch.job_id == document.job_id,
            JobMatch.profile_version == document.profile_version,
        )
    )
    verdict = (match.llm_verdict or {}) if match is not None else {}
    gaps = verdict.get("must_have_gaps") or verdict.get("gaps") or []
    for gap in gaps[:2]:
        gap = str(gap).replace(" (partial)", "").strip()
        if gap:
            out.append(f"The posting asks for {gap} — how should the letter handle that?")

    out.extend(
        [
            "Make the opening paragraph less generic",
            "Shorten it — one paragraph too long",
            "Which paragraph is weakest for this job?",
        ]
    )
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))][:limit]
