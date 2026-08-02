import {
  ArrowDownWideNarrow,
  Building2,
  CalendarClock,
  Layers,
  Server,
  Sparkles,
  X,
} from "lucide-react";
import type { Facets, Filters, SortField } from "@/lib/types";
import { titleCase } from "@/lib/format";
import { MultiSelect } from "./MultiSelect";
import { Segmented, cx } from "./primitives";

const POSTED_OPTIONS: { value: number | null; label: string }[] = [
  { value: null, label: "Any time" },
  { value: 1, label: "24h" },
  { value: 7, label: "7d" },
  { value: 30, label: "30d" },
];

const SORT_OPTIONS: { value: SortField; label: string }[] = [
  { value: "first_seen_at", label: "Recently found" },
  { value: "posted_at", label: "Recently posted" },
  { value: "title", label: "Title A–Z" },
  { value: "company", label: "Company A–Z" },
];

export function FilterBar({
  filters,
  facets,
  activeCount,
  onPatch,
  onToggle,
  onReset,
  hasLastVisit,
}: {
  filters: Filters;
  facets: Facets | undefined;
  activeCount: number;
  onPatch: (changes: Partial<Filters>) => void;
  onToggle: (key: "companies" | "ats" | "departments", value: string) => void;
  onReset: () => void;
  hasLastVisit: boolean;
}) {
  return (
    <div className="glass glass-sheen relative z-20 shrink-0 rounded-2xl px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <MultiSelect
          label="Company"
          icon={<Building2 size={14} />}
          options={facets?.companies ?? []}
          selected={filters.companies}
          onToggle={(value) => onToggle("companies", value)}
          onClear={() => onPatch({ companies: [] })}
        />
        <MultiSelect
          label="Platform"
          icon={<Server size={14} />}
          options={(facets?.ats ?? []).map((entry) => ({ ...entry, value: entry.value }))}
          selected={filters.ats}
          onToggle={(value) => onToggle("ats", value)}
          onClear={() => onPatch({ ats: [] })}
          searchable={false}
        />
        <MultiSelect
          label="Department"
          icon={<Layers size={14} />}
          options={facets?.departments ?? []}
          selected={filters.departments}
          onToggle={(value) => onToggle("departments", value)}
          onClear={() => onPatch({ departments: [] })}
        />

        <span className="mx-0.5 hidden h-6 w-px bg-edge lg:block" />

        <Segmented
          value={filters.remote === null ? "any" : filters.remote ? "remote" : "onsite"}
          onChange={(value) =>
            onPatch({ remote: value === "any" ? null : value === "remote" })
          }
          options={[
            { value: "any", label: "Anywhere" },
            { value: "remote", label: "Remote" },
            { value: "onsite", label: "On-site" },
          ]}
        />

        <Segmented
          value={filters.status}
          onChange={(status) => onPatch({ status })}
          options={[
            { value: "open", label: "Open" },
            { value: "closed", label: "Closed", title: "Postings that vanished from their board" },
            { value: "any", label: "All" },
          ]}
        />

        {/* Posted-within */}
        <div className="inline-flex items-center gap-0.5 rounded-xl border border-edge bg-panel p-0.5 text-[13px]">
          <CalendarClock size={14} className="mx-1.5 shrink-0 text-subtle" />
          {POSTED_OPTIONS.map((option) => (
            <button
              key={option.label}
              onClick={() => onPatch({ postedWithinDays: option.value })}
              className={cx(
                "rounded-[10px] px-2.5 py-1.5 font-medium transition-all duration-150",
                filters.postedWithinDays === option.value
                  ? "bg-accent text-white shadow-[0_2px_10px_-3px_var(--accent)]"
                  : "text-muted hover:bg-panel-hover hover:text-ink",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>

        {hasLastVisit && (
          <button
            onClick={() => onPatch({ newOnly: !filters.newOnly })}
            className={cx(
              "flex h-9 items-center gap-1.5 rounded-xl border px-3 text-[13px] font-medium transition-all duration-150",
              filters.newOnly
                ? "border-highlight/40 bg-highlight/12 text-highlight"
                : "border-edge bg-panel text-muted hover:bg-panel-hover hover:text-ink",
            )}
            title="Only jobs discovered since your last visit"
          >
            <Sparkles size={14} />
            New only
          </button>
        )}

        <span className="ml-auto flex items-center gap-2">
          {/* Sort */}
          <label className="flex h-9 items-center gap-1.5 rounded-xl border border-edge bg-panel px-2.5 text-[13px] text-muted">
            <ArrowDownWideNarrow size={14} className="shrink-0 opacity-70" />
            <select
              value={filters.sort}
              onChange={(event) => {
                const sort = event.target.value as SortField;
                // Alphabetical sorts read better ascending; dates newest-first.
                onPatch({
                  sort,
                  order: sort === "title" || sort === "company" ? "asc" : "desc",
                });
              }}
              className="cursor-pointer appearance-none bg-transparent pr-1 font-medium text-ink outline-none"
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value} className="bg-bg-elevated">
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          {activeCount > 0 && (
            <button
              onClick={onReset}
              className="flex h-9 items-center gap-1.5 rounded-xl border border-edge bg-panel px-3 text-[13px] font-medium text-muted transition-colors hover:border-danger/30 hover:bg-danger/10 hover:text-danger"
            >
              <X size={14} />
              Clear {activeCount}
            </button>
          )}
        </span>
      </div>

      {/* Active filter chips — a readable summary of a composed query. */}
      {activeCount > 0 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5 border-t border-edge pt-2.5">
          {filters.q.trim() && (
            <Chip
              label={`“${filters.q.trim()}”${filters.qScope === "title" ? " in title" : ""}`}
              onRemove={() => onPatch({ q: "" })}
            />
          )}
          {filters.companies.map((value) => (
            <Chip key={value} label={value} onRemove={() => onToggle("companies", value)} />
          ))}
          {filters.ats.map((value) => (
            <Chip key={value} label={titleCase(value)} onRemove={() => onToggle("ats", value)} />
          ))}
          {filters.departments.map((value) => (
            <Chip key={value} label={value} onRemove={() => onToggle("departments", value)} />
          ))}
          {filters.remote !== null && (
            <Chip
              label={filters.remote ? "Remote" : "On-site"}
              onRemove={() => onPatch({ remote: null })}
            />
          )}
          {filters.status !== "open" && (
            <Chip
              label={`Status: ${filters.status}`}
              onRemove={() => onPatch({ status: "open" })}
            />
          )}
          {filters.postedWithinDays !== null && (
            <Chip
              label={`Posted ≤ ${filters.postedWithinDays}d`}
              onRemove={() => onPatch({ postedWithinDays: null })}
            />
          )}
          {filters.newOnly && (
            <Chip label="New since last visit" onRemove={() => onPatch({ newOnly: false })} />
          )}
        </div>
      )}
    </div>
  );
}

function Chip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className="group flex items-center gap-1 rounded-lg border border-accent/25 bg-accent-soft py-1 pr-1 pl-2 text-[12px] font-medium text-accent-text">
      <span className="max-w-[220px] truncate">{label}</span>
      <button
        onClick={onRemove}
        aria-label={`Remove filter ${label}`}
        className="rounded-md p-0.5 opacity-60 transition-opacity hover:opacity-100"
      >
        <X size={12} />
      </button>
    </span>
  );
}
