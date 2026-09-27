/**
 * Just enough LaTeX handling for the demo's generated documents.
 *
 * Two jobs, and they are deliberately separate:
 *
 *   `deTex` turns a fragment into styled spans, so the hand-written PDF writer
 *   can lay out text that was authored as LaTeX.
 *
 *   `texToBlocks` walks a whole document and produces the block list that
 *   writer renders. It understands the résumé class this project ships and the
 *   cover-letter preamble `app/cover_letter.py` emits — not LaTeX in general.
 *   Anything it does not recognize is passed through as a paragraph rather than
 *   dropped, because a visitor editing the LaTeX pane should see their text
 *   appear in the preview even when they write something this parser has never
 *   met.
 */

import type { Block, Span } from "../pdf";

/** Symbol macros, and the escapes that would otherwise show as backslashes. */
const SYMBOLS: [RegExp, string][] = [
  [/\\\$/g, "$"],
  [/\$\\sim\$/g, "~"],
  [/\\textbar\{\}|\\textbar\b/g, "|"],
  [/\\&/g, "&"],
  [/\\%/g, "%"],
  [/\\#/g, "#"],
  [/\\_/g, "_"],
  [/\\ldots\b/g, "…"],
  [/\\textendash\b/g, "–"],
  [/\\textemdash\b/g, "—"],
  [/---/g, "—"],
  [/--/g, "–"],
  [/~/g, " "],
];

/** Strip a `{...}` group, honouring nesting. Returns the inside and the rest. */
function takeGroup(text: string, start: number): { body: string; end: number } | null {
  if (text[start] !== "{") return null;
  let depth = 0;
  for (let i = start; i < text.length; i += 1) {
    if (text[i] === "\\") {
      i += 1;
      continue;
    }
    if (text[i] === "{") depth += 1;
    else if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return { body: text.slice(start + 1, i), end: i + 1 };
    }
  }
  return null;
}

function plain(text: string): string {
  let out = text;
  for (const [pattern, replacement] of SYMBOLS) out = out.replace(pattern, replacement);
  // A bare `\\` is a line break the caller already split on; any macro still
  // standing here had no argument worth keeping.
  out = out.replace(/\\\\/g, " ").replace(/\\[A-Za-z]+/g, "");
  // Anything still wearing braces is a group we have no opinion about.
  out = out.replace(/[{}]/g, "");
  // `` is the sentinel `preprocess` leaves where a math shorthand was, so
  // that the `~`-is-a-hard-space rule above cannot swallow it.
  return out.replace(//g, "~").replace(/\s+/g, " ");
}

/** Switch form (`{\bf x}`) rewritten as command form (`\textbf{x}`).
 *
 *  The résumé's Education line and the letter's name use the switch form, and
 *  handling it inside the span walker would mean tracking state across a brace
 *  group; rewriting first keeps that walker a straight scan. */
function normalizeSwitches(fragment: string): string {
  const map: Record<string, string> = {
    bf: "textbf",
    bfseries: "textbf",
    it: "textit",
    itshape: "textit",
    em: "textit",
  };
  let out = "";
  let i = 0;
  while (i < fragment.length) {
    if (fragment[i] !== "{") {
      out += fragment[i];
      i += 1;
      continue;
    }
    const group = takeGroup(fragment, i);
    const switched = group && /^\s*\\([A-Za-z]+)\s*/.exec(group.body);
    const replacement = switched ? map[switched[1] ?? ""] : undefined;
    if (group && switched && replacement) {
      out += `\\${replacement}{${normalizeSwitches(group.body.slice(switched[0].length))}}`;
      i = group.end;
      continue;
    }
    out += fragment[i];
    i += 1;
  }
  return out;
}

/** Macros that stand for a character rather than a command. */
const TEXT_MACROS: Record<string, string> = {
  textbar: "|",
  textbackslash: "\\",
  textasciitilde: "~",
  textendash: "–",
  textemdash: "—",
  ldots: "…",
  dots: "…",
  textbullet: "•",
};

/** Rewrites the two shapes the span walker cannot see: switch-form styling, and
 *  the `$\sim$` the résumé writes for "approximately". */
function preprocess(fragment: string): string {
  const math = fragment.replace(/\$\\sim\$/g, "").replace(/\\sim\b/g, "");
  return math.includes("{\\") ? normalizeSwitches(math) : math;
}

/**
 * Turn a fragment into spans.
 *
 * `\textbf`, `\textit`/`\emph` and `\href` are the only macros that change how
 * text reads in these documents; every other macro is dropped and its argument
 * kept, which is the right default for `\small`, `\LARGE` and friends.
 */
export function deTex(source: string, inherited: Partial<Span> = {}): Span[] {
  const fragment = preprocess(source);
  const spans: Span[] = [];
  let buffer = "";

  const flush = () => {
    if (!buffer) return;
    const text = plain(buffer);
    if (text) spans.push({ ...inherited, text });
    buffer = "";
  };

  let i = 0;
  while (i < fragment.length) {
    const char = fragment[i];
    if (char !== "\\") {
      buffer += char;
      i += 1;
      continue;
    }

    const macro = /^\\([A-Za-z]+)/.exec(fragment.slice(i));
    if (!macro) {
      buffer += fragment.slice(i, i + 2);
      i += 2;
      continue;
    }

    const name = macro[1];
    const cursor = i + macro[0].length;
    const group = takeGroup(fragment, cursor);

    if ((name === "textbf" || name === "bf") && group) {
      flush();
      spans.push(...deTex(group.body, { ...inherited, bold: true }));
      i = group.end;
      continue;
    }
    if ((name === "textit" || name === "emph" || name === "it") && group) {
      flush();
      spans.push(...deTex(group.body, { ...inherited, italic: true }));
      i = group.end;
      continue;
    }
    if (name === "href" && group) {
      const label = takeGroup(fragment, group.end);
      flush();
      const shown = label ? label.body : group.body;
      spans.push(...deTex(shown, { ...inherited, color: [0.12, 0.28, 0.72] }));
      i = label ? label.end : group.end;
      continue;
    }
    // Macros that ARE text. The walker reaches them before `plain` does, so
    // they have to be spelled out here or the separator simply disappears —
    // which is how the cover letter's contact line lost its pipes.
    const literal = TEXT_MACROS[name ?? ""];
    if (literal !== undefined) {
      buffer += literal;
      // `\textbar{}` carries an empty group purely to end the macro name.
      i = group && group.body === "" ? group.end : cursor;
      continue;
    }
    if (name === "hspace" || name === "vspace" || name === "label" || name === "setlength") {
      if (group) {
        i = group.end;
        continue;
      }
    }

    // An unknown macro: keep its argument, drop the macro itself.
    buffer += "";
    i = cursor;
    if (group && (name === "small" || name === "large" || name === "LARGE" || name === "Large")) {
      buffer += group.body;
      i = group.end;
    }
    continue;
  }

  flush();
  return spans;
}

/** Spans joined back to plain text — what the fact-checker and diff compare. */
export function spansToText(spans: Span[]): string {
  return spans
    .map((span) => (span.bold ? `**${span.text}**` : span.text))
    .join("")
    .replace(/\*\*\s*\*\*/g, " ")
    .trim();
}

/** LaTeX-escape a plain string, so generated text can never inject markup. */
export function escapeTex(text: string): string {
  return text
    .replace(/\\/g, "\\textbackslash{}")
    .replace(/([&%$#_{}])/g, "\\$1")
    .replace(/\^/g, "\\^{}")
    .replace(/~/g, "\\textasciitilde{}");
}

/* -------------------------------------------------------------- documents */

const SKIP = [
  /^\\documentclass/,
  /^\\usepackage/,
  /^\\set(length|list)/,
  /^\\def\\/,
  /^\\newcommand/,
  /^\\renewcommand/,
  /^\\pagestyle/,
  /^\\(begin|end)\{document\}/,
  /^\\(begin|end)\{rSection\}/,
  /^\\(begin|end)\{itemize\}/,
  /^\\(begin|end)\{tabular\}/,
  /^\\(begin|end)\{center\}/,
  /^\\itemsep/,
  /^\\vspace/,
  /^\\smallskip/,
  /^%/,
];

/** Split a document body into logical blocks, one per blank-line group. */
function paragraphs(tex: string): string[] {
  const body = tex.includes("\\begin{document}")
    ? tex.slice(tex.indexOf("\\begin{document}") + "\\begin{document}".length)
    : tex;
  return body
    .split(/\n\s*\n+/)
    .map((chunk) => chunk.trim())
    .filter(Boolean);
}

/**
 * The name and contact block, whichever way the document writes it.
 *
 * `consumedCenter` matters: the cover letter's header is a `center` environment
 * that also sits inside the document body, so the walk below has to be told it
 * was already rendered. Without that the letter's name and contact line appear
 * twice, once centred and once left-aligned.
 */
function headerBlocks(tex: string): { blocks: Block[]; consumedCenter: boolean } {
  const blocks: Block[] = [];
  const name = /\\name\{([^}]*)\}/.exec(tex);
  const address = tex.indexOf("\\address{");
  if (name) blocks.push({ type: "name", text: plain(name[1] ?? "") });

  if (address !== -1) {
    const group = takeGroup(tex, address + "\\address".length);
    if (group) {
      const spans: Span[] = [];
      group.body
        .split(/\\\\/)
        .map((line) => line.trim())
        .filter(Boolean)
        .forEach((line, index) => {
          if (index > 0) spans.push({ text: "   |   ", color: [0.45, 0.45, 0.5] });
          spans.push(...deTex(line));
        });
      blocks.push({ type: "center", spans, size: 9 });
    }
  }

  // The cover letter puts its header in a `center` environment instead.
  const centered = /\\begin\{center\}([\s\S]*?)\\end\{center\}/.exec(tex);
  if (!name && centered) {
    const lines = (centered[1] ?? "")
      .split(/\\\\(?:\[[^\]]*\])?/)
      .map((line) => line.trim())
      .filter(Boolean);
    const first = lines.shift();
    if (first) blocks.push({ type: "name", text: spansToText(deTex(first)).replace(/\*\*/g, "") });
    for (const line of lines) blocks.push({ type: "center", spans: deTex(line), size: 9 });
    return { blocks, consumedCenter: true };
  }

  return { blocks, consumedCenter: false };
}

/**
 * The whole document as renderable blocks.
 *
 * `rSection` becomes a heading, an `\item` a bullet, a `\hfill` line an entry
 * with a right-aligned date, and a `tabular` row a label/value pair. Everything
 * else is a paragraph.
 */
export function texToBlocks(tex: string): Block[] {
  const header = headerBlocks(tex);
  const blocks: Block[] = [...header.blocks];
  let inTabular = false;

  for (const chunk of paragraphs(tex)) {
    if (header.consumedCenter && chunk.startsWith("\\begin{center}")) continue;
    const section = /^\\begin\{rSection\}\{([^}]*)\}/.exec(chunk);
    if (section) {
      blocks.push({ type: "heading", text: plain(section[1] ?? "") });
      const rest = chunk.slice(section[0].length).trim();
      if (rest && !SKIP.some((pattern) => pattern.test(rest))) {
        blocks.push({ type: "para", spans: deTex(rest) });
      }
      continue;
    }
    if (/^\\end\{rSection\}/.test(chunk)) continue;

    if (chunk.includes("\\begin{tabular}")) inTabular = true;
    if (chunk.includes("\\end{tabular}")) inTabular = false;

    const lines = chunk
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !SKIP.some((pattern) => pattern.test(line)));
    if (lines.length === 0) continue;

    // A skills row: a label line, then `& value`, ended by `\\`.
    if (inTabular || lines.some((line) => line.startsWith("&"))) {
      const label = (lines[0] ?? "").replace(/\\\\$/, "").trim();
      const value = lines
        .slice(1)
        .join(" ")
        .replace(/^&\s*/, "")
        .replace(/\\\\\s*$/, "");
      if (value) {
        blocks.push({ type: "kv", label: plain(label), spans: deTex(value) });
        continue;
      }
    }

    const joined = lines.join("\n");
    if (joined.startsWith("\\item")) {
      for (const item of joined.split(/\\item\s+/).slice(1)) {
        const spans = deTex(item.replace(/\n/g, " "));
        if (spans.length > 0) blocks.push({ type: "bullet", spans });
      }
      continue;
    }

    for (const line of joined.split(/\\\\\s*/)) {
      const text = line.trim();
      if (!text) continue;
      const parts = text.split("\\hfill");
      if (parts.length === 2) {
        blocks.push({ type: "entry", left: deTex(parts[0] ?? ""), right: spansToText(deTex(parts[1] ?? "")).replace(/\*\*/g, "") });
      } else {
        const spans = deTex(text);
        if (spans.length > 0) blocks.push({ type: "para", spans });
      }
    }
  }

  return blocks;
}
