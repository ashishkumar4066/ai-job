import type { ReactNode } from "react";
import { Briefcase, Globe2, ListFilter, Sparkles, TrendingUp } from "lucide-react";
import type { Facets } from "@/lib/types";
import { cx, useSpotlight } from "./primitives";

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
        index={0}
        icon={<ListFilter size={14} />}
        label="Matching"
        value={matching}
        loading={loading}
        accent
      />
      <Stat
        index={1}
        icon={<Briefcase size={14} />}
        label="Open roles"
        value={totals?.open}
        loading={loading}
      />
      <Stat
        index={2}
        icon={<Globe2 size={14} />}
        label="Remote"
        value={totals?.remote}
        loading={loading}
      />
      <Stat
        index={3}
        icon={<TrendingUp size={14} />}
        label="Posted this week"
        value={totals?.posted_last_7d}
        loading={loading}
      />
      <Stat
        index={4}
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
  index,
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
  index: number;
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
  const onPointerMove = useSpotlight<HTMLElement>();
  const Element = onClick ? "button" : "div";

  return (
    <Element
      onClick={onClick}
      onPointerMove={onPointerMove}
      style={{ "--i": index } as React.CSSProperties}
      className={cx(
        "glass glass-sheen spotlight stagger relative min-w-[152px] shrink-0 overflow-hidden rounded-2xl px-3.5 py-3 text-left",
        "transition-[transform,border-color,box-shadow] duration-300 sm:min-w-0",
        onClick && "cursor-pointer hover:-translate-y-0.5 active:translate-y-0 active:scale-[0.99]",
        active && "border-highlight/45 bg-highlight/10",
      )}
    >
      {/* A hairline of brand colour along the bottom edge marks the one card
          that answers "what am I looking at right now". */}
      {accent && (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-x-3 bottom-0 h-px bg-gradient-to-r from-transparent via-accent to-transparent opacity-70"
        />
      )}

      <div className="relative z-[1]">
        <p className="flex items-center gap-1.5 text-[10.5px] font-semibold tracking-[0.08em] text-subtle uppercase">
          <span
            className={cx(
              "grid size-5 shrink-0 place-items-center rounded-md border",
              tone === "highlight"
                ? "border-highlight/25 bg-highlight/10 text-highlight"
                : accent
                  ? "border-accent/25 bg-accent-soft text-accent-text"
                  : "border-edge bg-panel-strong text-subtle",
            )}
          >
            {icon}
          </span>
          <span className="truncate">{label}</span>
        </p>

        {loading && value === undefined && !placeholder ? (
          <div className="skeleton mt-2.5 h-6 w-16 rounded-lg" />
        ) : (
          <p
            className={cx(
              "mt-1.5 text-[23px] leading-none font-semibold tracking-[-0.03em] tabular-nums",
              accent ? "text-gradient" : tone === "highlight" ? "text-highlight" : "text-ink",
            )}
          >
            {value !== undefined ? value.toLocaleString() : (placeholder ?? "—")}
          </p>
        )}
      </div>
    </Element>
  );
}
