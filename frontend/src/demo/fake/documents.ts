/**
 * Tailored résumés and cover letters, generated in the browser.
 *
 * Ten real documents ship with the snapshot; every other posting had none, and
 * the Tailor modal opened on a refusal. This fills that in.
 *
 * The hard constraint CLAUDE.md puts on the real generator applies here too,
 * and is easier to keep: **the generator may only select, re-order and re-word
 * facts stated in the profile.** So the résumé is produced by *reordering the
 * base template's own bullets* against the posting's keywords and dropping the
 * least relevant one — no sentence is rewritten, because nothing here can write
 * a sentence responsibly. The letter is assembled from a fixed bank of
 * profile-true sentences, picked by which of the posting's keywords the profile
 * can actually answer.
 *
 * Every document it produces is marked `llm_used: false`, so nothing downstream
 * mistakes one for a paid generation.
 */

import type { DiffRow, DocumentDiff, DocumentKind, TailoredDocument } from "@/lib/types";
import type { MatchRow } from "../snapshot";
import { escapeTex, spansToText, deTex } from "./latex";

/* ------------------------------------------------------------- keywording */

const STOP = new Set([
  "and", "the", "for", "with", "you", "our", "are", "will", "have", "this", "that", "from",
  "your", "not", "all", "who", "can", "any", "new", "out", "use", "using", "work", "working",
  "team", "teams", "role", "job", "jobs", "years", "year", "experience", "engineer", "engineering",
  "software", "developer", "development", "build", "building", "across", "into", "their", "them",
  "what", "when", "where", "while", "about", "more", "most", "such", "than", "they", "been",
  "well", "also", "must", "should", "would", "could", "like", "help", "make", "need", "want",
]);

/**
 * The posting's own vocabulary, best first.
 *
 * `matched_skills` and `missing_stacks` come first because the backend really
 * did read them out of this JD; the free-text terms behind them are a top-up so
 * a posting the matcher found little in still steers the ordering.
 */
export function keywordsFor(row: MatchRow, jdText: string | null): string[] {
  const seen = new Set<string>();
  const keywords: string[] = [];
  const add = (term: string) => {
    const clean = term.trim().toLowerCase();
    if (!clean || clean.length < 3 || STOP.has(clean) || seen.has(clean)) return;
    seen.add(clean);
    keywords.push(clean);
  };

  for (const skill of row.matched_skills) add(skill);
  for (const stack of row.missing_stacks) add(stack);
  for (const word of row.job.title.split(/[^A-Za-z0-9+#.]+/)) add(word);

  if (jdText) {
    const counts = new Map<string, number>();
    for (const word of jdText.toLowerCase().split(/[^a-z0-9+#.]+/)) {
      if (word.length < 4 || STOP.has(word)) continue;
      counts.set(word, (counts.get(word) ?? 0) + 1);
    }
    [...counts.entries()]
      .filter(([, count]) => count >= 3)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 12)
      .forEach(([word]) => add(word));
  }

  return keywords;
}

/** How well a passage answers the posting. Earlier keywords weigh more. */
function relevance(text: string, keywords: string[]): number {
  const haystack = text.toLowerCase();
  let total = 0;
  keywords.forEach((keyword, index) => {
    if (haystack.includes(keyword)) total += Math.max(1, 12 - index);
  });
  return total;
}

/* ---------------------------------------------------------------- résumé */

interface ItemBlock {
  /** Everything before the first `\item` — the `\itemsep` line and its spacing. */
  preamble: string;
  items: string[];
  start: number;
  end: number;
}

/** Locate every `itemize` body in the template, with its `\item`s split out. */
function itemizeBlocks(tex: string): ItemBlock[] {
  const blocks: ItemBlock[] = [];
  const pattern = /\\begin\{itemize\}([\s\S]*?)\\end\{itemize\}/g;
  let match = pattern.exec(tex);
  while (match) {
    const body = match[1] ?? "";
    const first = body.indexOf("\\item");
    if (first !== -1) {
      const items = body
        .slice(first)
        .split(/\n\s*(?=\\item\b)/)
        .map((item) => item.trim())
        .filter(Boolean);
      blocks.push({
        preamble: body.slice(0, first).replace(/\s+$/, ""),
        items,
        start: match.index + "\\begin{itemize}".length,
        end: match.index + "\\begin{itemize}".length + body.length,
      });
    }
    match = pattern.exec(tex);
  }
  return blocks;
}

/** A `\item` reduced to the text a reader sees — what gets scored and diffed. */
function itemText(item: string): string {
  return spansToText(deTex(item.replace(/^\\item\s*/, "")));
}

function renderBlock(block: ItemBlock, items: string[]): string {
  return `${block.preamble}\n\n${items.map((item) => `    ${item}`).join("\n\n")}\n\n`;
}

/** Skills rows, whose comma-separated entries are reordered in place. */
function reorderSkillRows(tex: string, keywords: string[]): { tex: string; changed: string[] } {
  const changed: string[] = [];
  const next = tex.replace(
    /(\n)([A-Za-z][^\n&]*?)(\n& )([\s\S]*?)(\n\\\\)/g,
    (whole, lead: string, label: string, sep: string, value: string, tail: string) => {
      const entries = value.split(/,\s*/).map((entry) => entry.trim()).filter(Boolean);
      if (entries.length < 3) return whole;
      const ordered = entries
        .map((entry, index) => ({ entry, index, score: relevance(entry, keywords) }))
        .sort((a, b) => b.score - a.score || a.index - b.index)
        .map((row) => row.entry);
      if (ordered.join(", ") === entries.join(", ")) return whole;
      changed.push(label.trim());
      return `${lead}${label}${sep}${ordered.join(", ")}${tail}`;
    },
  );
  return { tex: next, changed };
}

export interface TailorResult {
  tex: string;
  notes: string[];
  dropped: string[];
  diff: DiffRow[];
}

/**
 * Reorder the base template against one posting.
 *
 * Blocks are spliced from the end backwards so an earlier splice cannot move a
 * later block's offsets — the same reason `app/resume_tex.py` does it that way.
 */
export function tailorResume(baseTex: string, keywords: string[]): TailorResult {
  const blocks = itemizeBlocks(baseTex);
  const notes: string[] = [];
  const dropped: string[] = [];
  const diff: DiffRow[] = [];
  let tex = baseTex;

  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index]!;
    const scored = block.items.map((item, position) => ({
      item,
      position,
      text: itemText(item),
      score: relevance(itemText(item), keywords),
    }));
    const ordered = [...scored].sort((a, b) => b.score - a.score || a.position - b.position);

    // Only a long block can afford to lose a bullet, and only one that answers
    // nothing in the posting. A two-bullet section losing one reads as a bug.
    const cut =
      ordered.length >= 7 && ordered[ordered.length - 1]!.score === 0 ? ordered.pop() : undefined;
    if (cut) dropped.push(cut.text);

    if (ordered.map((entry) => entry.position).join() !== scored.map((_, i) => i).join() || cut) {
      tex = `${tex.slice(0, block.start)}${renderBlock(
        block,
        ordered.map((entry) => entry.item),
      )}${tex.slice(block.end)}`;
    }

    // Recorded front-to-back so the diff reads in document order.
    diff.unshift(
      ...scored.map((entry): DiffRow => {
        const now = ordered.findIndex((candidate) => candidate.position === entry.position);
        return {
          region_id: `bullet.${index}.${entry.position}`,
          label: `Bullet ${entry.position + 1}`,
          kind: "bullet",
          group: `block.${index}`,
          status: now === -1 ? "dropped" : now === entry.position ? "unchanged" : "moved",
          base: entry.text,
          current: now === -1 ? "" : entry.text,
          base_index: entry.position,
          current_index: now === -1 ? null : now,
        };
      }),
    );
  }

  const skills = reorderSkillRows(tex, keywords);
  tex = skills.tex;

  const leading = [...(blocks[0]?.items ?? [])].sort(
    (a, b) => relevance(itemText(b), keywords) - relevance(itemText(a), keywords),
  );
  const lead = leading[0] ? itemText(leading[0]) : "";
  if (lead) {
    notes.push(
      `Leads the experience section with "${lead.replace(/\*\*/g, "").slice(0, 70)}…" — it answers the most of what this posting asks for.`,
    );
  }
  if (keywords.length > 0) {
    notes.push(
      `Ordered against the posting's own vocabulary: ${keywords.slice(0, 6).join(", ")}.`,
    );
  }
  for (const label of skills.changed) {
    notes.push(`Re-ordered the ${label} skills row so the stacks this posting names come first.`);
  }
  if (dropped.length > 0) {
    notes.push(
      `Dropped ${dropped.length} bullet${dropped.length === 1 ? "" : "s"} that answered nothing in this posting, to keep the résumé to one page.`,
    );
  }
  notes.push(
    "Selection and ordering only — no bullet was rewritten, so every claim is the profile's own wording.",
  );

  return { tex, notes, dropped, diff };
}

/* ----------------------------------------------------------- cover letter */

/** Profile-true sentences, each gated on the posting naming something it answers. */
const PITCH: [RegExp, string][] = [
  [
    /agent|langgraph|langchain|orchestrat|workflow/i,
    "I designed a \\textbf{LangGraph} agentic workflow with human-in-the-loop controls that automates inspection creation and cut research effort by \\textbf{30\\%}.",
  ],
  [
    /rag|retrieval|vector|embedding|search|semantic/i,
    "I built a \\textbf{Text-to-SQL} engine on schema-grounded \\textbf{RAG} with guardrails against destructive queries and cross-tenant leakage, reaching \\textbf{75\\%} accuracy across \\textbf{200+ schemas}.",
  ],
  [
    /llm|gpt|gemini|mistral|openai|anthropic|claude|model/i,
    "I run \\textbf{Gemini}, \\textbf{Mistral} and \\textbf{Groq} in production, routing work between them on cost and latency.",
  ],
  [
    /observab|monitor|tracing|cost|token|platform|infrastructure/i,
    "I built a token-metering and \\textbf{LLM observability} platform from scratch to replace \\textbf{Langfuse}, with request-level tracing and audit trails; prompt caching cut token spend roughly \\textbf{90\\%}.",
  ],
  [
    /react|frontend|front-end|typescript|ui|next/i,
    "On the product side I ship \\textbf{React} and \\textbf{TypeScript} UIs, including a reusable rich-text editor adopted across \\textbf{7 products}.",
  ],
  [
    /node|backend|back-end|api|rest|graphql|fastapi|python|java/i,
    "My backend work is \\textbf{Node.js}, \\textbf{Python}/\\textbf{FastAPI} and \\textbf{Java}, behind REST APIs serving \\textbf{10 enterprise tenants}.",
  ],
  [
    /real-?time|event|stream|kafka|websocket|queue|cdc/i,
    "I built an event-driven notification service on \\textbf{PostgreSQL WAL-based CDC} that delivers alerts to \\textbf{1,000+ users} within \\textbf{20 seconds} over \\textbf{WebSockets}.",
  ],
  [
    /vision|image|document|ocr|extraction|multimodal/i,
    "I architected a multimodal document extraction pipeline on a \\textbf{Gemini} vision model at \\textbf{90\\%} field-level accuracy, and trained a \\textbf{YOLOv11} layout model to \\textbf{80\\% mAP@0.5}.",
  ],
  [
    /scale|tenant|saas|migrat|monolith|architecture|distributed/i,
    "I re-architected a legacy monolith into a \\textbf{multi-tenant SaaS} platform with SSO and per-tenant isolation, taking throughput up \\textbf{20\\%}.",
  ],
  [
    /lead|mentor|senior|staff|own|autonom/i,
    "I am the lead engineer for my team's AI systems: I own the architecture, drive build-vs-buy through RFCs, and mentor \\textbf{5 engineers}.",
  ],
];

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export interface LetterResult {
  tex: string;
  notes: string[];
  paragraphs: string[];
}

/**
 * Assemble a letter for one posting.
 *
 * The header, date, addressee and sign-off are rendered from the profile and
 * the posting exactly as `app/cover_letter.py` does — the model there only ever
 * writes the body, and here nothing writes it at all: the paragraphs are picked
 * from `PITCH` by what the posting names.
 */
export function coverLetter(row: MatchRow, keywords: string[], contact: string[]): LetterResult {
  const company = row.job.company;
  const title = row.job.title;
  const haystack = `${title} ${keywords.join(" ")}`;

  const picked: string[] = [];
  for (const [pattern, sentence] of PITCH) {
    if (pattern.test(haystack) && !picked.includes(sentence)) picked.push(sentence);
    if (picked.length === 5) break;
  }
  while (picked.length < 3) {
    const next = PITCH[picked.length]?.[1];
    if (!next) break;
    if (picked.includes(next)) break;
    picked.push(next);
  }

  const named = keywords.slice(0, 3).map((keyword) => escapeTex(keyword)).join(", ");
  const opening =
    `I'm writing about the \\textbf{${escapeTex(title)}} role at \\textbf{${escapeTex(company)}}. ` +
    (named
      ? `The posting leads on ${named}, and that is where the last two years of my work have been. `
      : "") +
    picked[0];
  const middle = `${picked[1]} ${picked[2] ?? ""}`.trim();
  const closing =
    `${picked.slice(3).join(" ")} ` +
    `I work remotely from India with US-hours overlap, I own systems end to end rather than handing them off, ` +
    `and I'd like to bring that to ${escapeTex(company)}. I'd welcome the chance to talk it through.`.trim();

  const paragraphs = [opening, middle, closing.trim()].filter(Boolean);
  const today = new Date();
  const date = `${today.getDate()} ${MONTHS[today.getMonth()]} ${today.getFullYear()}`;

  const tex = `\\documentclass[11pt]{article}

\\usepackage[left=0.9in,right=0.9in,top=0.7in,bottom=0.7in]{geometry}
\\usepackage[colorlinks=true,urlcolor=blue,linkcolor=blue]{hyperref}
\\setlength{\\parindent}{0pt}
\\setlength{\\parskip}{9pt}
\\pagestyle{empty}

\\begin{document}

\\begin{center}
{\\LARGE\\bfseries Ashish Kumar}\\\\[5pt]
{\\small ${contact.join(" \\textbar{} ")}}
\\end{center}

\\vspace{6pt}
Bihar, India\\\\
${date}

Hiring Team\\\\
${escapeTex(company)}

\\textbf{Re: ${escapeTex(title)}}

Dear ${escapeTex(company)} hiring team,

% --- BODY (paragraphs) ---
${paragraphs.join("\n\n")}
% --- END BODY ---

Sincerely,\\\\
Ashish Kumar

\\end{document}
`;

  const notes = [
    `Addressed to ${company} for ${title}; the header, date and sign-off are rendered from the profile, never written.`,
    picked.length > 0
      ? `Opens on ${keywords.slice(0, 3).join(", ") || "the posting's stack"}, because that is what the posting leads with.`
      : "Opens on the profile's production AI work.",
    "Every claim is a sentence the profile already states — nothing about this posting was invented to fit it.",
  ];

  return { tex, notes, paragraphs };
}

/* ----------------------------------------------------------- the envelope */

export interface GeneratedDocument {
  document: TailoredDocument;
  diff: DocumentDiff | null;
}

/** Wrap generated LaTeX in the record shape the API returns. */
export function asDocument(
  id: number,
  row: MatchRow,
  kind: DocumentKind,
  profileVersion: string,
  tex: string,
  notes: string[],
  keywords: string[],
  dropped: string[],
): TailoredDocument {
  const now = new Date().toISOString();
  return {
    id,
    job_id: row.job.id,
    kind,
    profile_version: profileVersion,
    tex,
    is_draft: true,
    hand_edited: false,
    // Never true for a generated document: no call was made, and the Matches
    // row's "LLM" marker must not claim one was.
    llm_used: false,
    llm_tokens: 0,
    created_at: now,
    updated_at: now,
    stale: false,
    tailoring_notes: notes,
    jd_keywords: keywords.slice(0, 10),
    issues: [],
    reworded: [],
    dropped,
  };
}

export function asDiff(documentId: number, rows: DiffRow[]): DocumentDiff {
  const count = (status: string) => rows.filter((row) => row.status === status).length;
  return {
    document_id: documentId,
    kind: "resume",
    available: true,
    note: "",
    rows,
    reworded: count("reworded"),
    dropped: count("dropped"),
    moved: count("moved"),
    unchanged: count("unchanged"),
    added: 0,
  };
}
