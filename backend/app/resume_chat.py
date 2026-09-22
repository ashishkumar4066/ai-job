"""Stage 3 — refine a tailored résumé by chatting about it.

The tailoring pass (`tailor.py`) is one shot. What it cannot do is take a
follow-up: "lead with the WebSockets work", "the summary is too long", "why did
you drop the YOLO bullet?". This module is that follow-up, one LLM call per
message, on the document as it stands *now* — hand edits and earlier chat edits
included.

The same three guards as tailoring, not weaker ones
----------------------------------------------------
A chat is a much easier place to talk a model into something than a schema'd
one-shot pass: the user can simply ask for "Next.js" to be added. So nothing
here is allowed to be looser than `tailor.py`:

1. **Regions, not LaTeX.** The model sees the résumé as region ids and plain
   text and answers with decisions keyed by those ids. It cannot add a bullet
   or touch the preamble, because there is no slot for either.
2. **`factcheck.py` against the BASE résumé and the profile**, never against
   the current document. Checking against the current text would let a hand
   edit launder an invented metric into "the profile says so". A blocked
   change is kept in the proposal, marked blocked with its reason, so the user
   sees *why* the thing they asked for did not happen.
3. **A human applies every change.** A reply is a *proposal*. Nothing touches
   `tex` until `apply_proposal`, and the proposal records the hash of the text
   it was written against, so a suggestion made before a hand edit cannot be
   applied over it blindly.

Storage
-------
The conversation lives in `generated_documents.chat` as a JSON list. It
belongs to the document: regenerating the résumé starts a new one, because
every region id and every "before" text in the old proposals refers to a
résumé that no longer exists.

Cost
----
Each message carries the JD, the profile brief and every region, the same
payload as tailoring (~6-9k tokens). History is capped at the last
`_HISTORY_TURNS` messages and sent as short text, so a long conversation does
not grow the prompt without bound.
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

from app.config import Settings, get_settings
from app.documents import DocumentError, drop_cached, tex_hash
from app.factcheck import FactCorpus
from app.llm import LLMScreener, profile_brief
from app.models import GeneratedDocument, JobMatch, JobPosting
from app.profile import Profile, get_profile
from app.resume_tex import (
    BulletEdit,
    ResumeDocument,
    ResumeEdits,
    ResumeTemplateError,
)
from app.tailor import TAILOR_PROMPT_VERSION

log = logging.getLogger(__name__)

# Bump when the chat prompt changes meaning. Stored on each assistant turn so a
# conversation can be read back knowing which instructions produced it.
CHAT_PROMPT_VERSION: Final[str] = f"2026-09-21.1+{TAILOR_PROMPT_VERSION}"

# Messages of history sent with each turn. Older ones are still stored and
# shown; the model just does not re-read them.
_HISTORY_TURNS: Final[int] = 8
_MAX_MESSAGE_CHARS: Final[int] = 2_000


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
    "Write plain text. Mark emphasis as **bold** exactly as the input does. Never write "
    "LaTeX, backslashes or braces."
)

CHAT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    # Changes first, reply last: the reply describes edits already decided,
    # rather than promising edits the lists then fail to contain.
    "required": ["changes", "order", "reply"],
    "properties": {
        "changes": {
            "type": "array",
            "maxItems": 8,
            "description": (
                "Up to 8 edits to the résumé that do what the user asked. EMPTY when the user "
                "asked a question, asked for something the profile cannot support, or no "
                "edit is needed. Only change what the request is about."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "include", "rewritten", "reason"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": (
                            "Copy a region id from the RESUME list exactly ('summary', "
                            "'exp.asint.2', 'skills.back-end'). Never invent one."
                        ),
                    },
                    "include": {
                        "type": "boolean",
                        "description": (
                            "false to DROP this bullet from the résumé. Skills rows and the "
                            "summary are never dropped; always true for them."
                        ),
                    },
                    "rewritten": {
                        "type": "string",
                        "description": (
                            "The full new text of this region. EMPTY STRING when dropping it. "
                            "Keep every number, employer, product and technology exactly as the "
                            "résumé or profile states it: rephrase and reorder facts, never add "
                            "one, never invent a metric. A skills row stays a comma-separated "
                            "list and may only contain items it already has. Bullets max 45 "
                            "words; summary 45-70 words. " + _MARKER_RULE
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
            "maxItems": 20,
            "items": {"type": "string"},
            "description": (
                "Bullet ids in their new order, ONLY when the user asked to reorder or to lead "
                "with something. Reordering happens within each job only. EMPTY otherwise."
            ),
        },
        "reply": {
            "type": "string",
            "description": (
                "Your answer to the user, 1-4 sentences, plain text, addressed to them as 'you'. "
                "Say what the edits do. If they asked for something the profile does not "
                "support (a technology listed under gaps, a skill or metric the résumé never "
                "states), say so plainly and suggest they add it to profile.yaml if it is true "
                "— do not make the edit."
            ),
        },
    },
}

_SYSTEM_PROMPT: Final[str] = (
    "You are helping one candidate refine their résumé for one job posting, in a chat. "
    "You may only select, re-order and re-word facts the candidate's profile or résumé "
    "already states. You must never invent an employer, a date, a title, a technology or a "
    "metric, even when the user asks you to — a résumé that overstates is a lie the candidate "
    "has to defend in an interview. Refuse those requests in the reply and explain why. "
    "Every edit you propose is shown to the user as a suggestion they accept or reject. "
    "Follow each field's description exactly."
)


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


def _regions_block(document: ResumeDocument) -> str:
    lines: list[str] = []
    current = ""
    for region in document.regions:
        if region.label != current:
            current = region.label
            lines.append(f"\n[{current}]")
        lines.append(f"{region.id}: {region.text}")
    return "\n".join(lines).strip()


def _history_line(message: dict[str, Any]) -> str:
    """A past turn, compressed to what the model needs to follow the thread."""
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
    document: ResumeDocument,
    history: list[dict[str, Any]],
    message: str,
    max_jd_chars: int,
) -> list[dict[str, str]]:
    jd = " ".join((job.description_text or "").split())[:max_jd_chars] or "(no description)"
    past = "\n".join(_history_line(m) for m in history[-_HISTORY_TURNS:]) or "(none)"
    user = (
        f"POSTING\nTitle: {job.title}\nCompany: {job.company}\n\n{jd}\n\n"
        f"CANDIDATE\n{profile_text}\n\n"
        f"RESUME AS IT STANDS NOW (id: text)\n{_regions_block(document)}\n\n"
        f"CONVERSATION SO FAR\n{past}\n\n"
        f"USER'S NEW MESSAGE\n{message}"
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------


def _parse(tex: str) -> ResumeDocument:
    try:
        return ResumeDocument.parse(tex)
    except ResumeTemplateError as exc:
        raise DocumentError(
            "the résumé's structure no longer parses (a section or list was edited away), "
            "so chat cannot address its bullets — revert or fix the LaTeX first"
        ) from exc


def build_proposal(
    answer: ChatAnswer, current: ResumeDocument, corpus: FactCorpus
) -> dict[str, Any]:
    """Turn the model's answer into a reviewable, fact-checked proposal.

    Pure — no database, no network — so the guarantee is testable with a
    canned answer, exactly like `tailor.apply_tailoring`.
    """
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for change in answer.changes:
        region = current.region(change.id)
        if region is None or change.id in seen:
            log.info("resume_chat.unknown_region", extra={"id": change.id})
            continue
        seen.add(change.id)

        drop = not change.include and region.kind == "bullet"
        after = "" if drop else " ".join(change.rewritten.split())
        if not drop and (not after or after == region.text):
            continue  # Says "change" but changes nothing.

        issues = [] if drop else corpus.check(change.id, after)
        blocked = [i.message for i in issues if i.severity == "blocking"]
        changes.append(
            {
                "region_id": change.id,
                "label": region.label,
                "kind": region.kind,
                "action": "drop" if drop else "rewrite",
                "before": region.text,
                "after": after,
                "reason": change.reason,
                "status": "blocked" if blocked else "proposed",
                "blocked": blocked,
                "warnings": [i.message for i in issues if i.severity == "warning"],
            }
        )

    order = [i for i in answer.order if (r := current.region(i)) is not None and r.kind == "bullet"]
    # An order identical to the current one is not a proposal.
    if order and order == [r.id for r in current.regions if r.id in set(order)]:
        order = []

    return {
        "base_hash": tex_hash(current.tex),
        "status": "pending" if (changes or order) else "none",
        "changes": changes,
        "order": order,
        "order_preview": [
            {"region_id": i, "text": current.region(i).text[:90]} for i in order  # type: ignore[union-attr]
        ],
    }


def apply_to_tex(tex: str, proposal: dict[str, Any], accept: list[str] | None) -> str:
    """Render the accepted part of a proposal onto `tex`. Pure.

    `accept` names the region ids to apply (plus `"order"` for the reorder);
    None means every change that was not blocked.
    """
    current = _parse(tex)
    wanted = {c["region_id"] for c in proposal["changes"] if c["status"] == "proposed"}
    if proposal.get("order"):
        wanted.add("order")
    if accept is not None:
        wanted &= set(accept)
    if not wanted:
        raise DocumentError("nothing to apply — every change was blocked or deselected")

    if tex_hash(tex) != proposal["base_hash"]:
        # The résumé moved since this was suggested. Still safe to apply if
        # every region we touch still says what it said then.
        for change in proposal["changes"]:
            if change["region_id"] not in wanted:
                continue
            region = current.region(change["region_id"])
            if region is None or region.text != change["before"]:
                raise DocumentError(
                    f"{change['label']} changed since this was suggested — ask again"
                )
        if "order" in wanted and any(current.region(i) is None for i in proposal["order"]):
            raise DocumentError("the bullets moved since this was suggested — ask again")

    summary: str | None = None
    bullets: list[BulletEdit] = []
    for change in proposal["changes"]:
        if change["region_id"] not in wanted:
            continue
        if change["region_id"] == "summary":
            summary = change["after"]
        elif change["action"] == "drop":
            bullets.append(BulletEdit(id=change["region_id"], include=False))
        else:
            bullets.append(BulletEdit(id=change["region_id"], text=change["after"]))

    edits = ResumeEdits(
        summary=summary,
        bullets=bullets,
        order=list(proposal["order"]) if "order" in wanted else [],
    )
    return current.render(edits)


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _message(role: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex[:12], "role": role, "text": text, "created_at": _now(), **extra}


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

    current = _parse(document.tex)
    history = list(document.chat or [])
    messages = build_messages(
        job=job,
        profile_text=profile_brief(profile),
        document=current,
        history=history,
        message=text,
        max_jd_chars=settings.llm_max_jd_chars,
    )

    async with LLMScreener(settings) as client:
        answer: ChatAnswer = await client.complete(
            messages,
            schema=CHAT_SCHEMA,
            name="resume_chat",
            parse=lambda content: ChatAnswer.model_validate(json.loads(content)),
            max_completion_tokens=settings.llm_tailor_completion_tokens,
        )
        tokens = client.tokens_spent

    corpus = FactCorpus.build(profile, ResumeDocument.load().tex)
    proposal = build_proposal(answer, current, corpus)
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
        "resume_chat.turn",
        extra={
            "doc_id": document.id,
            "tokens": tokens,
            "changes": len(proposal["changes"]),
            "blocked": sum(1 for c in proposal["changes"] if c["status"] == "blocked"),
        },
    )
    return history


def _find(document: GeneratedDocument, message_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    """Write the accepted edits into the document. Free — no LLM call."""
    history, message = _find(document, message_id)
    proposal = dict(message["proposal"])
    if proposal.get("status") != "pending":
        raise DocumentError(f"this suggestion is already {proposal.get('status')}")

    document.tex = apply_to_tex(document.tex, proposal, accept)
    applied = (
        {c["region_id"] for c in proposal["changes"] if c["status"] == "proposed"}
        | ({"order"} if proposal.get("order") else set())
    )
    proposal["applied"] = sorted(applied if accept is None else applied & set(accept))
    proposal["status"] = "applied"
    message["proposal"] = proposal

    document.chat = history
    # Chat edits sit outside the stored tailoring, so Revert (which replays
    # it) undoes them — the same contract as any other hand edit.
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

    Gaps become questions, not edit requests — "how should I handle X" can be
    answered honestly; "add X" for a stack the profile lacks cannot.
    """
    out: list[str] = []
    keywords = list((document.tailoring or {}).get("jd_keywords") or [])
    for keyword in keywords[:2]:
        out.append(f"Put more weight on my {keyword} work")

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
            out.append(f"The posting asks for {gap} — how should my résumé handle that?")

    out.extend(
        [
            "Tighten the summary for this role",
            "Which bullet is weakest for this job?",
        ]
    )
    try:
        first_job = next(
            r.label.split(" — ")[0]
            for r in ResumeDocument.parse(document.tex).regions
            if r.group.startswith("exp.")
        )
        out.append(f"Reorder my {first_job} bullets for this posting")
    except (ResumeTemplateError, StopIteration):
        pass
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))][:limit]
