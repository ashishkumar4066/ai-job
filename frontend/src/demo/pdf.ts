/**
 * A very small PDF writer, so a document the demo generates has a preview.
 *
 * The ten documents that ship with the snapshot were compiled by the real
 * Tectonic at export time, and their PDFs are served as static files. A
 * document the *visitor* asks for has no such file and nothing in a browser can
 * run LaTeX, so the demo lays the same content out itself and writes a PDF by
 * hand.
 *
 * It is deliberately minimal: one page size, the three base-14 Helvetica faces
 * (which every reader has, so nothing is embedded), WinAnsi encoding, and no
 * compression. That keeps the whole thing to a few hundred lines with no
 * dependency, and the output is a real PDF that Chrome, Firefox and Preview all
 * render — which is the entire requirement.
 *
 * It is NOT a LaTeX renderer and makes no attempt to match Tectonic's
 * typography. It renders the structure the generator hands it.
 */

/* ------------------------------------------------------------------ fonts */

/** AFM advance widths, 1/1000 em, for codes 32..126. */
const HELVETICA = [
  278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556,
  556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667,
  611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667,
  667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500,
  222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
];

const HELVETICA_BOLD = [
  278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556,
  556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667,
  611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667,
  667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556,
  278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
];

/** The handful of WinAnsi codes above 127 this renderer can emit. */
const HIGH_WIDTHS: Record<number, [number, number]> = {
  0x91: [222, 238], // ‘
  0x92: [222, 238], // ’
  0x93: [333, 500], // “
  0x94: [333, 500], // ”
  0x95: [350, 350], // •
  0x96: [556, 556], // –
  0x97: [1000, 1000], // —
  0xa0: [278, 278], // nbsp
  0xb7: [278, 278], // ·
};

export type FontId = "F1" | "F2" | "F3";

function widthTable(font: FontId): number[] {
  return font === "F2" ? HELVETICA_BOLD : HELVETICA;
}

/**
 * Map a JS string onto the WinAnsi bytes the content stream carries.
 *
 * Anything outside the small set above is folded to an ASCII lookalike rather
 * than dropped — a résumé rendering "Ahmedabad" as "Ahmed bad" because of one
 * stray character would look like a bug in the data, not in the renderer.
 */
const FOLD: Record<string, string> = {
  "‘": "",
  "’": "",
  "“": "",
  "”": "",
  "•": "",
  "–": "",
  "—": "",
  "…": "...",
  " ": " ",
  "−": "-",
  "×": "x",
  "→": "->",
  "�": "-",
};

export function toWinAnsi(text: string): string {
  let out = "";
  for (const char of text) {
    const folded = FOLD[char];
    if (folded !== undefined) {
      out += folded;
      continue;
    }
    const code = char.codePointAt(0)!;
    if (code >= 32 && code <= 126) out += char;
    else if (code === 9) out += " ";
    else if (HIGH_WIDTHS[code]) out += String.fromCharCode(code);
    else if (code < 256) out += String.fromCharCode(code);
    else out += "?";
  }
  return out;
}

/** Width of already-WinAnsi text, in points. */
export function widthOf(text: string, font: FontId, size: number): number {
  const table = widthTable(font);
  let total = 0;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    if (code >= 32 && code <= 126) total += table[code - 32] ?? 500;
    else total += (HIGH_WIDTHS[code] ?? [500, 500])[font === "F2" ? 1 : 0] ?? 500;
  }
  return (total * size) / 1000;
}

/* ----------------------------------------------------------------- blocks */

export interface Span {
  text: string;
  bold?: boolean;
  italic?: boolean;
  /** `[r, g, b]`, 0..1. Absent means black. */
  color?: [number, number, number];
}

export type Block =
  | { type: "name"; text: string }
  | { type: "center"; spans: Span[]; size?: number }
  | { type: "heading"; text: string }
  | { type: "entry"; left: Span[]; right: string }
  | { type: "bullet"; spans: Span[] }
  | { type: "para"; spans: Span[]; size?: number }
  | { type: "kv"; label: string; spans: Span[] }
  | { type: "gap"; height: number };

export interface PageStyle {
  width: number;
  height: number;
  margin: { top: number; right: number; bottom: number; left: number };
  size: number;
  leading: number;
  /** Width of the left column in a `kv` row. */
  labelWidth: number;
}

export const RESUME_STYLE: PageStyle = {
  width: 612,
  height: 792,
  margin: { top: 34, right: 40, bottom: 34, left: 40 },
  size: 9,
  leading: 11,
  labelWidth: 108,
};

export const LETTER_STYLE: PageStyle = {
  width: 612,
  height: 792,
  margin: { top: 54, right: 68, bottom: 54, left: 68 },
  size: 10.5,
  leading: 14.5,
  labelWidth: 0,
};

/* ------------------------------------------------------------ line layout */

interface Piece {
  text: string;
  font: FontId;
  size: number;
  width: number;
  color?: [number, number, number];
}

function fontFor(span: Span): FontId {
  if (span.bold) return "F2";
  if (span.italic) return "F3";
  return "F1";
}

/**
 * Greedy word wrap across a run of spans.
 *
 * Spans are split on spaces so a bold phrase can break mid-phrase, which is
 * what a résumé bullet full of `\textbf{}` needs — wrapping at span boundaries
 * only would leave ragged half-empty lines wherever a long bold run started.
 */
function wrap(spans: Span[], size: number, maxWidth: number): Piece[][] {
  const lines: Piece[][] = [];
  let line: Piece[] = [];
  let used = 0;

  const push = (piece: Piece) => {
    const last = line[line.length - 1];
    if (last && last.font === piece.font && last.color === piece.color) {
      last.text += piece.text;
      last.width += piece.width;
    } else {
      line.push(piece);
    }
    used += piece.width;
  };

  for (const span of spans) {
    const font = fontFor(span);
    const text = toWinAnsi(span.text);
    const words = text.split(/(\s+)/).filter((part) => part !== "");
    for (const word of words) {
      const width = widthOf(word, font, size);
      const blank = /^\s+$/.test(word);
      if (blank) {
        if (line.length === 0) continue;
        if (used + width > maxWidth) continue;
        push({ text: word, font, size, width, color: span.color });
        continue;
      }
      if (used + width > maxWidth && line.length > 0) {
        // Trim a trailing space before breaking, or the ragged edge drifts.
        const last = line[line.length - 1];
        if (last && /\s$/.test(last.text)) {
          const trimmed = last.text.replace(/\s+$/, "");
          last.width = widthOf(trimmed, last.font, size);
          last.text = trimmed;
        }
        lines.push(line);
        line = [];
        used = 0;
      }
      push({ text: word, font, size, width, color: span.color });
    }
  }
  if (line.length > 0) lines.push(line);
  return lines.length > 0 ? lines : [[]];
}

/* ------------------------------------------------------- stream assembly */

function escapeText(text: string): string {
  return text.replace(/[\\()]/g, (char) => `\\${char}`).replace(/[-ÿ]/g, (char) => {
    const code = char.charCodeAt(0);
    return `\\${code.toString(8).padStart(3, "0")}`;
  });
}

class Canvas {
  readonly parts: string[] = [];

  text(x: number, y: number, piece: Piece): void {
    const color = piece.color;
    if (color) this.parts.push(`${color[0]} ${color[1]} ${color[2]} rg`);
    this.parts.push(
      `BT /${piece.font} ${piece.size} Tf 1 0 0 1 ${x.toFixed(2)} ${y.toFixed(2)} Tm (${escapeText(
        piece.text,
      )}) Tj ET`,
    );
    if (color) this.parts.push("0 0 0 rg");
  }

  line(x1: number, y: number, x2: number, thickness: number, gray = 0.35): void {
    this.parts.push(
      `${gray} G ${thickness} w ${x1.toFixed(2)} ${y.toFixed(2)} m ${x2.toFixed(2)} ${y.toFixed(
        2,
      )} l S 0 G`,
    );
  }

  toString(): string {
    return this.parts.join("\n");
  }
}

/* -------------------------------------------------------------- the pages */

/** Lay the blocks out and return one content stream per page. */
function paginate(blocks: Block[], style: PageStyle): string[] {
  const pages: Canvas[] = [];
  const left = style.margin.left;
  const right = style.width - style.margin.right;
  const contentWidth = right - left;

  let canvas = new Canvas();
  let y = style.height - style.margin.top;
  pages.push(canvas);

  const room = (needed: number): void => {
    if (y - needed >= style.margin.bottom) return;
    canvas = new Canvas();
    pages.push(canvas);
    y = style.height - style.margin.top;
  };

  const drawLines = (lines: Piece[][], x: number, leading: number, indent = 0): void => {
    lines.forEach((line, index) => {
      room(leading);
      let cursor = index === 0 ? x : x + indent;
      for (const piece of line) {
        canvas.text(cursor, y - piece.size, piece);
        cursor += piece.width;
      }
      y -= leading;
    });
  };

  for (const block of blocks) {
    switch (block.type) {
      case "gap":
        y -= block.height;
        break;

      case "name": {
        const size = 19;
        room(size + 6);
        const text = toWinAnsi(block.text);
        const width = widthOf(text, "F2", size);
        canvas.text((style.width - width) / 2, y - size, {
          text,
          font: "F2",
          size,
          width,
        });
        y -= size + 4;
        break;
      }

      case "center": {
        const size = block.size ?? style.size;
        for (const line of wrap(block.spans, size, contentWidth)) {
          const width = line.reduce((total, piece) => total + piece.width, 0);
          room(size + 3);
          let cursor = (style.width - width) / 2;
          for (const piece of line) {
            canvas.text(cursor, y - size, piece);
            cursor += piece.width;
          }
          y -= size + 3;
        }
        break;
      }

      case "heading": {
        const size = style.size + 1.5;
        room(size + 9);
        y -= 5;
        const text = toWinAnsi(block.text.toUpperCase());
        canvas.text(left, y - size, {
          text,
          font: "F2",
          size,
          width: widthOf(text, "F2", size),
        });
        y -= size + 2.5;
        canvas.line(left, y, right, 0.6);
        y -= 5;
        break;
      }

      case "entry": {
        const size = style.size + 0.5;
        room(size + 4);
        const rightText = toWinAnsi(block.right);
        const rightWidth = widthOf(rightText, "F1", size);
        if (rightText) {
          canvas.text(right - rightWidth, y - size, {
            text: rightText,
            font: "F1",
            size,
            width: rightWidth,
          });
        }
        let cursor = left;
        for (const span of block.left) {
          const font = fontFor(span);
          const text = toWinAnsi(span.text);
          const width = widthOf(text, font, size);
          canvas.text(cursor, y - size, { text, font, size, width, color: span.color });
          cursor += width;
        }
        y -= size + 2.5;
        break;
      }

      case "bullet": {
        const indent = 11;
        const lines = wrap(block.spans, style.size, contentWidth - indent);
        room(style.leading);
        canvas.text(left + 1.5, y - style.size, {
          text: "",
          font: "F1",
          size: style.size,
          width: 0,
        });
        drawLines(lines, left + indent, style.leading);
        y -= 1.5;
        break;
      }

      case "para":
        drawLines(
          wrap(block.spans, block.size ?? style.size, contentWidth),
          left,
          block.size ? block.size + 4 : style.leading,
        );
        break;

      case "kv": {
        const label = toWinAnsi(block.label);
        const lines = wrap(block.spans, style.size, contentWidth - style.labelWidth);
        room(style.leading);
        canvas.text(left, y - style.size, {
          text: label,
          font: "F2",
          size: style.size,
          width: widthOf(label, "F2", style.size),
        });
        drawLines(lines, left + style.labelWidth, style.leading);
        y -= 1.5;
        break;
      }
    }
  }

  return pages.map((page) => page.toString());
}

/* ---------------------------------------------------------------- the file */

/**
 * Serialize the blocks as a PDF.
 *
 * Built as a Latin-1 string so that one character is one byte and the xref
 * offsets are just string lengths. Anything above U+00FF is already folded away
 * by `toWinAnsi`, so nothing is lost by the narrowing at the end.
 */
export function renderPdf(blocks: Block[], style: PageStyle): Uint8Array {
  const streams = paginate(blocks, style);
  const objects: string[] = [];
  const pageIds: number[] = [];

  // 1 catalog, 2 pages, 3..5 fonts, then a (page, content) pair each.
  const firstPage = 6;
  streams.forEach((_, index) => pageIds.push(firstPage + index * 2));

  objects[1] = "<< /Type /Catalog /Pages 2 0 R >>";
  objects[2] =
    `<< /Type /Pages /Count ${streams.length} /Kids [${pageIds
      .map((id) => `${id} 0 R`)
      .join(" ")}] >>`;
  const font = (name: string) =>
    `<< /Type /Font /Subtype /Type1 /BaseFont /${name} /Encoding /WinAnsiEncoding >>`;
  objects[3] = font("Helvetica");
  objects[4] = font("Helvetica-Bold");
  objects[5] = font("Helvetica-Oblique");

  streams.forEach((stream, index) => {
    const pageId = pageIds[index]!;
    objects[pageId] =
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${style.width} ${style.height}] ` +
      `/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents ${pageId + 1} 0 R >>`;
    objects[pageId + 1] = `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`;
  });

  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  for (let id = 1; id < objects.length; id += 1) {
    offsets[id] = body.length;
    body += `${id} 0 obj\n${objects[id]}\nendobj\n`;
  }

  const xref = body.length;
  const count = objects.length;
  body += `xref\n0 ${count}\n0000000000 65535 f \n`;
  for (let id = 1; id < count; id += 1) {
    body += `${String(offsets[id]).padStart(10, "0")} 00000 n \n`;
  }
  body += `trailer\n<< /Size ${count} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;

  const bytes = new Uint8Array(body.length);
  for (let i = 0; i < body.length; i += 1) bytes[i] = body.charCodeAt(i) & 0xff;
  return bytes;
}
