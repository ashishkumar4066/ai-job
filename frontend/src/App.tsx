import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, PlugZap, Send } from "lucide-react";
import { api, ApiError, fetchProfile, filtersToFilterSet, transferToMatches } from "@/lib/api";
import { useDebounced, useHotkeys, useLastVisit, usePrefetchJob, useTheme } from "@/lib/hooks";
import { useDailyRefresh } from "@/lib/useDailyRefresh";
import { useFilters } from "@/lib/useFilters";
import type { Job } from "@/lib/types";
import { FilterBar } from "@/components/FilterBar";
import { Funnel } from "@/components/Funnel";
import { JobDrawer } from "@/components/JobDrawer";
import { JobTable } from "@/components/JobTable";
import { MatchesView } from "@/components/MatchesView";
import { ProfilePanel } from "@/components/ProfilePanel";
import { SideNav } from "@/components/SideNav";
import { StatsStrip } from "@/components/StatsStrip";
import { SyncScreen } from "@/components/SyncScreen";
import { TopBar } from "@/components/TopBar";
import { EmptyState, Kbd, cx } from "@/components/primitives";

const PAGE_SIZE = 100;

// The page re-renders on every keystroke in the search box (the draft query
// lives here) and on every poll of a running sweep. These panels depend on
// neither, so with stable props they skip those renders entirely.
const MemoSideNav = memo(SideNav);
const MemoStatsStrip = memo(StatsStrip);
const MemoFilterBar = memo(FilterBar);
const MemoJobTable = memo(JobTable);

export default function App() {
  const queryClient = useQueryClient();
  const { dark, toggle } = useTheme();
  const { lastVisit, markSeenNow } = useLastVisit();
  const {
    filters,
    selectedJobId,
    view,
    setView,
    patch,
    reset,
    selectJob,
    toggleInList,
    setScope,
    activeCount,
  } = useFilters();

  // Phones hide the rail; this is the drawer that stands in for it.
  const [navOpen, setNavOpen] = useState(false);
  const closeNav = useCallback(() => setNavOpen(false), []);

  // Every visit sweeps the boards once a day; nothing below renders until it
  // settles, so the table never shows a stale snapshot of the market.
  const sync = useDailyRefresh();

  // The profile gate. `/profile` answers 200 with `configured: false` on a
  // first run rather than 500, so "not set up yet" is a state this can act on.
  // Deliberately independent of the sweep: browsing jobs needs no profile, and
  // the sweep is the slow half, so the two run side by side.
  const profileQuery = useQuery({
    queryKey: ["profile"],
    queryFn: fetchProfile,
    staleTime: 60_000,
    retry: false,
  });
  const [profileOpen, setProfileOpen] = useState(false);
  // Which step the dialog opens on. The top bar offers two ways in — edit the
  // profile, or replace the résumé files — and they land in different places.
  const [profileStart, setProfileStart] = useState<"upload" | undefined>(undefined);
  const openProfile = useCallback((at?: "upload") => {
    setProfileStart(at);
    setProfileOpen(true);
  }, []);
  // Nothing in Matches or tailoring works without a profile, so a first run
  // gets a dialog it cannot dismiss instead of an app full of empty panels.
  const needsProfile = profileQuery.data?.configured === false;

  // The stats/filters/funnel block folds away while you read the list. Kept
  // here rather than in JobTable because the block is its sibling, not its
  // child — the table only reports which way the list moved.
  const [chromeHidden, setChromeHidden] = useState(false);
  const onScrollAway = useCallback((away: boolean) => setChromeHidden(away), []);

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
    // Kept enabled on the Matches tile so switching back is instant, and
    // because the rail shows the open-jobs count from the same data.
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

  // How the board narrows to this list. Same parameters as `/jobs`, so the
  // last step is the table's own total.
  const funnelQuery = useQuery({
    queryKey: ["jobs-funnel", filters, lastVisit],
    queryFn: () => api.jobsFunnel(filters, lastVisit),
    staleTime: 30_000,
    enabled: sync.ready && view === "jobs",
  });

  // "Send to Matches": the current Jobs filters become Matches' scope. The
  // nonce tells Matches to open its Run dialog, since a sent scope does
  // nothing until the (free) ranking pass re-gates the rows.
  const [transferNonce, setTransferNonce] = useState(0);
  const transfer = useMutation({
    mutationFn: () => transferToMatches(filtersToFilterSet(filters, lastVisit)),
    onSuccess: (prefs) => {
      queryClient.setQueryData(["prefs"], prefs);
      queryClient.invalidateQueries({ queryKey: ["matches-funnel"] });
      setTransferNonce(Date.now());
      setView("matches");
    },
  });

  const jobs: Job[] = useMemo(
    () => jobsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [jobsQuery.data],
  );
  const total = jobsQuery.data?.pages[0]?.total ?? 0;
  const openJobs = facetsQuery.data?.totals.open ?? null;
  const lastIngest =
    facetsQuery.data?.last_ingest_finished_at ?? sync.status?.last_finished_at ?? null;

  /* ------------------------------------------------------------ navigation */
  const selectedIndex = useMemo(
    () => (selectedJobId === null ? -1 : jobs.findIndex((job) => job.id === selectedJobId)),
    [jobs, selectedJobId],
  );

  const step = useCallback(
    (delta: number) => {
      // j/k belong to the job list; on any other tile they must not open a drawer.
      if (view !== "jobs" || jobs.length === 0) return;
      const next = selectedIndex < 0 ? 0 : selectedIndex + delta;
      const clamped = Math.min(Math.max(next, 0), jobs.length - 1);
      const job = jobs[clamped];
      if (job) selectJob(job.id);
    },
    [view, jobs, selectedIndex, selectJob],
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

  // The new-jobs pill is a jump into the list, so it carries the tile with it.
  const showNew = useCallback(() => {
    setView("jobs");
    patch({ newOnly: true, sort: "first_seen_at", order: "desc" });
  }, [setView, patch]);

  // With the drawer open, warm the jobs either side so j/k lands on a JD that
  // is already loaded.
  const prefetchJob = usePrefetchJob();
  useEffect(() => {
    if (selectedIndex < 0) return;
    for (const neighbour of [jobs[selectedIndex + 1], jobs[selectedIndex - 1]]) {
      if (neighbour) prefetchJob(neighbour.id);
    }
  }, [selectedIndex, jobs, prefetchJob]);

  // `cancelRefetch: false` joins a page request already in flight. The default
  // cancels and restarts it, and the table asks on every scroll frame until the
  // fetch state catches up, so one fast scroll requested the same page ~9 times.
  const { fetchNextPage } = jobsQuery;
  const loadMore = useCallback(() => void fetchNextPage({ cancelRefetch: false }), [fetchNextPage]);

  const resetAll = useCallback(() => {
    setDraftQuery("");
    reset();
  }, [reset]);
  const toggleNewOnly = useCallback(
    () => patch({ newOnly: !filters.newOnly }),
    [patch, filters.newOnly],
  );

  const selectedJob = selectedIndex >= 0 ? jobs[selectedIndex] : undefined;
  const connectionError =
    jobsQuery.error instanceof ApiError && jobsQuery.error.status === 0
      ? jobsQuery.error
      : null;

  return (
    <div className="flex h-screen gap-3 p-3 md:gap-4 md:p-4">
      {/* Backdrop: violet aurora over near-black, a ruled grid to give the
          black a sense of surface, and grain to keep the wide gradients from
          banding. All one fixed layer behind the app. */}
      <div className="aurora" aria-hidden>
        <div className="aurora-third" />
        <div className="grid-veil" />
        <div className="grain" />
      </div>

      <MemoSideNav
        view={view}
        onViewChange={setView}
        open={navOpen}
        onClose={closeNav}
        jobCount={openJobs}
        lastIngest={lastIngest}
        syncing={sync.syncing}
      />

      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <TopBar
          query={draftQuery}
          onQueryChange={setDraftQuery}
          scope={filters.qScope}
          onScopeChange={setScope}
          dark={dark}
          onToggleTheme={toggle}
          onRefresh={sync.refresh}
          refreshing={sync.syncing}
          newCount={lastVisit ? (facetsQuery.data?.totals.new_since ?? 0) : 0}
          onShowNew={showNew}
          onOpenNav={() => setNavOpen(true)}
          onOpenProfile={() => openProfile()}
          onReplaceResume={() => openProfile("upload")}
          profileName={profileQuery.data?.full_name ?? ""}
          // Matches has no list for the search box to filter — see TopBar.
          title={view === "matches" ? "Matches" : undefined}
        />

        {view === "matches" ? (
          // Deliberately outside the sync gate. Matches reads persisted scores
          // rather than live board data, so a pending sweep has nothing stale
          // to protect us from — and gating it would make the panel wait ~90s
          // on a sweep whose results it does not even display.
          <MatchesView
            transferNonce={transferNonce}
            selectedJobId={selectedJobId}
            onSelectJob={selectJob}
            onBrowseJobs={() => setView("jobs")}
          />
        ) : !sync.ready ? (
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
            {/* The fold. `grid-rows-[1fr] → [0fr]` is what makes the height
                animate at all: the block's natural height is unknown, and
                `height: auto` does not transition. The inner track is what
                actually collapses, so the clip and the slide live there. */}
            <div
              className={cx(
                "grid shrink-0",
                "transition-[grid-template-rows,opacity,margin] duration-[380ms] ease-[cubic-bezier(0.22,1,0.36,1)]",
                "motion-reduce:transition-none",
                chromeHidden ? "-mb-3 grid-rows-[0fr] opacity-0" : "grid-rows-[1fr] opacity-100",
              )}
              // Nothing inside is reachable while it is folded, so it leaves
              // the tab order rather than becoming an invisible tab stop.
              aria-hidden={chromeHidden}
              inert={chromeHidden || undefined}
            >
              {/* The clip. Only while folded: the Company/Platform/Department
                  popovers are absolutely positioned inside the filter bar and
                  hang below it, so a permanent overflow-hidden here would cut
                  every dropdown off at the funnel. */}
              <div
                className={cx(
                  "min-h-0",
                  chromeHidden ? "overflow-hidden" : "overflow-visible",
                )}
              >
                {/* The slide, and it has to be a separate element from the
                    clip above — a transform on the clipping box moves the box
                    and its clip together, so the content would not travel
                    against the edge and the movement would be invisible. */}
                <div
                  className={cx(
                    "flex flex-col gap-3",
                    "transition-transform duration-[380ms] ease-[cubic-bezier(0.22,1,0.36,1)]",
                    "motion-reduce:transition-none",
                    chromeHidden ? "-translate-y-2" : "translate-y-0",
                  )}
                >
                  <MemoStatsStrip
                    facets={facetsQuery.data}
                    matching={total}
                    loading={jobsQuery.isLoading || facetsQuery.isLoading}
                    hasLastVisit={Boolean(lastVisit)}
                    newOnlyActive={filters.newOnly}
                    onToggleNewOnly={toggleNewOnly}
                  />

                  <MemoFilterBar
                    filters={filters}
                    facets={facetsQuery.data}
                    activeCount={activeCount}
                    onPatch={patch}
                    onToggle={toggleInList}
                    onReset={resetAll}
                    hasLastVisit={Boolean(lastVisit)}
                  />

                  <Funnel
                    steps={funnelQuery.data?.steps}
                    loading={funnelQuery.isLoading}
                    action={
                      <>
                        {transfer.isError && (
                          <span className="text-[11.5px] text-danger">
                            {(transfer.error as Error).message}
                          </span>
                        )}
                        <button
                          type="button"
                          disabled={
                            transfer.isPending || total === 0 || filters.status !== "open"
                          }
                          onClick={() => transfer.mutate()}
                          title={
                            filters.status !== "open"
                              ? "Only open jobs can be sent to Matches"
                              : "Matches will work on exactly these filters. Ranking and the verifier are free; the LLM only reads a short list you confirm."
                          }
                          className="btn-primary flex items-center gap-1.5 rounded-xl px-3.5 py-2 text-[12.5px] font-semibold disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {transfer.isPending ? (
                            <Loader2 size={14} className="animate-spin" />
                          ) : (
                            <Send size={14} />
                          )}
                          Send {total.toLocaleString()} to Matches
                        </button>
                      </>
                    }
                  />
                </div>
              </div>
            </div>

            {sync.error && (
              <div className="animate-fade-up flex shrink-0 items-center gap-2 rounded-xl border border-danger/35 bg-danger/10 px-4 py-2.5 text-[13px] text-danger">
                <AlertTriangle size={15} className="shrink-0" />
                {sync.error.message}
              </div>
            )}

            <MemoJobTable
              jobs={jobs}
              total={total}
              loading={jobsQuery.isLoading}
              loadingMore={jobsQuery.isFetchingNextPage}
              hasMore={Boolean(jobsQuery.hasNextPage)}
              onLoadMore={loadMore}
              selectedId={selectedJobId}
              onSelect={selectJob}
              lastVisit={lastVisit}
              onReset={resetAll}
              onScrollAway={onScrollAway}
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
      </div>

      <ProfilePanel
        open={profileOpen || needsProfile}
        blocking={needsProfile}
        startAt={profileStart}
        onClose={() => {
          setProfileOpen(false);
          setProfileStart(undefined);
        }}
      />

      {view === "jobs" && sync.ready && selectedJobId !== null && (
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
