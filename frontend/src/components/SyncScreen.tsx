import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  Clock,
  Loader2,
  RadarIcon,
  XCircle,
} from "lucide-react";
import type { IngestStatus, SourceState } from "@/lib/types";
import { cx } from "./primitives";

const STATE_META: Record<SourceState, { icon: typeof Circle; tone: string; label: string }> = {
  pending: { icon: Circle, tone: "text-subtle/60", label: "Queued" },
  fetching: { icon: Loader2, tone: "text-accent-text", label: "Fetching…" },
  done: { icon: CheckCircle2, tone: "text-mint", label: "Done" },
  failed: { icon: XCircle, tone: "text-danger", label: "Failed" },
  // Remotive's terms cap us at ~4 calls/day, so a skipped board is a correct
  // outcome, not a problem — it reads as neutral, never as an error.
  throttled: { icon: Clock, tone: "text-subtle", label: "Skipped — rate limit" },
};

function SourceRow({
  company,
  ats,
  state,
  fetched,
  error,
}: {
  company: string;
  ats: string;
  state: SourceState;
  fetched: number;
  error: string | null;
}) {
  const { icon: Icon, tone, label } = STATE_META[state];

  return (
    <li
      className={cx(
        "flex items-center gap-2.5 py-1.5 text-[13px] transition-opacity duration-300",
        state === "pending" && "opacity-55",
      )}
    >
      <Icon
        size={15}
        className={cx("shrink-0", tone, state === "fetching" && "animate-spin")}
        strokeWidth={state === "pending" ? 1.6 : 2}
      />
      <span className="min-w-0 flex-1 truncate text-ink">
        {company}
        <span className="ml-1.5 text-[11px] text-subtle capitalize">{ats}</span>
      </span>
      <span className={cx("shrink-0 text-[12px] tabular-nums", tone)} title={error ?? undefined}>
        {state === "done" ? `${fetched.toLocaleString()} jobs` : label}
      </span>
    </li>
  );
}

/**
 * Covers the dashboard until the day's sweep lands.
 *
 * Deliberately renders no job data: the point of the on-load sweep is that what
 * you read is what the boards say right now, so yesterday's rows never flash up
 * first. The per-source list is what makes the wait legible — 90 seconds of
 * bare spinner feels broken, 90 seconds of boards ticking over does not.
 */
export function SyncScreen({
  status,
  error,
  onSkip,
  onRetry,
}: {
  status: IngestStatus | null;
  error: Error | null;
  onSkip: () => void;
  onRetry: () => void;
}) {
  const total = status?.sources_total ?? 0;
  const done = status?.sources_done ?? 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const sources = status?.sources ?? [];

  return (
    <div className="flex flex-1 items-center justify-center overflow-y-auto py-4">
      <div
        className={cx(
          "glass-strong glass-sheen animate-fade-up relative w-full max-w-md overflow-hidden rounded-3xl px-6 py-7",
          // The rim turns while work is in flight, then settles — the border
          // itself reports whether anything is still happening.
          !error && "rim rim-on",
        )}
      >
        <div className="relative flex flex-col items-center text-center">
          <span
            className={cx(
              "relative grid size-14 place-items-center overflow-hidden rounded-2xl text-white",
              error
                ? "bg-gradient-to-br from-danger to-danger/70"
                : "bg-gradient-to-br from-accent to-accent-strong",
              "shadow-[inset_0_1px_0_oklch(100%_0_0_/_0.3),0_12px_32px_-12px_var(--accent-glow)]",
              !error && "animate-pulse-ring",
            )}
          >
            <span
              aria-hidden
              className="pointer-events-none absolute inset-x-0 top-0 h-1/2 bg-gradient-to-b from-white/25 to-transparent"
            />
            {error ? (
              <AlertTriangle size={24} className="relative" />
            ) : (
              <RadarIcon size={24} strokeWidth={2.2} className="relative" />
            )}
          </span>

          <h2 className="mt-4 text-[18px] font-semibold tracking-[-0.02em] text-ink">
            {error ? "Couldn't fetch the latest jobs" : "Fetching the latest jobs"}
          </h2>
          <p className="mt-1.5 max-w-xs text-[13px] leading-relaxed text-muted">
            {error
              ? error.message
              : total > 0
                ? `Sweeping ${total} ${total === 1 ? "board" : "boards"} — this runs once a day, so tomorrow is the next one.`
                : "Contacting the boards…"}
          </p>
        </div>

        {total > 0 && !error && (
          <>
            <div className="relative mt-5 flex items-center gap-3">
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-panel-strong shadow-[inset_0_1px_2px_oklch(0%_0_0_/_0.25)]">
                <div
                  className="relative h-full overflow-hidden rounded-full bg-gradient-to-r from-accent-strong to-accent transition-[width] duration-500 ease-out"
                  style={{ width: `${Math.max(pct, 4)}%` }}
                >
                  {/* A light travelling along the filled portion, so the bar
                      still reads as alive between source completions. */}
                  <span
                    aria-hidden
                    className="absolute inset-0 -translate-x-full animate-[shimmer_1.8s_infinite] bg-gradient-to-r from-transparent via-white/50 to-transparent"
                  />
                </div>
              </div>
              <span className="shrink-0 text-[12px] font-medium text-subtle tabular-nums">
                {done}/{total}
              </span>
            </div>

            <ul className="relative mt-3 max-h-64 divide-y divide-edge overflow-y-auto pr-1">
              {sources.map((source) => (
                <SourceRow
                  key={source.source_id}
                  company={source.company}
                  ats={source.ats}
                  state={source.state}
                  fetched={source.fetched}
                  error={source.error}
                />
              ))}
            </ul>
          </>
        )}

        <div className="relative mt-5 flex items-center justify-center gap-2">
          {error && (
            <button
              onClick={onRetry}
              className="btn-primary rounded-xl px-4 py-2 text-[13px] font-semibold"
            >
              Try again
            </button>
          )}
          <button
            onClick={onSkip}
            className="rounded-xl border border-edge bg-panel px-4 py-2 text-[13px] font-medium text-muted transition-colors hover:border-edge-strong hover:bg-panel-hover hover:text-ink"
          >
            {error ? "Show stored jobs" : "Skip the wait"}
          </button>
        </div>
      </div>
    </div>
  );
}
