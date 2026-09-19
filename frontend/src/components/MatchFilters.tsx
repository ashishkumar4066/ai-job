import { useEffect, useRef, useState } from "react";
import { BrainCircuit, Check, Filter, Gauge, ShieldCheck, X } from "lucide-react";

import type {
  LlmBandFilter,
  MatchBand,
  MatchBandFilters,
  ValidityBandFilter,
} from "@/lib/types";
import { FIT_BANDS } from "./CheckBadges";
import { cx } from "./primitives";

/**
 * The Matches "Filter" button and its popover.
 *
 * Three independent verdicts, each its own section, because they answer
 * different questions and used to be confused for one another:
 *
 *   - Ranker fit — the free keyword score against your profile.
 *   - LLM fit    — the deep read's band, where a current read exists.
 *   - Validator  — how real and current the POSTING looks, not how well it fits.
 *
 * Options within a section are ORed, sections are ANDed. Each option's count
 * is what the list would hold with the other two sections as they are.
 */

type Option<T extends string> = { id: T; label: string; dot?: string; hint?: string };

const FIT_OPTIONS: Option<MatchBand>[] = FIT_BANDS.map((b) => ({
  id: b.id,
  label: b.label,
  dot: b.dot,
}));

const LLM_OPTIONS: Option<LlmBandFilter>[] = [
  ...FIT_OPTIONS,
  { id: "unread", label: "Not read", hint: "No current deep read of this JD" },
];

const VALIDITY_OPTIONS: Option<ValidityBandFilter>[] = [
  { id: "solid", label: "Solid", dot: "bg-success", hint: "Validity 85–100" },
  { id: "ok", label: "OK", dot: "bg-success/60", hint: "Validity 70–84" },
  { id: "questionable", label: "Questionable", dot: "bg-highlight", hint: "Validity 50–69" },
  { id: "suspect", label: "Suspect", dot: "bg-danger", hint: "Validity under 50" },
  { id: "unchecked", label: "Not checked", hint: "The validity pass has not run on it" },
];

export const EMPTY_BAND_FILTERS: MatchBandFilters = { fit: [], llm: [], validity: [] };

export function activeFilterCount(f: MatchBandFilters): number {
  return f.fit.length + f.llm.length + f.validity.length;
}

/** "Ranker: Strong, Excellent · LLM: Not read" — for the header line. */
export function describeFilters(f: MatchBandFilters): string {
  const part = <T extends string>(name: string, ids: T[], opts: Option<T>[]) =>
    ids.length ? `${name}: ${opts.filter((o) => ids.includes(o.id)).map((o) => o.label).join(", ")}` : "";
  return [
    part("Ranker", f.fit, FIT_OPTIONS),
    part("LLM", f.llm, LLM_OPTIONS),
    part("Validator", f.validity, VALIDITY_OPTIONS),
  ]
    .filter(Boolean)
    .join(" · ");
}

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

export function MatchFilterButton({
  value,
  onChange,
  counts,
}: {
  value: MatchBandFilters;
  onChange: (next: MatchBandFilters) => void;
  counts: {
    fit: Partial<Record<MatchBand, number>>;
    llm: Partial<Record<LlmBandFilter, number>>;
    validity: Partial<Record<ValidityBandFilter, number>>;
  };
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const active = activeFilterCount(value);

  // Closes on a click outside or Esc, like any menu.
  useEffect(() => {
    if (!open) return;
    const onDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className={cx(
          "flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12px] font-medium transition-colors",
          active
            ? "border-accent/40 bg-accent/12 text-accent-text"
            : "border-edge bg-panel text-muted hover:border-edge-strong hover:text-ink",
        )}
      >
        <Filter size={13} />
        Filter
        {active > 0 && (
          <span className="grid min-w-4 place-items-center rounded-full bg-accent px-1 font-mono text-[10px] leading-4 text-white">
            {active}
          </span>
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Filter matches"
          className="glass-strong animate-fade-in absolute top-full right-0 z-30 mt-1.5 w-[min(340px,calc(100vw-32px))] overflow-hidden rounded-xl border border-edge shadow-xl"
        >
          <div className="flex items-center gap-2 border-b border-edge px-3.5 py-2.5">
            <span className="text-[12.5px] font-semibold text-ink">Filter matches</span>
            {active > 0 && (
              <button
                type="button"
                onClick={() => onChange(EMPTY_BAND_FILTERS)}
                className="ml-auto text-[11.5px] text-subtle underline-offset-2 hover:text-ink hover:underline"
              >
                Clear all
              </button>
            )}
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close filters"
              className={cx(
                "rounded-md p-0.5 text-subtle transition-colors hover:text-ink",
                active === 0 && "ml-auto",
              )}
            >
              <X size={14} />
            </button>
          </div>

          <div className="max-h-[min(70vh,520px)] overflow-y-auto">
            <Section
              icon={<Gauge size={12} />}
              title="Ranker fit"
              hint="The free keyword score of the JD against your profile."
              options={FIT_OPTIONS}
              selected={value.fit}
              counts={counts.fit}
              onToggle={(id) => onChange({ ...value, fit: toggle(value.fit, id) })}
              onClear={() => onChange({ ...value, fit: [] })}
            />
            <Section
              icon={<BrainCircuit size={12} />}
              title="LLM fit"
              hint="The deep read's verdict, where the LLM has read the current JD."
              options={LLM_OPTIONS}
              selected={value.llm}
              counts={counts.llm}
              onToggle={(id) => onChange({ ...value, llm: toggle(value.llm, id) })}
              onClear={() => onChange({ ...value, llm: [] })}
            />
            <Section
              icon={<ShieldCheck size={12} />}
              title="Validator"
              hint="How real and current the posting looks — not how well it fits you."
              options={VALIDITY_OPTIONS}
              selected={value.validity}
              counts={counts.validity}
              onToggle={(id) => onChange({ ...value, validity: toggle(value.validity, id) })}
              onClear={() => onChange({ ...value, validity: [] })}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function Section<T extends string>({
  icon,
  title,
  hint,
  options,
  selected,
  counts,
  onToggle,
  onClear,
}: {
  icon: React.ReactNode;
  title: string;
  hint: string;
  options: Option<T>[];
  selected: T[];
  counts: Partial<Record<T, number>>;
  onToggle: (id: T) => void;
  onClear: () => void;
}) {
  return (
    <div className="border-b border-edge px-3.5 py-3 last:border-b-0">
      <div className="mb-0.5 flex items-center gap-1.5">
        <span className="grid size-5 place-items-center rounded-md bg-accent/12 text-accent-text">
          {icon}
        </span>
        <span className="text-[12px] font-semibold text-ink">{title}</span>
        {selected.length > 0 && (
          <button
            type="button"
            onClick={onClear}
            className="ml-auto text-[11px] text-subtle underline-offset-2 hover:text-ink hover:underline"
          >
            Any
          </button>
        )}
      </div>
      <p className="mb-2 text-[11px] leading-snug text-subtle">{hint}</p>
      <div className="flex flex-col">
        {options.map((opt) => {
          const on = selected.includes(opt.id);
          const n = counts[opt.id] ?? 0;
          return (
            <button
              key={opt.id}
              type="button"
              role="checkbox"
              aria-checked={on}
              title={opt.hint}
              onClick={() => onToggle(opt.id)}
              className={cx(
                "flex items-center gap-2 rounded-md px-1.5 py-1 text-left text-[12px] transition-colors hover:bg-panel-strong",
                on ? "text-ink" : n === 0 ? "text-subtle" : "text-muted",
              )}
            >
              <span
                className={cx(
                  "grid size-3.5 shrink-0 place-items-center rounded-[4px] border",
                  on ? "border-accent bg-accent text-white" : "border-edge-strong",
                )}
              >
                {on && <Check size={10} strokeWidth={3} />}
              </span>
              {opt.dot ? (
                <span className={cx("size-1.5 shrink-0 rounded-full", opt.dot)} />
              ) : (
                <span className="size-1.5 shrink-0" />
              )}
              {opt.label}
              <span className="ml-auto font-mono text-[10.5px] opacity-70">
                {n.toLocaleString()}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
