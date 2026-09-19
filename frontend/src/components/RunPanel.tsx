import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  BrainCircuit,
  Check,
  Gauge,
  Loader2,
  Play,
  ShieldCheck,
  X,
} from "lucide-react";

import {
  fetchLlmEstimate,
  fetchMatchesFunnel,
  llmStatus,
  pipelineStatus,
  runPipeline,
  startLlmScreen,
} from "@/lib/api";
import type { LlmEstimate, LlmStatus, PipelineStage, PipelineStatus } from "@/lib/types";
import { cx } from "./primitives";

/** Per-pass read limits offered. The server clamps anything above 50. */
const READ_CAPS = [10, 15, 20, 50] as const;

/**
 * The Run control: one click, three free stages, then a price.
 *
 *   validity  ->  ranking  ->  estimate  ->  [STOP, ask]  ->  deep read
 *
 * The stop is the whole design. The first three stages are pure CPU and
 * finish in about twenty seconds; the deep read is a multi-hour pass against
 * a measured 8,000 token/minute ceiling that spends a large share of a
 * 1,000-request daily budget. Chaining straight into it would let one click
 * commit hours of billing silently, so the estimate is shown and the user
 * confirms.
 *
 * The estimate is computed from the real prompts that would be sent, not from
 * a per-row average — JD length varies by an order of magnitude across
 * boards, so an average misprices a pass by minutes.
 */

const STAGES: { id: PipelineStage; label: string; icon: React.ReactNode }[] = [
  { id: "validity", label: "Checking validity", icon: <ShieldCheck size={13} /> },
  { id: "ranking", label: "Ranking against your profile", icon: <Gauge size={13} /> },
  { id: "estimating", label: "Pricing the deep read", icon: <BrainCircuit size={13} /> },
];

const ORDER: PipelineStage[] = ["validity", "ranking", "estimating", "done"];

function stageState(
  current: PipelineStage,
  stage: PipelineStage,
): "pending" | "active" | "done" {
  const a = ORDER.indexOf(stage);
  const b = ORDER.indexOf(current);
  if (current === "failed") return a <= b ? "done" : "pending";
  if (b > a) return "done";
  if (b === a) return "active";
  return "pending";
}

export function RunPanel({
  /** Bumped by the caller after a preferences save, to surface the nudge. */
  prefsDirtySince,
  /** Unsaved preference edits exist; the button becomes "Save & run". */
  unsaved = false,
  /** Runs before the pipeline starts — the dialog saves pending edits here,
   *  so Run never gates on preferences the user can see but has not saved. */
  beforeRun,
  onFinished,
  onRunningChange,
  /** A run finished with a deep read to price — the dialog reopens for it. */
  onNeedsConfirm,
  /** A deep read confirmed here has finished — the dialog closes on it. */
  onDeepReadDone,
}: {
  prefsDirtySince: number;
  unsaved?: boolean;
  beforeRun?: () => Promise<unknown>;
  onFinished: () => void;
  onRunningChange?: (running: boolean) => void;
  onNeedsConfirm?: () => void;
  onDeepReadDone?: () => void;
}) {
  const qc = useQueryClient();
  const [polling, setPolling] = useState(false);
  const [confirming, setConfirming] = useState<LlmEstimate | null>(null);
  // The finished deep read's summary stays up until dismissed or a new run.
  // Keyed on the read's `finished_at`, not on having watched it finish: a
  // panel that mounted after the read (a reload) used to show nothing, and
  // the line below still said "25 on the LLM shortlist" as if none were read.
  const [dismissedReadAt, setDismissedReadAt] = useState<string | null>(null);
  const lastLlmState = useRef<LlmStatus["state"] | undefined>(undefined);
  const lastSeenPrefs = useRef(prefsDirtySince);
  const [nudge, setNudge] = useState(false);

  useEffect(() => {
    if (prefsDirtySince !== lastSeenPrefs.current) {
      lastSeenPrefs.current = prefsDirtySince;
      setNudge(true);
    }
  }, [prefsDirtySince]);

  const status = useQuery<PipelineStatus>({
    queryKey: ["pipeline"],
    queryFn: pipelineStatus,
    // Poll while a run is live, judged by what the SERVER last said as well as
    // by local state. Keyed on `polling` alone, a panel that mounted (reload,
    // HMR) while a run was in flight read "validity" once and never asked
    // again, so it spun on "Checking validity" after the run had finished.
    refetchInterval: (query) => {
      const s = query.state.data?.stage;
      return polling || s === "validity" || s === "ranking" || s === "estimating"
        ? 1000
        : false;
    },
  });

  const stage = status.data?.stage ?? "idle";
  const running = stage === "validity" || stage === "ranking" || stage === "estimating";
  const lastStage = useRef<PipelineStage | undefined>(undefined);

  useEffect(() => {
    onRunningChange?.(running || polling);
  }, [running, polling, onRunningChange]);

  useEffect(() => {
    const previous = lastStage.current;
    lastStage.current = stage;
    // Finishes on either signal: a run this panel started, or one it watched
    // go from live to finished after mounting mid-run.
    const wasLive =
      polling || previous === "validity" || previous === "ranking" || previous === "estimating";
    if (!wasLive) return;
    if (stage === "done" || stage === "failed") {
      setPolling(false);
      for (const key of ["matches", "matches-funnel", "jobs", "jobs-funnel", "facets"]) {
        qc.invalidateQueries({ queryKey: [key] });
      }
      onFinished();
      const estimate = status.data?.estimate;
      // Only ask when there is something to buy.
      if (stage === "done" && estimate && estimate.pending > 0 && estimate.configured) {
        setConfirming(estimate);
        onNeedsConfirm?.();
      }
    }
  }, [polling, stage, qc, onFinished, onNeedsConfirm, status.data?.estimate]);

  // Read once on mount — so a reload mid-pass picks the progress back up —
  // and every 2s while the pass runs. Without this poll the dashboard never
  // learned that a deep read had finished: the spinner ran forever and the
  // Matches list kept showing pre-read rows.
  //
  // `watchingLlm` keeps the poll on for a pass this panel confirmed, whatever
  // the POST answered: keyed on `state === "running"` alone, a POST that
  // answered before the pass had started ("idle") switched polling off for
  // good, so the list never refreshed and the dialog never closed.
  const [watchingLlm, setWatchingLlm] = useState(false);
  // `finished_at` of the pass before the confirmed one — a "done" still
  // carrying it describes the old pass, not this one.
  const llmBaseline = useRef<string | null>(null);
  const llm = useQuery<LlmStatus>({
    queryKey: ["llm"],
    queryFn: llmStatus,
    refetchInterval: (query) =>
      watchingLlm || query.state.data?.state === "running" ? 2000 : false,
  });
  const llmState = llm.data?.state;
  const llmFinishedAt = llm.data?.finished_at ?? null;
  const llmRunning = llmState === "running" || (watchingLlm && llmState !== "done");

  useEffect(() => {
    const previous = lastLlmState.current;
    lastLlmState.current = llmState;
    const watchedDone =
      watchingLlm && llmState === "done" && llmFinishedAt !== llmBaseline.current;
    const seenDone = previous === "running" && llmState === "done";
    if (!watchedDone && !seenDone) return;
    for (const key of [
      "matches",
      "matches-funnel",
      "jobs",
      "jobs-funnel",
      "facets",
      "job",
      "llm-estimate",
    ]) {
      qc.invalidateQueries({ queryKey: [key] });
    }
    if (watchingLlm) {
      setWatchingLlm(false);
      onDeepReadDone?.();
    }
  }, [llmState, llmFinishedAt, watchingLlm, qc, onDeepReadDone]);

  // Live, unlike `ranking` below, which is the Run's snapshot and never moves
  // after a deep read. Same key as the Matches funnel, so it shares that cache
  // and the invalidation above refreshes both.
  const funnel = useQuery({
    queryKey: ["matches-funnel"],
    queryFn: fetchMatchesFunnel,
    staleTime: 30_000,
  });

  const start = useMutation({
    mutationFn: async () => {
      await beforeRun?.();
      return runPipeline(false);
    },
    onSuccess: (data) => {
      setNudge(false);
      // Seed the cache with the live stage first: left holding the previous
      // run's "done", the finish effect would end this run before it began.
      qc.setQueryData(["pipeline"], data);
      setPolling(true);
    },
  });

  const deepRead = useMutation({
    mutationFn: (limit: number) => {
      llmBaseline.current = llm.data?.finished_at ?? null;
      return startLlmScreen({ limit });
    },
    onSuccess: (data) => {
      setConfirming(null);
      qc.setQueryData(["llm"], data);
      setWatchingLlm(true);
    },
  });
  const llmResult = llm.data?.result;
  const readAt = llm.data?.finished_at ?? null;
  const runAt = status.data?.finished_at ?? null;
  // A read older than the last Run describes a ranking that no longer exists.
  const showLlmSummary =
    llmState === "done" &&
    !running &&
    readAt !== null &&
    readAt !== dismissedReadAt &&
    (runAt === null || readAt > runAt);

  const ranking = status.data?.ranking;

  return (
    <div className="relative">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 md:px-5">
        <button
          type="button"
          disabled={running || start.isPending}
          onClick={() => start.mutate()}
          className={cx(
            "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px] font-medium transition-colors",
            running || start.isPending
              ? "cursor-not-allowed border border-edge bg-panel text-subtle"
              : nudge
                ? "bg-highlight text-white hover:bg-highlight/90"
                : "bg-accent text-white hover:bg-accent/90",
          )}
        >
          {running || start.isPending ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <Play size={13} />
          )}
          {running ? "Running" : unsaved ? "Save & run matching" : "Run matching"}
        </button>

        {start.isError && (
          <span className="text-[11.5px] text-danger">{(start.error as Error).message}</span>
        )}

        {nudge && !running && !unsaved && (
          <span className="text-[11.5px] text-highlight">
            Preferences changed — run to apply them
          </span>
        )}

        {!running && stage === "done" && ranking && (
          <div className="text-[11.5px] text-subtle">
            <span className="text-ink">{ranking.matching_prefs.toLocaleString()}</span> of{" "}
            {ranking.in_transfer.toLocaleString()} jobs match your preferences
            {" · "}
            <span className="text-ink">{ranking.shortlisted.toLocaleString()}</span> on the LLM
            shortlist
            {funnel.data && !funnel.data.stale && funnel.data.read > 0 && (
              <>
                {" · "}
                <span className="text-ink">{funnel.data.read.toLocaleString()}</span> read by the
                LLM
              </>
            )}
            {/* Every count here is over the same jobs as the first number.
                The whole-board validity tally used to sit on this line and
                read as if it described the jobs you sent. */}
            {ranking.blocked ? (
              <span title="The job description itself rules you out: US work authorization, no visa sponsorship, US citizens only, security clearance, or remote only within a region that excludes India.">
                {" · "}
                {ranking.blocked.toLocaleString()} blocked by the JD
              </span>
            ) : null}
            {ranking.low_validity ? (
              <span title="The verifier scored these below 70 — stale, evergreen or thin postings.">
                {" · "}
                {ranking.low_validity.toLocaleString()} low-validity
              </span>
            ) : null}
          </div>
        )}

        {llmRunning && llm.data && (
          <span className="flex min-w-0 items-center gap-1.5 text-[11.5px] text-accent-text">
            <Loader2 size={12} className="shrink-0 animate-spin" />
            <span className="shrink-0">
              Deep read {llm.data.done.toLocaleString()}/{llm.data.total.toLocaleString()}
              {" · "}
              {llm.data.tokens.toLocaleString()} tokens
              {llm.data.failed > 0 && ` · ${llm.data.failed} failed`}
            </span>
            {llm.data.current && (
              <span className="truncate text-subtle">— {llm.data.current}</span>
            )}
          </span>
        )}
      </div>

      {/* ------------------------------------------- finished deep read summary */}
      {showLlmSummary && (
        <div
          className={cx(
            "flex items-start gap-2 border-t border-edge px-4 py-2.5 text-[12px] md:px-5",
            llm.data?.error ? "bg-danger/10 text-danger" : "text-ink",
          )}
        >
          {llm.data?.error ? (
            <AlertTriangle size={14} className="mt-px shrink-0" />
          ) : (
            <Check size={14} className="mt-px shrink-0 text-success" />
          )}
          <span>
            {llmResult ? (
              <>
                Deep read finished: {llmResult.screened.toLocaleString()} read
                {llmResult.failed > 0 && `, ${llmResult.failed} failed`}
                {llmResult.cached > 0 && `, ${llmResult.cached.toLocaleString()} already cached`}
                {llmResult.skipped_budget > 0 &&
                  `, ${llmResult.skipped_budget.toLocaleString()} left for a later run`}
                {" · "}
                {llmResult.tokens.toLocaleString()} tokens ·{" "}
                {(llmResult.duration_ms / 60000).toFixed(1)} min
                {Object.keys(llmResult.bands).length > 0 && (
                  <span className="block text-subtle">
                    {Object.entries(llmResult.bands)
                      .map(([band, n]) => `${n} ${band}`)
                      .join(" · ")}
                    {llmResult.blocked > 0 && ` · ${llmResult.blocked} blocked by the JD`}
                    {(llmResult.statuses.ghost ?? 0) > 0 &&
                      ` · ${llmResult.statuses.ghost} likely ghost`}
                    {" — see them in the Matches list"}
                  </span>
                )}
              </>
            ) : (
              "Deep read finished"
            )}
            {llm.data?.error && <> — stopped: {llm.data.error}</>}
          </span>
          <button
            type="button"
            onClick={() => setDismissedReadAt(readAt)}
            aria-label="Dismiss"
            className="ml-auto rounded-md p-0.5 text-subtle transition-colors hover:text-ink"
          >
            <X size={13} />
          </button>
        </div>
      )}

      {/* ------------------------------------------------------- live stages */}
      {running && (
        <div className="border-t border-edge px-4 py-2.5 md:px-5">
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            {STAGES.map((s) => {
              const state = stageState(stage, s.id);
              return (
                <div
                  key={s.id}
                  className={cx(
                    "flex items-center gap-1.5 text-[11.5px]",
                    state === "done"
                      ? "text-success"
                      : state === "active"
                        ? "text-ink"
                        : "text-subtle",
                  )}
                >
                  {state === "done" ? (
                    <Check size={13} />
                  ) : state === "active" ? (
                    <Loader2 size={13} className="animate-spin" />
                  ) : (
                    s.icon
                  )}
                  {s.label}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {status.data?.error && (
        <div className="flex items-start gap-2 border-t border-edge bg-danger/10 px-4 py-2.5 text-[12px] text-danger md:px-5">
          <AlertTriangle size={14} className="mt-px shrink-0" />
          <span>{status.data.error}</span>
        </div>
      )}

      {/* ------------------------------------------------- the cost confirm */}
      {confirming && (
        <CostConfirm
          estimate={confirming}
          busy={deepRead.isPending}
          error={deepRead.error as Error | null}
          onCancel={() => setConfirming(null)}
          onConfirm={(limit) => deepRead.mutate(limit)}
        />
      )}
    </div>
  );
}

/**
 * The confirmation. States what it will spend in rows, wall-clock minutes and
 * share of the provider's long TOKEN cap, plus how many rows that cap can still
 * hold. On Groq the cap is 200k tokens a DAY — ~110 rows — so a 400-row pass is
 * a multi-day job, and "~2 hours" alone would hide that the pass stops once the
 * cap runs out and resumes on a later run. On Mistral the cap is per MONTH.
 */
function CostConfirm({
  estimate: initial,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  estimate: LlmEstimate;
  busy: boolean;
  error: Error | null;
  onCancel: () => void;
  onConfirm: (limit: number) => void;
}) {
  // How many shortlisted jobs this pass may read. Re-priced by the server for
  // each choice, from the real prompts, rather than scaled client-side.
  const [cap, setCap] = useState(initial.cap || 15);
  const repriced = useQuery({
    queryKey: ["llm-estimate", cap],
    queryFn: () => fetchLlmEstimate(cap),
    enabled: cap !== initial.cap,
    staleTime: 30_000,
  });
  const estimate = cap === initial.cap ? initial : (repriced.data ?? initial);
  const hasCap = estimate.token_cap > 0;
  const heavy = estimate.cap_budget_pct >= 50 || estimate.est_minutes >= 60;
  const overflows = hasCap && estimate.fits_in_cap < estimate.pending;
  const left = Math.max(0, estimate.token_cap - estimate.tokens_used);
  // "today" / "this month", and the window the used figure was counted over.
  const period = estimate.token_cap_period === "month" ? "month" : "day";
  const periodWord = period === "month" ? "this month" : "today";
  const windowWord = period === "month" ? "the last 30 days" : "the last 24h";
  return (
    <div className="border-t border-edge bg-panel/60 px-4 py-3.5 md:px-5">
      <div className="mb-2 flex items-center gap-2">
        <span className="grid size-6 place-items-center rounded-md bg-accent/12 text-accent-text">
          <BrainCircuit size={13} />
        </span>
        <div className="text-[12.5px] font-semibold text-ink">
          Deep read {estimate.pending.toLocaleString()} job
          {estimate.pending === 1 ? "" : "s"}?
        </div>
        <button
          type="button"
          onClick={onCancel}
          aria-label="Dismiss"
          className="ml-auto rounded-md p-1 text-subtle transition-colors hover:text-ink"
        >
          <X size={14} />
        </button>
      </div>

      <p className="mb-2.5 text-[11.5px] leading-relaxed text-muted">
        Ranking and the verifier are done, and cost nothing. The LLM only reads the shortlist —
        {" "}
        {estimate.routed.toLocaleString()} job{estimate.routed === 1 ? "" : "s"} that pass every
        free check — best score first, and never more than you pick here.
      </p>

      <div className="mb-3 flex flex-wrap items-center gap-1.5 text-[11.5px]">
        <span className="mr-1 text-subtle">Read at most</span>
        {READ_CAPS.map((n) => (
          <button
            key={n}
            type="button"
            onClick={() => setCap(n)}
            aria-pressed={cap === n}
            className={cx(
              "rounded-lg border px-2.5 py-1 font-medium tabular-nums transition-colors",
              cap === n
                ? "border-accent bg-accent/15 text-accent-text"
                : "border-edge bg-panel text-muted hover:border-edge-strong hover:text-ink",
            )}
          >
            {n}
          </button>
        ))}
        <span className="ml-1 text-subtle">jobs this pass</span>
        {repriced.isFetching && <Loader2 size={12} className="animate-spin text-subtle" />}
      </div>

      <div className="mb-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11.5px] sm:grid-cols-4">
        <Stat label="Jobs to read" value={estimate.pending.toLocaleString()} />
        <Stat
          label="Est. time"
          value={`~${estimate.est_minutes < 1 ? "<1" : Math.round(estimate.est_minutes)} min`}
          tone={estimate.est_minutes >= 60 ? "warn" : undefined}
        />
        <Stat
          label={`Of ${period === "month" ? "monthly" : "daily"} tokens`}
          value={hasCap ? `${estimate.cap_budget_pct}%` : "—"}
          tone={estimate.cap_budget_pct >= 50 ? "warn" : undefined}
        />
        <Stat
          label={`Fits ${periodWord}`}
          value={`${estimate.fits_in_cap.toLocaleString()} / ${estimate.pending.toLocaleString()}`}
          tone={overflows ? "warn" : undefined}
        />
      </div>

      <p className="mb-2.5 text-[11px] text-subtle">
        {hasCap && (
          <>
            {estimate.tokens_used.toLocaleString()} of {estimate.token_cap.toLocaleString()} tokens
            used in {windowWord} · {left.toLocaleString()} left ·{" "}
          </>
        )}
        {estimate.cached.toLocaleString()} already read
        {estimate.over_cap > 0 && (
          <> · {estimate.over_cap.toLocaleString()} more wait for a later pass</>
        )}
      </p>

      {overflows ? (
        <p className="mb-2.5 flex items-start gap-1.5 text-[11.5px] text-highlight">
          <AlertTriangle size={13} className="mt-px shrink-0" />
          Only {estimate.fits_in_cap.toLocaleString()} of these fit in {periodWord}'s token
          allowance. The pass reads the best-ranked first, stops cleanly when the cap is reached,
          and the rest are picked up by a later run — nothing already read is paid for twice.
        </p>
      ) : (
        heavy && (
          <p className="mb-2.5 flex items-start gap-1.5 text-[11.5px] text-highlight">
            <AlertTriangle size={13} className="mt-px shrink-0" />
            This is a long pass. It runs in the background and you can keep using the dashboard.
          </p>
        )
      )}

      {error && (
        <p className="mb-2.5 flex items-start gap-1.5 text-[11.5px] text-danger">
          <AlertTriangle size={13} className="mt-px shrink-0" />
          {error.message}
        </p>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={busy || estimate.pending === 0}
          onClick={() => onConfirm(cap)}
          className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-[12px] font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy ? <Loader2 size={13} className="animate-spin" /> : <BrainCircuit size={13} />}
          Read {estimate.pending.toLocaleString()} job{estimate.pending === 1 ? "" : "s"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg border border-edge bg-panel px-3 py-1.5 text-[12px] text-muted transition-colors hover:border-edge-strong hover:text-ink"
        >
          Not now
        </button>
        <span className="text-[11px] text-subtle">
          {estimate.provider ? `${estimate.provider} · ` : ""}
          {estimate.model}
        </span>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "warn";
}) {
  return (
    <div>
      <div className="text-subtle">{label}</div>
      <div className={cx("font-medium", tone === "warn" ? "text-highlight" : "text-ink")}>
        {value}
      </div>
    </div>
  );
}
