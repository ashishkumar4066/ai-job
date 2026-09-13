import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpDown,
  ArrowUpRight,
  Banknote,
  Clock,
  Gauge,
  Loader2,
  Send,
  SlidersHorizontal,
  Sparkles,
  TriangleAlert,
  X,
} from "lucide-react";

import { clearTransfer, fetchMatches, fetchMatchesFunnel, fetchProfile } from "@/lib/api";
import { absoluteDate, relativeTime } from "@/lib/format";
import { LlmBadge, VerifierBadge } from "./CheckBadges";
import { Funnel } from "./Funnel";
import { JobDrawer } from "./JobDrawer";
import { PrefsPanel } from "./PrefsPanel";
import type { Match, MatchBand, MatchSort } from "@/lib/types";
import { CompanyAvatar, EmptyState, SkeletonRow, cx, useSpotlight } from "./primitives";

/**
 * The Matches panel — every eligible posting ranked against `profile.yaml`.
 *
 * Two things this surface deliberately does NOT do:
 *
 * 1. **It never hides a low score.** The ranker is an ordering, not a filter;
 *    Phase 1's eligibility rules already did the dropping, on facts a board
 *    states outright. A keyword-overlap scorer run against this board would
 *    have discarded a full-stack AI role that explicitly wanted India-based
 *    candidates, purely because its description named no technologies. So a
 *    weak score sorts to the bottom and stays visible.
 *
 * 2. **It shows bands, not just numbers.** Re-scoring the same posting moves
 *    the number a few points, so presenting 73-vs-68 as a ranking would be
 *    precision the data does not have. The band is the honest unit; the
 *    number is shown small, beside it, for ordering.
 */

const BANDS: { id: MatchBand; label: string; tone: string; dot: string }[] = [
  { id: "excellent", label: "Excellent", tone: "text-success", dot: "bg-success" },
  { id: "strong", label: "Strong", tone: "text-accent-text", dot: "bg-accent" },
  { id: "moderate", label: "Moderate", tone: "text-highlight", dot: "bg-highlight" },
  { id: "weak", label: "Weak", tone: "text-muted", dot: "bg-muted" },
  { id: "poor", label: "Poor", tone: "text-subtle", dot: "bg-subtle" },
];

const USD_PAY_SOURCE: Record<NonNullable<Match["usd_pay_source"]>, string> = {
  board: "from the board's salary field",
  deep_read: "quoted by the deep read",
  jd: "found in the job description",
};

const SORTS: { id: MatchSort; label: string }[] = [
  { id: "posted_at", label: "Recently posted" },
  { id: "score", label: "Best fit" },
  { id: "first_seen_at", label: "Recently found" },
];

export function MatchesView({
  transferNonce,
  selectedJobId,
  onSelectJob,
  onBrowseJobs,
}: {
  /** Bumped by "Send to Matches" on the Jobs tab — opens the Run dialog. */
  transferNonce: number;
  selectedJobId: number | null;
  onSelectJob: (id: number | null) => void;
  onBrowseJobs: () => void;
}) {
  const qc = useQueryClient();
  const [band, setBand] = useState<MatchBand | null>(null);
  // Newest posting first by default; the server sorts `posted_at` desc with
  // unstated dates last.
  const [sort, setSort] = useState<MatchSort>("posted_at");
  const [onlyShortlisted, setOnlyShortlisted] = useState(false);
  const [hideBlocked, setHideBlocked] = useState(false);
  const [showPrefs, setShowPrefs] = useState(false);
  // Mirrored up from the dialog's Run control, so a run started there and
  // left running after the dialog closes still shows on the header button.
  const [running, setRunning] = useState(false);
  const [showPrefMisses, setShowPrefMisses] = useState(false);

  // A freshly sent scope does nothing until the free ranking pass re-gates
  // the rows, so arriving from "Send to Matches" opens the Run dialog.
  useEffect(() => {
    if (transferNonce) setShowPrefs(true);
  }, [transferNonce]);

  const profile = useQuery({ queryKey: ["profile"], queryFn: fetchProfile, staleTime: 60_000 });
  const funnel = useQuery({
    queryKey: ["matches-funnel"],
    queryFn: fetchMatchesFunnel,
    staleTime: 30_000,
  });
  const clear = useMutation({
    mutationFn: clearTransfer,
    onSuccess: (prefs) => {
      qc.setQueryData(["prefs"], prefs);
      qc.invalidateQueries({ queryKey: ["matches-funnel"] });
      setShowPrefs(true);
    },
  });

  const matches = useQuery({
    queryKey: ["matches", band, sort, onlyShortlisted, hideBlocked, showPrefMisses],
    queryFn: () =>
      fetchMatches({
        band,
        sort,
        shortlisted: onlyShortlisted ? true : null,
        hideBlocked,
        // Preference misses are hidden by default — that is the shape of this
        // surface. They are never lost: the Jobs tile shows the whole board.
        matchPrefs: !showPrefMisses,
        limit: 200,
      }),
    staleTime: 30_000,
  });

  const bands = matches.data?.bands ?? {};
  const total = matches.data?.total ?? 0;
  // Before the band selection: the "All" chip and the header count stay put
  // when a band is clicked; only `total` (the selected band) moves.
  const totalAll = matches.data?.total_all ?? 0;
  const items = matches.data?.items ?? [];

  // The drawer walks the match list, not the Jobs list, so j/k-style prev/next
  // stays inside what is on screen here.
  const selectedIndex =
    selectedJobId === null ? -1 : items.findIndex((m) => m.job.id === selectedJobId);
  const stepTo = (delta: number) => {
    const next = items[selectedIndex + delta];
    if (next) onSelectJob(next.job.id);
  };

  // A profile exists but nothing is scored yet — the one state where the
  // panel genuinely has nothing to show and the fix is a single button.
  const unscored =
    matches.isSuccess &&
    total === 0 &&
    !band &&
    !onlyShortlisted &&
    !hideBlocked &&
    !showPrefMisses;
  const sent = funnel.data?.transfer ?? null;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2.5">
      {/* A dialog, but always mounted: a run started inside it keeps polling
          after it closes, and reopens it if the deep read needs a confirm. */}
      <PrefsPanel
        open={showPrefs}
        onOpen={() => setShowPrefs(true)}
        onClose={() => setShowPrefs(false)}
        onRunningChange={setRunning}
        onRunFinished={() => {
          qc.invalidateQueries({ queryKey: ["profile"] });
          qc.invalidateQueries({ queryKey: ["matches-funnel"] });
        }}
        externalChangeAt={transferNonce}
      />

      {/* Every number between the eligible board and the LLM, each step
          saying what it removed. */}
      <Funnel
        steps={funnel.data?.steps}
        loading={funnel.isLoading}
        footnote={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {sent ? (
              <>
                <Send size={12} className="text-accent-text" />
                <span>
                  Working on jobs sent from Jobs
                  {sent.labels.length ? `: ${sent.labels.join(" · ")}` : " (no filters)"}
                  {" — "}
                  {sent.count_at_transfer.toLocaleString()} when sent
                  {sent.transferred_at ? ` ${relativeTime(sent.transferred_at)}` : ""}
                </span>
                <button
                  type="button"
                  onClick={() => clear.mutate()}
                  disabled={clear.isPending}
                  className="rounded-md px-1.5 py-0.5 text-subtle underline-offset-2 hover:text-ink hover:underline"
                >
                  Use all eligible jobs
                </button>
              </>
            ) : (
              <span>
                Working on every eligible job. Filter on the Jobs tab and press{" "}
                <span className="text-ink">Send to Matches</span> to narrow this.
              </span>
            )}
            {funnel.data?.stale && (
              <span className="flex items-center gap-1 text-highlight">
                <AlertTriangle size={12} />
                Preferences changed since the last run — run matching to refresh these numbers
              </span>
            )}
          </span>
        }
      />

    <div className="glass-strong glass-sheen relative flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl">
      {/* ---------------------------------------------------------- header */}
      <div className="shrink-0 border-b border-edge px-4 py-3 md:px-5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <div className="flex items-center gap-2">
            <span className="grid size-7 place-items-center rounded-lg bg-accent/12 text-accent-text">
              <Gauge size={15} />
            </span>
            <div className="leading-tight">
              <div className="text-[13.5px] font-semibold text-ink">
                {totalAll.toLocaleString()} ranked
                {band && (
                  <span className="ml-1.5 text-[11.5px] font-normal text-subtle">
                    · showing {total.toLocaleString()} {band}
                  </span>
                )}
              </div>
              {profile.data && (
                <div className="text-[11px] text-subtle">
                  against {profile.data.full_name || "your profile"} ·{" "}
                  <span className="font-mono">{profile.data.version.slice(0, 8)}</span>
                </div>
              )}
            </div>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <label className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-[12px] text-muted transition-colors hover:border-edge-strong">
              <input
                type="checkbox"
                checked={onlyShortlisted}
                onChange={(e) => setOnlyShortlisted(e.target.checked)}
                className="size-3.5 accent-[var(--accent)]"
              />
              LLM shortlist
              {matches.data && (
                <span className="font-mono text-[11px] text-subtle">
                  {matches.data.shortlisted}
                </span>
              )}
            </label>

            <label
              title="Hide jobs whose description rules you out: US work authorization, no visa sponsorship, US citizens only, security clearance, or remote only within a region that excludes India."
              className={cx(
                "flex cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] transition-colors",
                hideBlocked
                  ? "border-danger/35 bg-danger/10 text-danger"
                  : "border-edge bg-panel text-muted hover:border-edge-strong",
              )}
            >
              <input
                type="checkbox"
                checked={hideBlocked}
                onChange={(e) => setHideBlocked(e.target.checked)}
                className="size-3.5 accent-[var(--accent)]"
              />
              Hide blocked
              {matches.data && (
                <span className="font-mono text-[11px] opacity-70">{matches.data.blocked}</span>
              )}
            </label>

            <div className="relative">
              <ArrowUpDown
                size={13}
                className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-subtle"
              />
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value as MatchSort)}
                className="appearance-none rounded-lg border border-edge bg-panel py-1.5 pr-7 pl-7 text-[12px] text-ink transition-colors hover:border-edge-strong focus:outline-none"
              >
                {SORTS.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>

            <button
              onClick={() => setShowPrefs(true)}
              aria-haspopup="dialog"
              className={cx(
                "flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12px] font-medium transition-colors",
                running
                  ? "border-accent bg-accent/15 text-accent-text"
                  : "border-edge bg-panel text-muted hover:border-edge-strong hover:text-ink",
              )}
              title="Preferences and Run matching. Filters Matches only."
            >
              {running ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <SlidersHorizontal size={13} />
              )}
              {running ? "Matching…" : "Preferences"}
            </button>
          </div>
        </div>

        {/* Band filter chips. Counts describe the whole result, not the page. */}
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <Chip active={band === null} onClick={() => setBand(null)} label="All" count={totalAll} />
          {BANDS.map((b) => (
            <Chip
              key={b.id}
              active={band === b.id}
              onClick={() => setBand(band === b.id ? null : b.id)}
              label={b.label}
              count={bands[b.id] ?? 0}
              dot={b.dot}
            />
          ))}
        </div>

        {/* Preference misses are hidden by default, but never silently: the
            count is always visible and one click reveals them, with the
            reason each one missed shown on its row. */}
        <label className="mt-2 flex w-fit cursor-pointer items-center gap-1.5 text-[11.5px] text-muted">
          <input
            type="checkbox"
            checked={showPrefMisses}
            onChange={(e) => setShowPrefMisses(e.target.checked)}
            className="size-3.5 accent-[var(--accent)]"
          />
          Include jobs that miss my preferences
        </label>
      </div>

      {/* ------------------------------------------------------------ list */}
      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2 md:px-3">
        {matches.isPending && (
          <div className="space-y-1.5 p-1">
            {Array.from({ length: 8 }).map((_, i) => (
              <SkeletonRow key={i} />
            ))}
          </div>
        )}

        {matches.isError && (
          <EmptyState
            icon={<AlertTriangle size={26} />}
            title="Couldn't load matches"
            description={(matches.error as Error).message}
            action={
              <button
                onClick={() => matches.refetch()}
                className="btn-primary rounded-xl px-4 py-2 text-[13px] font-semibold"
              >
                Retry
              </button>
            }
          />
        )}

        {unscored && (
          <EmptyState
            icon={<Gauge size={26} />}
            title="Nothing ranked yet"
            description="Check validity and rank every eligible job against your profile. About twenty seconds, and it makes no API calls — the paid deep read is offered separately afterwards."
            action={
              <button
                onClick={() => setShowPrefs(true)}
                className="btn-primary rounded-xl px-4 py-2 text-[13px] font-semibold"
              >
                Open preferences & run
              </button>
            }
          />
        )}

        {matches.isSuccess && total === 0 && !unscored && (
          <EmptyState
            icon={<Gauge size={26} />}
            title="No matches for these filters"
            description="Try clearing the band filter or the search box."
            action={
              <button
                onClick={onBrowseJobs}
                className="btn-primary rounded-xl px-4 py-2 text-[13px] font-semibold"
              >
                Browse all jobs
              </button>
            }
          />
        )}

        {items.map((m) => (
          <MatchRow key={m.job.id} match={m} onSelect={() => onSelectJob(m.job.id)} />
        ))}
      </div>
    </div>

      {selectedJobId !== null && (
        <JobDrawer
          jobId={selectedJobId}
          summary={items[selectedIndex]?.job}
          onClose={() => onSelectJob(null)}
          onPrev={() => stepTo(-1)}
          onNext={() => stepTo(1)}
          hasPrev={selectedIndex > 0}
          hasNext={selectedIndex >= 0 && selectedIndex < items.length - 1}
        />
      )}
    </div>
  );
}

function Chip({
  active,
  onClick,
  label,
  count,
  dot,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  dot?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cx(
        "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] transition-colors",
        active
          ? "border-accent/40 bg-accent/12 text-accent-text"
          : "border-edge bg-panel text-muted hover:border-edge-strong",
      )}
    >
      {dot && <span className={cx("size-1.5 rounded-full", dot)} />}
      {label}
      <span className="font-mono text-[10.5px] opacity-70">{count.toLocaleString()}</span>
    </button>
  );
}

function MatchRow({ match, onSelect }: { match: Match; onSelect: () => void }) {
  const onPointerMove = useSpotlight<HTMLDivElement>();
  const band = BANDS.find((b) => b.id === match.band)!;

  // The reasons the ranker emitted, minus the bookkeeping ones the row shows
  // structurally anyway (freshness, the raw skill list).
  const reasons = useMemo(
    () =>
      match.match_reasons.filter(
        // `low_confidence` is shown as a badge; rows scored before the
        // shortlist still carry its old "routed for a full read" wording.
        (r) => !/^(fresh|recent|aging|stale|skills_matched|low_confidence):/.test(r),
      ),
    [match.match_reasons],
  );

  return (
    // A div, not a <button>: the row holds an Apply link, and an <a> inside a
    // <button> is invalid markup that browsers handle inconsistently.
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
      onPointerMove={onPointerMove}
      className={cx(
        "spotlight group relative mb-1.5 w-full cursor-pointer overflow-hidden rounded-xl border border-edge bg-panel px-3 py-2.5 text-left",
        "transition-colors duration-150 hover:border-edge-strong hover:bg-panel-strong",
      )}
    >
      <div className="flex items-start gap-3">
        {/* Fit score + band. Labelled "fit" because the Validator's validity
            score (on the tag, right) is a different 0-100 number, and an
            unlabelled 42 beside it read as a contradiction. */}
        <div
          className="flex w-14 shrink-0 flex-col items-center pt-0.5"
          title={`Fit ${match.score}/100 — how well this job matches your profile (skills, experience, role). Not the same as validity, which the Validator tag reports.`}
        >
          <span className="text-[9.5px] font-medium tracking-wide text-subtle uppercase">Fit</span>
          <span className={cx("text-[19px] leading-none font-semibold", band.tone)}>
            {match.score}
          </span>
          <span className={cx("mt-1 text-[10px] font-medium tracking-wide uppercase", band.tone)}>
            {band.label}
          </span>
        </div>

        <CompanyAvatar name={match.job.company} size={30} />

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="truncate text-[13.5px] font-semibold text-ink">
              {match.job.title}
            </span>
            {!match.confident && (
              <span
                title="This posting names too few technologies for the score to be reliable. It is not sent to the LLM automatically — open it and use “Deep read this job” if it looks worth one call."
                className="inline-flex items-center gap-1 rounded-full border border-highlight/30 bg-highlight/12 px-1.5 py-0.5 text-[10px] font-medium text-highlight"
              >
                <TriangleAlert size={9} />
                Low confidence
              </span>
            )}
            {match.shortlisted && (
              <span
                title="On the LLM shortlist: matches your preferences, scores 65+, confidently scored, verified 70+, no blocker in the JD."
                className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent-text"
              >
                <Sparkles size={9} />
                Shortlisted
              </span>
            )}
            {match.blockers.length > 0 && (
              <span
                title={`Found in the JD (free check): ${match.blockers.map((b) => b.split(":").slice(1).join(":")).join(" · ")}`}
                className="inline-flex items-center gap-1 rounded-full border border-danger/30 bg-danger/10 px-1.5 py-0.5 text-[10px] font-medium text-danger"
              >
                <X size={9} />
                {BLOCKER_LABELS[match.blockers[0]!.split(":")[0]!] ?? "Blocker"}
              </span>
            )}
            {/* Only reachable with "include preference misses" on. The badge
                names the preference that excluded it, so the row explains
                itself rather than just looking arbitrarily greyed out. */}
            {!match.prefs_pass && (
              <span
                title={`Outside your preferences: ${prefMissReasons(match).join(", ")}`}
                className="inline-flex items-center gap-1 rounded-full border border-edge-strong bg-panel px-1.5 py-0.5 text-[10px] font-medium text-subtle"
              >
                <SlidersHorizontal size={9} />
                {prefMissLabel(match)}
              </span>
            )}
            {/* The fit half of a deep read, when one exists. `blocked` is the
                thing worth interrupting for: it means the JD itself rules the
                candidate out, which no free signal can see. */}
            {match.llm_verdict?.blocked && (
              <span
                title="The JD states a hard blocker — sponsorship required, or on-site only."
                className="inline-flex items-center gap-1 rounded-full border border-danger/30 bg-danger/10 px-1.5 py-0.5 text-[10px] font-medium text-danger"
              >
                <X size={9} />
                Blocked
              </span>
            )}
          </div>

          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
            <span className="truncate">{match.job.company}</span>
            <span className="text-subtle">·</span>
            <span className="truncate">{match.job.locations[0] ?? "Remote"}</span>
            {match.years_required != null && (
              <>
                <span className="text-subtle">·</span>
                <span>{match.years_required}+ yrs asked</span>
              </>
            )}
          </div>

          {/* Matched skills, then the stacks I don't have. Both are the
              ranker's evidence — the part that stays stable run to run. */}
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            {/* USD pay leads the findings: it is the one fact here that
                answers "is this worth it" rather than "can I do it". */}
            {match.usd_pay && (
              <span
                title={`Pays in USD — ${USD_PAY_SOURCE[match.usd_pay_source ?? "jd"]}`}
                className="flex items-center gap-0.5 rounded-md border border-highlight/30 bg-highlight/12 px-1.5 py-0.5 text-[10.5px] font-medium text-highlight"
              >
                <Banknote size={10} />
                {match.usd_pay}
              </span>
            )}
            {match.matched_skills.slice(0, 7).map((s) => (
              <span
                key={s}
                className="rounded-md border border-success/25 bg-success/10 px-1.5 py-0.5 text-[10.5px] text-success"
              >
                {s}
              </span>
            ))}
            {match.matched_skills.length > 7 && (
              <span className="text-[10.5px] text-subtle">
                +{match.matched_skills.length - 7}
              </span>
            )}
            {match.missing_stacks.slice(0, 4).map((s) => (
              <span
                key={s}
                className="flex items-center gap-0.5 rounded-md border border-danger/25 bg-danger/8 px-1.5 py-0.5 text-[10.5px] text-danger"
              >
                <X size={8} />
                {s}
              </span>
            ))}
          </div>

          {reasons.length > 0 && (
            <p className="mt-1.5 truncate text-[11px] text-subtle">{reasons.join(" · ")}</p>
          )}
        </div>

        {/* subscore breakdown, so a placement can be argued with */}
        <div className="hidden shrink-0 flex-col gap-0.5 pt-0.5 text-right sm:flex">
          {(["skill", "years", "family", "india", "fresh", "penalty"] as const).map((k) => {
            const v = match.subscores[k];
            if (v == null || v === 0) return null;
            return (
              <div key={k} className="flex items-center justify-end gap-1.5 text-[10px]">
                <span className="text-subtle">{k}</span>
                <span
                  className={cx(
                    "w-6 text-right font-mono",
                    v < 0 ? "text-danger" : "text-muted",
                  )}
                >
                  {v > 0 ? `+${v}` : v}
                </span>
              </div>
            );
          })}
        </div>

        {/* Posted + checks + Apply. Falls back to first-seen when the board
            gave no posting date, same as the Jobs table. */}
        <div className="flex w-[136px] shrink-0 flex-col items-end gap-1.5 pt-0.5">
          <span className="flex items-center gap-1">
            <VerifierBadge
              score={match.job.validity_score}
              reasons={match.job.validity_reasons}
              checkedAt={match.job.validity_checked_at}
            />
            <LlmBadge read={match.llm_read} />
          </span>
          <span
            className="flex items-center gap-1 text-[11.5px] whitespace-nowrap text-muted tabular-nums"
            title={
              match.job.posted_at
                ? `Posted ${absoluteDate(match.job.posted_at)}`
                : `Posting date not stated — first seen ${absoluteDate(match.job.first_seen_at)}`
            }
          >
            <Clock size={11} className="text-subtle" />
            {relativeTime(match.job.posted_at ?? match.job.first_seen_at)}
          </span>
          <a
            href={match.job.apply_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
            className="flex items-center gap-1 rounded-lg border border-edge bg-panel px-2.5 py-1 text-[11.5px] font-semibold text-muted opacity-70 transition-all duration-200 group-hover:opacity-100 hover:border-accent/45 hover:bg-accent-soft hover:text-accent-text focus-visible:opacity-100"
          >
            Apply
            <ArrowUpRight size={12} />
          </a>
        </div>
      </div>
    </div>
  );
}

/** Shown when `profile.yaml` is missing — the one failure the panel can't
 *  recover from on its own, since matching has nothing to score against. */
export function MatchesPlaceholder({ onBrowseJobs }: { onBrowseJobs: () => void }) {
  return (
    <EmptyState
      icon={<AlertTriangle size={26} />}
      title="No profile found"
      description="Matching needs backend/profile.yaml. Create it, then re-score."
      action={
        <button
          onClick={onBrowseJobs}
          className="btn-primary rounded-xl px-4 py-2 text-[13px] font-semibold"
        >
          Browse all jobs
        </button>
      }
    />
  );
}

/* ------------------------------------------------------- preference reasons */

/** Human labels for the `prefs_reasons` strings the gate emits.
 *
 *  The gate records reasons for passes as well as misses (so the drawer can
 *  say "admitted because it stated no salary"), so this filters to the ones
 *  that actually excluded the row. The raw strings carry their evidence after
 *  a colon — `workplace_mismatch:onsite` — which is kept in the tooltip. */
/** Short labels for `blockers.py` keys. */
const BLOCKER_LABELS: Record<string, string> = {
  us_work_authorization: "US work auth",
  no_visa_sponsorship: "No sponsorship",
  us_citizenship: "US citizens only",
  security_clearance: "Clearance",
  remote_region_only: "Region-only remote",
  candidate_region_only: "Region-only",
};

const PREF_MISS_LABELS: Record<string, string> = {
  not_transferred: "not sent from Jobs",
  posted_too_old: "posted too long ago",
  workplace_mismatch: "wrong work mode",
  workplace_unstated: "work mode not stated",
  employment_mismatch: "wrong commitment",
  employment_unstated: "commitment not stated",
  pay_below_floor: "below pay floor",
  pay_unstated: "pay not stated",
  years_out_of_band: "experience out of band",
  years_unstated: "experience not stated",
  validity_below: "low validity",
  company_excluded: "company excluded",
  ats_excluded: "source excluded",
};

function prefMissReasons(match: Match): string[] {
  return match.prefs_reasons.filter((r) => PREF_MISS_LABELS[r.split(":")[0] ?? ""]);
}

function prefMissLabel(match: Match): string {
  const first = prefMissReasons(match)[0];
  if (!first) return "outside preferences";
  const [key = "", evidence] = first.split(":");
  const label = PREF_MISS_LABELS[key] ?? key;
  return evidence ? `${label} (${evidence})` : label;
}
