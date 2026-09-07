import { useMemo, useState } from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";
import { useDismissable } from "@/lib/hooks";
import type { FacetEntry } from "@/lib/types";
import { cx } from "./primitives";

/**
 * Searchable multi-select backed by facet counts, so you only ever see options
 * that actually have jobs behind them.
 */
export function MultiSelect({
  label,
  icon,
  options,
  selected,
  onToggle,
  onClear,
  searchable = true,
}: {
  label: string;
  icon: React.ReactNode;
  options: FacetEntry[];
  selected: string[];
  onToggle: (value: string) => void;
  onClear: () => void;
  searchable?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useDismissable<HTMLDivElement>(open, () => setOpen(false));

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matched = needle
      ? options.filter((option) => option.value.toLowerCase().includes(needle))
      : options;
    // Keep chosen values pinned to the top so they never scroll out of reach.
    const chosen = matched.filter((option) => selected.includes(option.value));
    const rest = matched.filter((option) => !selected.includes(option.value));
    return [...chosen, ...rest];
  }, [options, query, selected]);

  const count = selected.length;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className={cx(
          "flex h-9 items-center gap-2 rounded-xl border px-3 text-[13px] font-medium transition-all duration-200",
          count > 0
            ? "border-accent/45 bg-accent-soft text-accent-text shadow-[0_4px_14px_-8px_var(--accent-glow)]"
            : open
              ? "border-edge-strong bg-panel-hover text-ink"
              : "border-edge bg-panel text-muted hover:border-edge-strong hover:bg-panel-hover hover:text-ink",
        )}
      >
        <span className="opacity-70">{icon}</span>
        <span>{label}</span>
        {count > 0 && (
          <span className="grid size-[18px] place-items-center rounded-full bg-gradient-to-b from-accent to-accent-strong text-[10px] font-semibold text-white shadow-[inset_0_1px_0_oklch(100%_0_0_/_0.3)]">
            {count}
          </span>
        )}
        <ChevronDown
          size={14}
          className={cx("opacity-50 transition-transform duration-300", open && "rotate-180")}
        />
      </button>

      {open && (
        <div className="glass-popover glass-sheen animate-fade-up absolute top-11 left-0 z-40 w-72 overflow-hidden rounded-2xl">
          {searchable && (
            <div className="flex items-center gap-2 border-b border-edge px-3 py-2.5">
              <Search size={14} className="shrink-0 text-subtle" />
              <input
                autoFocus
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={`Filter ${label.toLowerCase()}…`}
                className="w-full bg-transparent text-[13px] text-ink outline-none placeholder:text-subtle"
              />
              {query && (
                <button onClick={() => setQuery("")} aria-label="Clear search">
                  <X size={13} className="text-subtle hover:text-ink" />
                </button>
              )}
            </div>
          )}

          <div className="max-h-72 overflow-y-auto overscroll-contain p-1.5">
            {visible.length === 0 ? (
              <p className="px-3 py-6 text-center text-[13px] text-subtle">No matches</p>
            ) : (
              visible.map((option) => {
                const active = selected.includes(option.value);
                return (
                  <button
                    key={option.value}
                    onClick={() => onToggle(option.value)}
                    className={cx(
                      "group flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors",
                      active
                        ? "bg-accent-soft text-ink"
                        : "text-muted hover:bg-panel-hover hover:text-ink",
                    )}
                  >
                    <span
                      className={cx(
                        "grid size-[16px] shrink-0 place-items-center rounded-[5px] border transition-all duration-200",
                        active
                          ? "border-accent bg-gradient-to-b from-accent to-accent-strong text-white shadow-[0_2px_8px_-3px_var(--accent-glow)]"
                          : "border-edge-strong group-hover:border-accent/50",
                      )}
                    >
                      {active && <Check size={11} strokeWidth={3} />}
                    </span>
                    <span className="flex-1 truncate">{option.value}</span>
                    <span className="shrink-0 font-mono text-[11px] text-subtle tabular-nums">
                      {option.count}
                    </span>
                  </button>
                );
              })
            )}
          </div>

          {count > 0 && (
            <div className="border-t border-edge p-1.5">
              <button
                onClick={onClear}
                className="w-full rounded-lg px-2.5 py-1.5 text-[12px] font-medium text-muted transition-colors hover:bg-panel-hover hover:text-ink"
              >
                Clear {count} selected
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
