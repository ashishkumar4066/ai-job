import type { ReactNode } from "react";
import { Briefcase, Globe2, ListFilter, Sparkles, TrendingUp } from "lucide-react";
import type { Facets } from "@/lib/types";
import { cx } from "./primitives";

export function StatsStrip({
  facets,
  matching,
  loading,
  hasLastVisit,
  newOnlyActive,
  onToggleNewOnly,
}: {
  facets: Facets | undefined;
  matching: number;
  loading: boolean;
  hasLastVisit: boolean;
  newOnlyActive: boolean;
  onToggleNewOnly: () => void;
}) {
  const totals = facets?.totals;

  return (
    // On phones this scrolls sideways as one row instead of stacking into three,
    // which would leave the job list almost no vertical space.
    <div className="flex shrink-0 gap-2.5 overflow-x-auto pb-1 sm:grid sm:grid-cols-3 sm:overflow-visible sm:pb-0 lg:grid-cols-5">
      <Stat
        icon={<ListFilter size={14} />}
        label="Matching"
        value={matching}
        loading={loading}
        accent
      />
      <Stat icon={<Briefcase size={14} />} label="Open roles" value={totals?.open} loading={loading} />
      <Stat icon={<Globe2 size={14} />} label="Remote" value={totals?.remote} loading={loading} />
      <Stat
        icon={<TrendingUp size={14} />}
        label="Posted this week"
        value={totals?.posted_last_7d}
        loading={loading}
      />
      <Stat
        icon={<Sparkles size={14} />}
        label={hasLastVisit ? "New since last visit" : "First visit"}
        value={hasLastVisit ? totals?.new_since : undefined}
        placeholder={hasLastVisit ? undefined : "All new"}
        loading={loading}
        tone="highlight"
        onClick={hasLastVisit && (totals?.new_since ?? 0) > 0 ? onToggleNewOnly : undefined}
        active={newOnlyActive}
      />
    </div>
  );
}

function Stat({
  icon,
  label,
  value,
  placeholder,
  loading,
  accent,
  tone,
  onClick,
  active,
}: {
  icon: ReactNode;
  label: string;
  value: number | undefined;
  placeholder?: string;
  loading: boolean;
  accent?: boolean;
  tone?: "highlight";
  onClick?: () => void;
  active?: boolean;
}) {
  const Element = onClick ? "button" : "div";

  return (
    <Element
      onClick={onClick}
      className={cx(
        "glass glass-sheen relative min-w-[148px] shrink-0 rounded-2xl px-3.5 py-3 text-left transition-all duration-150 sm:min-w-0",
        onClick && "cursor-pointer hover:bg-panel-hover active:scale-[0.99]",
        active && "border-highlight/40 bg-highlight/10",
      )}
    >
      <p className="flex items-center gap-1.5 text-[11px] font-medium tracking-wide text-subtle uppercase">
        <span className={cx(tone === "highlight" ? "text-highlight" : "opacity-60")}>{icon}</span>
        <span className="truncate">{label}</span>
      </p>
      {loading && value === undefined && !placeholder ? (
        <div className="skeleton mt-2 h-6 w-16 rounded-lg" />
      ) : (
        <p
          className={cx(
            "mt-1 text-[22px] leading-none font-semibold tracking-tight tabular-nums",
            accent ? "text-gradient" : tone === "highlight" ? "text-highlight" : "text-ink",
          )}
        >
          {value !== undefined ? value.toLocaleString() : (placeholder ?? "—")}
        </p>
      )}
    </Element>
  );
}
