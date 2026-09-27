/**
 * Document chat, without a model behind it.
 *
 * The real panel sends the document's regions and the last eight messages to
 * the provider and fact-checks whatever comes back. Two properties of that are
 * worth keeping even in a demo, because they are the point of the feature:
 *
 *   **A reply is a proposal, never an edit.** Nothing here writes to the
 *   document; the visitor applies or dismisses, exactly as they would.
 *
 *   **A claim the profile cannot support is refused, visibly.** Ask for AWS or
 *   Kubernetes and the reply says no and names why, because those really are in
 *   `profile.yaml`'s `gaps:` — the same denylist `app/factcheck.py` blocks on.
 *   The refusal is the feature, so it would be the wrong thing to fake away.
 *
 * What it cannot do is write prose. So every change it proposes is a
 * re-ordering, a drop, or a trim of a sentence that is already there.
 */

import type { ChatChange, ChatMessage, ChatProposal, ResumeChat, TailoredDocument } from "@/lib/types";
import { deTex, spansToText } from "./latex";

const STOP = new Set([
  "the", "and", "for", "with", "put", "more", "less", "make", "add", "my", "me", "on", "to",
  "in", "it", "of", "a", "an", "is", "are", "be", "can", "you", "your", "please", "up", "at",
  "work", "about", "some", "that", "this", "want", "would", "like", "lead", "leading",
]);

/** A slice of the document the chat can address. */
export interface Region {
  id: string;
  label: string;
  kind: string;
  group: string;
  /** The exact LaTeX, so applying a change is a splice rather than a re-render. */
  raw: string;
  text: string;
}

const BODY_START = "% --- BODY (paragraphs) ---";
const BODY_END = "% --- END BODY ---";

function itemizeSpans(tex: string): { start: number; end: number; items: string[] }[] {
  const spans: { start: number; end: number; items: string[] }[] = [];
  const pattern = /\\begin\{itemize\}([\s\S]*?)\\end\{itemize\}/g;
  let match = pattern.exec(tex);
  while (match) {
    const body = match[1] ?? "";
    const first = body.indexOf("\\item");
    if (first !== -1) {
      spans.push({
        start: match.index + "\\begin{itemize}".length + first,
        end: match.index + "\\begin{itemize}".length + body.length,
        items: body
          .slice(first)
          .split(/\n\s*(?=\\item\b)/)
          .map((item) => item.trim())
          .filter(Boolean),
      });
    }
    match = pattern.exec(tex);
  }
  return spans;
}

function bodySpan(tex: string): { start: number; end: number; paragraphs: string[] } | null {
  const start = tex.indexOf(BODY_START);
  const end = tex.indexOf(BODY_END);
  if (start === -1 || end === -1) return null;
  const inner = tex.slice(start + BODY_START.length, end);
  return {
    start: start + BODY_START.length,
    end,
    paragraphs: inner.split(/\n\s*\n+/).map((part) => part.trim()).filter(Boolean),
  };
}

/** Everything the chat may touch, in document order. */
export function regionsOf(document: TailoredDocument): Region[] {
  if (document.kind === "cover_letter") {
    const body = bodySpan(document.tex);
    if (!body) return [];
    return body.paragraphs.map((raw, index) => ({
      id: `para.${index}`,
      label: `Paragraph ${index + 1}`,
      kind: "paragraph",
      group: "body",
      raw,
      text: spansToText(deTex(raw)),
    }));
  }

  const regions: Region[] = [];
  itemizeSpans(document.tex).forEach((span, blockIndex) => {
    span.items.forEach((raw, itemIndex) => {
      regions.push({
        id: `bullet.${blockIndex}.${itemIndex}`,
        label: `Bullet ${itemIndex + 1}`,
        kind: "bullet",
        group: `block.${blockIndex}`,
        raw,
        text: spansToText(deTex(raw.replace(/^\\item\s*/, ""))),
      });
    });
  });
  return regions;
}

/* ------------------------------------------------------------- the reply */

/** Words that express *how* to edit, not *what* to foreground. */
const INTENT = new Set([
  "short", "shorter", "shortest", "trim", "tighten", "tighter", "page", "cut", "concise",
  "brief", "briefer", "one", "back", "down", "remove", "drop", "reorder", "move", "weight",
  "focus", "emphasise", "emphasize", "highlight", "foreground", "first", "top",
]);

function terms(message: string): string[] {
  return [
    ...new Set(
      message
        .toLowerCase()
        .split(/[^a-z0-9+#.]+/)
        .filter((word) => word.length >= 3 && !STOP.has(word) && !INTENT.has(word)),
    ),
  ];
}

function hits(text: string, focus: string[]): number {
  const haystack = text.toLowerCase();
  return focus.filter((term) => haystack.includes(term)).length;
}

function messageId(): string {
  return Math.random().toString(16).slice(2, 14).padEnd(12, "0");
}

/** Sentence split that keeps the terminator, so a trim leaves valid prose. */
function sentences(text: string): string[] {
  return text.split(/(?<=[.!?])\s+/).filter(Boolean);
}

export interface ReplyInput {
  document: TailoredDocument;
  message: string;
  /** `profile.yaml`'s `gaps:` — claiming one of these is blocking. */
  gaps: string[];
}

/**
 * Compose an assistant turn for one user message.
 *
 * The reply text describes exactly what the proposal contains, because a chat
 * that says it rewrote four bullets and then shows one reorder is worse than no
 * chat at all.
 */
export function reply({ document, message, gaps }: ReplyInput): ChatMessage {
  const focus = terms(message);
  const regions = regionsOf(document);
  const changes: ChatChange[] = [];
  const said: string[] = [];

  const claimed = gaps.filter((gap) => focus.some((term) => gap.includes(term) || term === gap));
  if (claimed.length > 0) {
    changes.push({
      region_id: regions[0]?.id ?? "summary",
      label: regions[0]?.label ?? "Summary",
      kind: regions[0]?.kind ?? "summary",
      action: "add",
      before: regions[0]?.text ?? "",
      after: "",
      reason: `Would claim ${claimed.join(", ")}, which the profile does not evidence.`,
      status: "blocked",
      blocked: claimed.map(
        (gap) => `"${gap}" is listed under gaps: in profile.yaml — the fact check blocks it.`,
      ),
      warnings: [],
    });
    said.push(
      `I can't put ${claimed.join(" or ")} on this — ${
        claimed.length === 1 ? "it is" : "they are"
      } in your profile's \u201cgaps\u201d list, so the fact check blocks the claim rather than letting it through.`,
    );
  }

  // Re-ordering: the one change that is always safe, because it moves text the
  // document already carries.
  const scored = regions.map((region, index) => ({
    region,
    index,
    score: hits(region.text, focus),
  }));
  const wantsTrim = /\b(short|shorter|trim|tighten|one page|1 page|cut|concise)\b/i.test(message);

  const order: string[] = [];
  const orderPreview: { region_id: string; text: string }[] = [];
  const groups = [...new Set(regions.map((region) => region.group))];
  // Counted as *promotions*, not as positions that shifted: moving one bullet
  // from the bottom to the top changes nine indices, and "I moved 9 bullets"
  // is a much bigger claim than what the preview then shows.
  let promoted = 0;
  for (const group of groups) {
    const inGroup = scored.filter((entry) => entry.region.group === group);
    const ordered = [...inGroup].sort((a, b) => b.score - a.score || a.index - b.index);
    ordered.forEach((entry, position) => {
      if (inGroup.indexOf(entry) > position) promoted += 1;
      order.push(entry.region.id);
      orderPreview.push({ region_id: entry.region.id, text: entry.region.text });
    });
  }
  if (promoted === 0) {
    order.length = 0;
    orderPreview.length = 0;
  } else {
    said.push(
      `I moved ${promoted} ${document.kind === "cover_letter" ? "paragraph" : "bullet"}${
        promoted === 1 ? "" : "s"
      } up so the ones answering ${focus.slice(0, 3).join(", ") || "your note"} come first.`,
    );
  }

  if (wantsTrim) {
    if (document.kind === "cover_letter") {
      // Trim the sentence that answers the request least — dropping text is a
      // selection, which is inside what this stand-in may do.
      const target = [...scored].sort((a, b) => a.score - b.score)[0];
      if (target) {
        const parts = sentences(target.region.text);
        if (parts.length > 1) {
          const weakest = parts
            .map((sentence, index) => ({ sentence, index, score: hits(sentence, focus) }))
            .sort((a, b) => a.score - b.score || b.index - a.index)[0]!;
          changes.push({
            region_id: target.region.id,
            label: target.region.label,
            kind: target.region.kind,
            action: "rewrite",
            before: target.region.text,
            after: parts.filter((_, index) => index !== weakest.index).join(" "),
            reason: "Drops the sentence furthest from what you asked for; nothing else is touched.",
            status: "proposed",
            blocked: [],
            warnings: [],
          });
          said.push("I cut the one sentence that was doing the least work; nothing else is touched.");
        }
      }
    } else {
      for (const group of groups) {
        const inGroup = scored.filter((entry) => entry.region.group === group);
        if (inGroup.length < 6) continue;
        const weakest = [...inGroup].sort((a, b) => a.score - b.score || b.index - a.index)[0];
        if (!weakest || weakest.score > 0) continue;
        changes.push({
          region_id: weakest.region.id,
          label: weakest.region.label,
          kind: weakest.region.kind,
          action: "drop",
          before: weakest.region.text,
          after: "",
          reason: "Answers nothing this posting asks for, and the section can spare it.",
          status: "proposed",
          blocked: [],
          warnings: [],
        });
      }
      const dropped = changes.filter((change) => change.action === "drop").length;
      if (dropped > 0) {
        said.push(
          `I dropped ${dropped} bullet${dropped === 1 ? "" : "s"} that answered nothing in this posting.`,
        );
      }
    }
  }

  if (changes.length === 0 && order.length === 0) {
    said.push(
      "Nothing needed moving for that — the document already leads with the material closest to your note. Try naming a stack you want foregrounded, or ask me to tighten it.",
    );
  }

  const proposal: ChatProposal | null =
    changes.length > 0 || order.length > 0
      ? { status: "pending", changes, order, order_preview: orderPreview, applied: [] }
      : null;

  return {
    id: messageId(),
    role: "assistant",
    text: said.join(" "),
    created_at: new Date().toISOString(),
    tokens: 3200 + ((regions.length * 137) % 1400),
    proposal,
  };
}

/* ------------------------------------------------------------- applying */

/**
 * Apply a proposal to the LaTeX.
 *
 * Drops and re-ordering are resolved per group in one rewrite, because applying
 * them one at a time would renumber the ids between steps — the same trap the
 * region-level diff exists to avoid.
 */
export function applyProposal(
  document: TailoredDocument,
  proposal: ChatProposal,
  accepted: Set<string>,
): string {
  const regions = regionsOf(document);
  const byId = new Map(regions.map((region) => [region.id, region]));
  const dropped = new Set(
    proposal.changes
      .filter((change) => change.action === "drop" && accepted.has(change.region_id))
      .map((change) => change.region_id),
  );
  const rewritten = new Map(
    proposal.changes
      .filter((change) => change.action === "rewrite" && accepted.has(change.region_id))
      .map((change) => [change.region_id, change.after]),
  );

  const groups = [...new Set(regions.map((region) => region.group))];
  const finalOrder = new Map<string, string[]>();
  for (const group of groups) {
    const current = regions.filter((region) => region.group === group).map((region) => region.id);
    const wanted = proposal.order.filter((id) => byId.get(id)?.group === group);
    // A partial order would delete whatever it forgot to mention, so it is only
    // honoured when it accounts for every region in its group.
    const ordered = wanted.length === current.length ? wanted : current;
    finalOrder.set(
      group,
      ordered.filter((id) => !dropped.has(id)),
    );
  }

  if (document.kind === "cover_letter") {
    const body = bodySpan(document.tex);
    if (!body) return document.tex;
    const paragraphs = (finalOrder.get("body") ?? []).map((id) => {
      const replacement = rewritten.get(id);
      if (replacement !== undefined) return replacement.replace(/\*\*(.+?)\*\*/g, "\\textbf{$1}");
      return byId.get(id)?.raw ?? "";
    });
    return `${document.tex.slice(0, body.start)}\n${paragraphs.join("\n\n")}\n${document.tex.slice(body.end)}`;
  }

  // Splice from the last block backwards so earlier offsets stay valid.
  let tex = document.tex;
  const spans = itemizeSpans(tex);
  for (let index = spans.length - 1; index >= 0; index -= 1) {
    const ids = finalOrder.get(`block.${index}`);
    if (!ids) continue;
    const items = ids.map((id) => {
      const replacement = rewritten.get(id);
      if (replacement !== undefined) {
        return `\\item ${replacement.replace(/\*\*(.+?)\*\*/g, "\\textbf{$1}")}`;
      }
      return byId.get(id)?.raw ?? "";
    });
    const span = spans[index]!;
    tex = `${tex.slice(0, span.start)}${items.join("\n\n    ")}\n\n${tex.slice(span.end)}`;
  }
  return tex;
}

/* ---------------------------------------------------------- the transcript */

/** Starter prompts, from the posting's own keywords and the read's gaps. */
export function suggestionsFor(document: TailoredDocument, gaps: string[]): string[] {
  const starters: string[] = [];
  for (const keyword of document.jd_keywords.slice(0, 3)) {
    starters.push(`Put more weight on my ${keyword} work`);
  }
  if (document.kind === "cover_letter") starters.push("Make the middle paragraph tighter");
  else starters.push("Trim it back to one page");
  if (gaps.length > 0) starters.push(`Can you add ${gaps[0]} to my skills?`);
  return starters.slice(0, 5);
}

export function emptyChat(documentId: number, suggestions: string[]): ResumeChat {
  return { document_id: documentId, messages: [], suggestions, tokens: 0 };
}

export function userMessage(text: string): ChatMessage {
  return {
    id: messageId(),
    role: "user",
    text,
    created_at: new Date().toISOString(),
    tokens: 0,
    proposal: null,
  };
}
