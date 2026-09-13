import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import type { FunnelStep } from "@/lib/types";
import { cx } from "./primitives";

/**
 * One chain of numbers, each step saying what it removed.
 *
 * Built because the dashboard showed 5,481, 740, "430 of 910" and a deep read
 * of 427 with nothing connecting them. Every count here comes from the same
 * filter builder as the list beneath it, so the last step is the list's size.
 */
export function Funnel({
  steps,
  loading,
  action,
  footnote,
}: {
  steps: FunnelStep[] | undefined;
  loading?: boolean;
  action?: ReactNode;
  footnote?: ReactNode;
}) {
  return (
    <div className="glass relative z-10 shrink-0 rounded-2xl px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-1 gap-y-2">
        {loading && !steps
          ? Array.from({ length: 4 }, (_, i) => (
              <div key={i} className="skeleton h-9 w-24 rounded-xl" />
            ))
          : steps?.map((step, index) => (
              <div key={step.key} className="flex items-center gap-1">
                {index > 0 && <ChevronRight size={14} className="shrink-0 text-subtle/60" />}
                <Step step={step} first={index === 0} last={index === steps.length - 1} />
              </div>
            ))}
        {action && <div className="ml-auto flex items-center gap-2 pl-2">{action}</div>}
      </div>
      {footnote && <div className="mt-2 border-t border-edge pt-2 text-[11.5px] text-subtle">{footnote}</div>}
    </div>
  );
}

function Step({ step, first, last }: { step: FunnelStep; first: boolean; last: boolean }) {
  const breakdown = Object.entries(step.breakdown ?? {});
  const title = [
    `${step.label}: ${step.count.toLocaleString()}`,
    !first && step.dropped > 0 ? `removed ${step.dropped.toLocaleString()}` : null,
    ...breakdown.map(([reason, n]) => `  ${n.toLocaleString()} — ${reason}`),
    step.note ?? null,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <div
      title={title}
      className={cx(
        "flex min-w-0 flex-col rounded-xl border px-2.5 py-1.5 leading-tight",
        last ? "border-accent/40 bg-accent-soft" : "border-edge bg-panel",
      )}
    >
      <span className="truncate text-[10.5px] font-medium tracking-[0.04em] text-subtle uppercase">
        {step.label}
      </span>
      <span className="flex items-baseline gap-1.5">
        <span
          className={cx(
            "text-[15px] font-semibold tabular-nums",
            last ? "text-accent-text" : "text-ink",
          )}
        >
          {step.count.toLocaleString()}
        </span>
        {!first && step.dropped > 0 && (
          <span className="text-[11px] text-danger/80 tabular-nums">
            −{step.dropped.toLocaleString()}
          </span>
        )}
      </span>
      {breakdown.length > 0 && (
        <span className="max-w-[220px] truncate text-[10.5px] text-subtle">
          {breakdown
            .slice(0, 2)
            .map(([reason, n]) => `${n} ${reason}`)
            .join(" · ")}
          {breakdown.length > 2 ? " …" : ""}
        </span>
      )}
      {step.note && breakdown.length === 0 && (
        <span className="max-w-[220px] truncate text-[10.5px] text-subtle">{step.note}</span>
      )}
    </div>
  );
}
