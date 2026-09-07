import { useEffect, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowUpRight, Globe2, Loader2, MapPin, SearchX } from "lucide-react";
import { formatLocations, formatSalary, relativeTime } from "@/lib/format";
import type { Job } from "@/lib/types";
import { Badge, CompanyAvatar, EmptyState, SkeletonRow, cx } from "./primitives";

const ROW_HEIGHT = 68;
/** Rows rendered beyond the viewport — enough to hide fast-scroll tearing. */
const OVERSCAN = 8;

/** Shared column template keeps the header and rows locked together. */
const GRID =
  "grid grid-cols-[minmax(0,1fr)_112px] items-center gap-3 md:grid-cols-[minmax(0,2.1fr)_minmax(0,1.1fr)_120px_96px] lg:grid-cols-[minmax(0,2.2fr)_minmax(0,1.1fr)_minmax(0,0.85fr)_minmax(0,1fr)_104px_92px]";

export function JobTable({
  jobs,
  total,
  loading,
  loadingMore,
  hasMore,
  onLoadMore,
  selectedId,
  onSelect,
  lastVisit,
  onReset,
}: {
  jobs: Job[];
  total: number;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  onLoadMore: () => void;
  selectedId: number | null;
  onSelect: (id: number) => void;
  lastVisit: string | null;
  onReset: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: jobs.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
    getItemKey: (index) => jobs[index]?.id ?? index,
  });

  const items = virtualizer.getVirtualItems();

  // Infinite scroll: pull the next page once the tail comes into view.
  useEffect(() => {
    const last = items[items.length - 1];
    if (!last) return;
    if (hasMore && !loadingMore && last.index >= jobs.length - 12) onLoadMore();
  }, [items, hasMore, loadingMore, jobs.length, onLoadMore]);

  // A new query should start at the top, not wherever the old one was.
  useEffect(() => {
    if (!loading) scrollRef.current?.scrollTo({ top: 0 });
  }, [loading]);

  // Keep the keyboard-selected row visible.
  useEffect(() => {
    if (selectedId === null) return;
    const index = jobs.findIndex((job) => job.id === selectedId);
    if (index >= 0) virtualizer.scrollToIndex(index, { align: "auto" });
  }, [selectedId, jobs, virtualizer]);

  const lastVisitTime = lastVisit ? new Date(lastVisit).getTime() : null;

  return (
    <section className="glass-strong glass-sheen relative flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl">
      {/* Column header */}
      <div
        className={cx(
          GRID,
          "relative z-[2] shrink-0 border-b border-edge bg-panel-strong/40 px-4 py-2.5",
          "text-[10.5px] font-semibold tracking-[0.1em] text-subtle uppercase backdrop-blur-sm",
        )}
      >
        <span>Role</span>
        <span className="hidden md:block">Location</span>
        <span className="hidden lg:block">Department</span>
        <span className="hidden lg:block">Salary</span>
        <span className="hidden md:block">Posted</span>
        <span className="text-right">Apply</span>
      </div>

      {loading ? (
        <div className="flex-1 overflow-hidden">
          {Array.from({ length: 10 }, (_, index) => (
            <SkeletonRow key={index} />
          ))}
        </div>
      ) : jobs.length === 0 ? (
        <EmptyState
          icon={<SearchX size={26} />}
          title="No jobs match these filters"
          description="Try widening the date range, clearing a company filter, or switching search scope back to All."
          action={
            <button
              onClick={onReset}
              className="btn-primary mt-1 rounded-xl px-4 py-2 text-[13px] font-semibold"
            >
              Clear all filters
            </button>
          }
        />
      ) : (
        <div className="relative min-h-0 flex-1">
          {/* Rows dissolve into the header rather than being sliced by it. */}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 top-0 z-[1] h-4 bg-gradient-to-b from-black/12 to-transparent dark:from-black/40"
          />

          <div ref={scrollRef} className="h-full overflow-y-auto overscroll-contain">
            <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
              {items.map((virtualRow) => {
                const job = jobs[virtualRow.index];
                if (!job) return null;
                const isNew =
                  lastVisitTime !== null && new Date(job.first_seen_at).getTime() > lastVisitTime;

                return (
                  <div
                    key={virtualRow.key}
                    data-index={virtualRow.index}
                    className="absolute top-0 left-0 w-full"
                    style={{
                      height: virtualRow.size,
                      transform: `translateY(${virtualRow.start}px)`,
                    }}
                  >
                    <JobRow
                      job={job}
                      isNew={isNew}
                      selected={job.id === selectedId}
                      onSelect={onSelect}
                    />
                  </div>
                );
              })}
            </div>

            {loadingMore && (
              <div className="flex items-center justify-center gap-2 py-4 text-[13px] text-subtle">
                <Loader2 size={14} className="animate-spin" />
                Loading more…
              </div>
            )}
            {!hasMore && jobs.length > 0 && (
              <p className="py-4 text-center text-[12px] text-subtle">
                All {total.toLocaleString()} matching jobs loaded
              </p>
            )}
          </div>

          {/* Matching fade at the foot, so the list runs out of the panel
              rather than being chopped off by its edge. */}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 bottom-0 z-[1] h-6 bg-gradient-to-t from-black/22 to-transparent dark:from-black/55"
          />
        </div>
      )}
    </section>
  );
}

function JobRow({
  job,
  isNew,
  selected,
  onSelect,
}: {
  job: Job;
  isNew: boolean;
  selected: boolean;
  onSelect: (id: number) => void;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onSelect(job.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(job.id);
        }
      }}
      className={cx(
        GRID,
        // `isolate` keeps the -z-10 wash from escaping behind the panel itself.
        "group relative isolate h-full cursor-pointer border-b border-edge px-4",
        "transition-colors duration-150",
      )}
    >
      {/* Hover wash: violet pooling in from the left, strongest at the row
          start where the eye lands, fading out before the Apply column. */}
      <span
        aria-hidden
        className={cx(
          "pointer-events-none absolute inset-0 -z-10 transition-opacity duration-200",
          selected
            ? "bg-gradient-to-r from-accent-soft via-accent-soft to-transparent opacity-100"
            : "bg-gradient-to-r from-accent-soft/70 to-transparent opacity-0 group-hover:opacity-100",
        )}
      />
      {/* Selection rail */}
      <span
        aria-hidden
        className={cx(
          "pointer-events-none absolute inset-y-0 left-0 -z-10 w-[3px] rounded-r-full transition-all duration-200",
          selected
            ? "bg-gradient-to-b from-accent to-accent-strong opacity-100"
            : "bg-accent opacity-0 group-hover:opacity-40",
        )}
      />

      {/* Role + company */}
      <div className="flex min-w-0 items-center gap-3">
        <span className="relative shrink-0">
          <CompanyAvatar name={job.company} />
          {isNew && (
            <span className="absolute -top-0.5 -right-0.5 size-2.5 rounded-full bg-highlight shadow-[0_0_8px_var(--highlight)] ring-2 ring-bg" />
          )}
        </span>
        <div className="min-w-0">
          <p className="flex items-center gap-1.5">
            <span
              className={cx(
                "truncate text-[14px] font-medium transition-colors",
                selected ? "text-accent-text" : "text-ink group-hover:text-accent-text",
              )}
            >
              {job.title}
            </span>
            {job.status === "closed" && (
              <Badge tone="danger" className="hidden sm:inline-flex">
                Closed
              </Badge>
            )}
          </p>
          <p className="mt-0.5 flex items-center gap-1.5 truncate text-[12px] text-subtle">
            <span className="font-medium text-muted">{job.company}</span>
            <span className="opacity-40">·</span>
            <span className="capitalize">{job.ats}</span>
            <span className="md:hidden">
              <span className="opacity-40"> · </span>
              {formatLocations(job.locations, job.remote)}
            </span>
          </p>
        </div>
      </div>

      {/* Location */}
      <div className="hidden min-w-0 items-center gap-1.5 md:flex">
        {job.remote ? (
          <Globe2 size={13} className="shrink-0 text-accent-text" />
        ) : (
          <MapPin size={13} className="shrink-0 text-subtle" />
        )}
        <span className="truncate text-[13px] text-muted" title={job.locations.join(" · ")}>
          {formatLocations(job.locations, job.remote)}
        </span>
        {job.remote && (
          <Badge tone="accent" className="hidden shrink-0 xl:inline-flex">
            Remote
          </Badge>
        )}
      </div>

      {/* Department */}
      <div className="hidden min-w-0 lg:block">
        <span className="truncate text-[13px] text-subtle" title={job.department ?? undefined}>
          {job.department ?? "—"}
        </span>
      </div>

      {/* Salary — "Not stated" is a real answer here, not an empty cell. Most
          postings state no pay, and an unstated salary never disqualifies a
          job, so it has to read as a fact rather than as missing data. */}
      <div className="hidden min-w-0 lg:block">
        {(() => {
          const pay = formatSalary(job.salary_min, job.salary_max, job.salary_currency);
          return pay ? (
            <span className="truncate text-[13px] whitespace-nowrap text-muted tabular-nums">
              {pay}
            </span>
          ) : (
            <span className="text-[12.5px] text-subtle/60 italic">Not stated</span>
          );
        })()}
      </div>

      {/* Posted */}
      <div className="hidden md:block">
        <span className="text-[13px] whitespace-nowrap text-subtle tabular-nums">
          {relativeTime(job.posted_at ?? job.first_seen_at)}
        </span>
      </div>

      {/* Apply */}
      <div className="flex justify-end">
        <a
          href={job.apply_url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(event) => event.stopPropagation()}
          // Always visible (an empty "Apply" column reads as broken), but muted
          // until the row is hovered so 3000 rows don't shout at once.
          className="flex items-center gap-1 rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-[12px] font-semibold text-muted opacity-55 transition-all duration-200 group-hover:opacity-100 hover:border-accent/45 hover:bg-accent-soft hover:text-accent-text hover:shadow-[0_4px_12px_-6px_var(--accent-glow)] focus-visible:opacity-100"
        >
          Apply
          <ArrowUpRight size={13} className="transition-transform duration-200 group-hover:translate-x-px group-hover:-translate-y-px" />
        </a>
      </div>
    </div>
  );
}
