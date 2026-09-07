import { useCallback, useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { AlertTriangle, PlugZap } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useDebounced, useHotkeys, useLastVisit, useTheme } from "@/lib/hooks";
import { useDailyRefresh } from "@/lib/useDailyRefresh";
import { useFilters } from "@/lib/useFilters";
import type { Job } from "@/lib/types";
import { FilterBar } from "@/components/FilterBar";
import { JobDrawer } from "@/components/JobDrawer";
import { JobTable } from "@/components/JobTable";
import { StatsStrip } from "@/components/StatsStrip";
import { SyncScreen } from "@/components/SyncScreen";
import { TopBar } from "@/components/TopBar";
import { EmptyState, Kbd } from "@/components/primitives";

const PAGE_SIZE = 100;

export default function App() {
  const { dark, toggle } = useTheme();
  const { lastVisit, markSeenNow } = useLastVisit();
  const { filters, selectedJobId, patch, reset, selectJob, toggleInList, setScope, activeCount } =
    useFilters();

  // Every visit sweeps the boards once a day; nothing below renders until it
  // settles, so the table never shows a stale snapshot of the market.
  const sync = useDailyRefresh();

  // Typing stays instant; only the settled value hits the API and the URL.
  const [draftQuery, setDraftQuery] = useState(filters.q);
  const debouncedQuery = useDebounced(draftQuery, 280);

  useEffect(() => {
    if (debouncedQuery !== filters.q) patch({ q: debouncedQuery });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery]);

  // Keep the box in sync when the URL changes underneath us (back/forward).
  useEffect(() => {
    setDraftQuery((current) => (current === filters.q ? current : filters.q));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.q]);

  const jobsQuery = useInfiniteQuery({
    queryKey: ["jobs", filters, lastVisit],
    queryFn: ({ pageParam }) =>
      api.jobs(filters, { limit: PAGE_SIZE, offset: pageParam as number }, lastVisit),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
    staleTime: 30_000,
    // Not merely hidden — not requested. Nothing pre-sweep ever reaches the UI.
    enabled: sync.ready,
  });

  const facetsQuery = useQuery({
    // `matchesPrefs` is part of the key: it changes the counts, so a cached
    // set from the other state would describe rows that are not on screen.
    queryKey: ["facets", filters.status, filters.matchesPrefs, lastVisit],
    queryFn: () => api.facets(lastVisit, filters.status, filters.matchesPrefs),
    staleTime: 60_000,
    enabled: sync.ready,
  });

  const jobs: Job[] = useMemo(
    () => jobsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [jobsQuery.data],
  );
  const total = jobsQuery.data?.pages[0]?.total ?? 0;

  /* ------------------------------------------------------------ navigation */
  const selectedIndex = useMemo(
    () => (selectedJobId === null ? -1 : jobs.findIndex((job) => job.id === selectedJobId)),
    [jobs, selectedJobId],
  );

  const step = useCallback(
    (delta: number) => {
      if (jobs.length === 0) return;
      const next = selectedIndex < 0 ? 0 : selectedIndex + delta;
      const clamped = Math.min(Math.max(next, 0), jobs.length - 1);
      const job = jobs[clamped];
      if (job) selectJob(job.id);
    },
    [jobs, selectedIndex, selectJob],
  );

  useHotkeys({
    j: () => step(1),
    k: () => step(-1),
    Escape: () => selectJob(null),
    r: () => {
      if (!sync.syncing) sync.refresh();
    },
    t: toggle,
  });

  const selectedJob = selectedIndex >= 0 ? jobs[selectedIndex] : undefined;
  const connectionError =
    jobsQuery.error instanceof ApiError && jobsQuery.error.status === 0
      ? jobsQuery.error
      : null;

  return (
    <div className="flex h-screen flex-col gap-3 p-3 md:p-4">
      {/* Backdrop: violet aurora over near-black, a ruled grid to give the
          black a sense of surface, and grain to keep the wide gradients from
          banding. All one fixed layer behind the app. */}
      <div className="aurora" aria-hidden>
        <div className="aurora-third" />
        <div className="grid-veil" />
        <div className="grain" />
      </div>

      <TopBar
        query={draftQuery}
        onQueryChange={setDraftQuery}
        scope={filters.qScope}
        onScopeChange={setScope}
        dark={dark}
        onToggleTheme={toggle}
        onRefresh={sync.refresh}
        refreshing={sync.syncing}
        lastIngest={
          facetsQuery.data?.last_ingest_finished_at ?? sync.status?.last_finished_at ?? null
        }
        newCount={lastVisit ? (facetsQuery.data?.totals.new_since ?? 0) : 0}
        onShowNew={() => patch({ newOnly: true, sort: "first_seen_at", order: "desc" })}
      />

      {!sync.ready ? (
        <SyncScreen
          status={sync.status}
          error={sync.error}
          onSkip={sync.skipWait}
          onRetry={sync.refresh}
        />
      ) : connectionError ? (
        <div className="glass-strong flex flex-1 items-center justify-center rounded-2xl">
          <EmptyState
            icon={<PlugZap size={24} />}
            title="Backend unreachable"
            description="Start the Phase 1 API, then retry:  uvicorn app.main:app --reload"
            action={
              <button
                onClick={() => jobsQuery.refetch()}
                className="btn-primary mt-1 rounded-xl px-4 py-2 text-[13px] font-semibold"
              >
                Retry
              </button>
            }
          />
        </div>
      ) : (
        <>
          <StatsStrip
            facets={facetsQuery.data}
            matching={total}
            loading={jobsQuery.isLoading || facetsQuery.isLoading}
            hasLastVisit={Boolean(lastVisit)}
            newOnlyActive={filters.newOnly}
            onToggleNewOnly={() => patch({ newOnly: !filters.newOnly })}
          />

          <FilterBar
            filters={filters}
            facets={facetsQuery.data}
            activeCount={activeCount}
            onPatch={patch}
            onToggle={toggleInList}
            onReset={() => {
              setDraftQuery("");
              reset();
            }}
            hasLastVisit={Boolean(lastVisit)}
          />

          {sync.error && (
            <div className="animate-fade-up flex shrink-0 items-center gap-2 rounded-xl border border-danger/35 bg-danger/10 px-4 py-2.5 text-[13px] text-danger">
              <AlertTriangle size={15} className="shrink-0" />
              {sync.error.message}
            </div>
          )}

          <JobTable
            jobs={jobs}
            total={total}
            loading={jobsQuery.isLoading}
            loadingMore={jobsQuery.isFetchingNextPage}
            hasMore={Boolean(jobsQuery.hasNextPage)}
            onLoadMore={jobsQuery.fetchNextPage}
            selectedId={selectedJobId}
            onSelect={selectJob}
            lastVisit={lastVisit}
            onReset={() => {
              setDraftQuery("");
              reset();
            }}
          />

          <footer className="flex shrink-0 flex-wrap items-center justify-between gap-2 px-1 text-[11px] text-subtle">
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="flex items-center gap-1">
                <Kbd>/</Kbd> search
              </span>
              <span className="flex items-center gap-1">
                <Kbd>j</Kbd>
                <Kbd>k</Kbd> navigate
              </span>
              <span className="flex items-center gap-1">
                <Kbd>r</Kbd> refresh
              </span>
              <span className="flex items-center gap-1">
                <Kbd>t</Kbd> theme
              </span>
              <span className="flex items-center gap-1">
                <Kbd>esc</Kbd> close
              </span>
            </span>
            <span className="flex items-center gap-3">
              {jobs.length > 0 && (
                <span>
                  Showing {jobs.length.toLocaleString()} of {total.toLocaleString()}
                </span>
              )}
              {lastVisit && (
                <button
                  onClick={markSeenNow}
                  className="rounded-md px-1.5 py-0.5 transition-colors hover:bg-panel-hover hover:text-ink"
                >
                  Mark all as seen
                </button>
              )}
            </span>
          </footer>
        </>
      )}

      {sync.ready && selectedJobId !== null && (
        <JobDrawer
          jobId={selectedJobId}
          summary={selectedJob}
          onClose={() => selectJob(null)}
          onPrev={() => step(-1)}
          onNext={() => step(1)}
          hasPrev={selectedIndex > 0}
          hasNext={selectedIndex >= 0 && selectedIndex < jobs.length - 1}
        />
      )}
    </div>
  );
}
