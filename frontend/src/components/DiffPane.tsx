import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileQuestion, Loader2 } from "lucide-react";

import { fetchDocumentDiff } from "@/lib/api";
import type { DiffRow, DiffStatus } from "@/lib/types";
import { cx } from "./primitives";

/**
 * The tailored résumé against the untailored one — CLAUDE.md's "diff against
 * the base résumé so I can see exactly what was changed".
 *
 * Region-level, not line-level. The tailored .tex is the template with spans
 * spliced back in, so a line diff of the LaTeX is mostly brace and wrapping
 * noise around each touched bullet, which buries the only thing being looked
 * for: whether a *claim* changed. The backend does the region matching (see
 * `documents.diff_against_base`), including by content rather than by region
 * id, because a bullet's id is its position and dropping one renumbers the rest.
 *
 * Within a reworded region, words are highlighted here. Word-level is the right
 * grain for reading a rewrite: the whole point is to see that "80% mAP" is still
 * "80% mAP" while the framing around it moved.
 */

const LABELS: Record<DiffStatus, string> = {
  reworded: "Reworded",
  dropped: "Dropped",
  moved: "Moved",
  added: "Added",
  unchanged: "Unchanged",
};

const TONES: Record<DiffStatus, string> = {
  reworded: "border-sky-400/40 bg-sky-400/10 text-sky-200",
  dropped: "border-rose-400/40 bg-rose-400/10 text-rose-200",
  moved: "border-violet-400/40 bg-violet-400/10 text-violet-200",
  added: "border-amber-400/40 bg-amber-400/10 text-amber-200",
  unchanged: "border-edge bg-white/5 text-subtle",
};

/** Words in `text` that do not appear in `against`, for highlighting.
 *
 *  A set rather than a positional diff: a tailored bullet reorders clauses, and
 *  a positional algorithm would then mark most of the line as changed even
 *  where every word survived. What matters is which words are *new*.
 */
function newWords(text: string, against: string): Set<string> {
  const known = new Set(against.toLowerCase().match(/[\w%+./-]+/g) ?? []);
  const out = new Set<string>();
  for (const word of text.toLowerCase().match(/[\w%+./-]+/g) ?? []) {
    if (!known.has(word)) out.add(word);
  }
  return out;
}

function Marked({ text, against, tone }: { text: string; against: string; tone: string }) {
  const changed = useMemo(() => newWords(text, against), [text, against]);
  if (!text) return <span className="text-subtle italic">(empty)</span>;
  return (
    <>
      {text.split(/(\s+)/).map((part, index) => {
        const bare = part.toLowerCase().replace(/^[^\w%+./-]+|[^\w%+./-]+$/g, "");
        return /^\s+$/.test(part) || !changed.has(bare) ? (
          part
        ) : (
          <mark key={index} className={cx("rounded px-0.5", tone)}>
            {part}
          </mark>
        );
      })}
    </>
  );
}

export function DiffPane({ docId }: { docId: number }) {
  const [showUnchanged, setShowUnchanged] = useState(false);
  const diff = useQuery({
    queryKey: ["document-diff", docId],
    queryFn: () => fetchDocumentDiff(docId),
    staleTime: 10_000,
  });

  if (diff.isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center gap-2 text-[12.5px] text-subtle">
        <Loader2 size={16} className="animate-spin" /> Comparing…
      </div>
    );
  }

  if (diff.isError) {
    return (
      <div className="flex-1 px-4 py-6 text-[12.5px] text-danger">
        {(diff.error as Error).message}
      </div>
    );
  }

  const data = diff.data!;
  if (!data.available) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
        <FileQuestion size={20} className="text-subtle" />
        <p className="max-w-sm text-[12.5px] leading-relaxed text-subtle">{data.note}</p>
      </div>
    );
  }

  const changed = data.rows.filter((row) => row.status !== "unchanged");
  const shown = showUnchanged ? data.rows : changed;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-edge px-3 py-2">
        {(["reworded", "dropped", "moved", "added"] as const)
          .filter((status) => data[status] > 0)
          .map((status) => (
            <span
              key={status}
              className={cx("rounded-md border px-1.5 py-0.5 text-[10.5px] font-medium", TONES[status])}
            >
              {data[status]} {LABELS[status].toLowerCase()}
            </span>
          ))}
        {changed.length === 0 && (
          <span className="text-[11.5px] text-subtle">
            Identical to your base résumé — nothing was changed.
          </span>
        )}
        <button
          type="button"
          onClick={() => setShowUnchanged((v) => !v)}
          className="ml-auto text-[11px] text-subtle underline-offset-2 hover:text-ink hover:underline"
        >
          {showUnchanged ? "Changes only" : `Show all ${data.rows.length}`}
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2.5">
        <ul className="space-y-2">
          {shown.map((row, index) => (
            <Row key={`${row.status}:${row.region_id}:${index}`} row={row} />
          ))}
        </ul>
      </div>
    </div>
  );
}

function Row({ row }: { row: DiffRow }) {
  const moved =
    row.status === "moved" && row.base_index != null && row.current_index != null
      ? row.current_index < row.base_index
        ? `up ${row.base_index - row.current_index}`
        : `down ${row.current_index - row.base_index}`
      : null;

  return (
    <li className="rounded-xl border border-edge bg-panel/50 p-2.5">
      <div className="mb-1.5 flex items-center gap-2">
        <span
          className={cx(
            "rounded-md border px-1.5 py-0.5 text-[10px] font-semibold tracking-wide uppercase",
            TONES[row.status],
          )}
        >
          {LABELS[row.status]}
          {moved && ` ${moved}`}
        </span>
        <span className="min-w-0 truncate text-[11px] text-subtle">{row.label}</span>
        <span className="ml-auto shrink-0 font-mono text-[10px] text-subtle/70">
          {row.region_id}
        </span>
      </div>

      {row.status === "unchanged" || row.status === "moved" ? (
        <p className="text-[12px] leading-relaxed text-muted">{row.current || row.base}</p>
      ) : (
        <div className="space-y-1.5">
          {row.base && (
            <p className="text-[12px] leading-relaxed text-subtle">
              <span className="mr-1.5 select-none text-rose-300/70">−</span>
              <Marked text={row.base} against={row.current} tone="bg-rose-400/20 text-rose-100" />
            </p>
          )}
          {row.current && (
            <p className="text-[12px] leading-relaxed text-ink">
              <span className="mr-1.5 select-none text-emerald-300/70">+</span>
              <Marked
                text={row.current}
                against={row.base}
                tone="bg-emerald-400/20 text-emerald-100"
              />
            </p>
          )}
        </div>
      )}
    </li>
  );
}
