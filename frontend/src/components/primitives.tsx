import { useCallback, type PointerEvent, type ReactNode } from "react";
import { companyHue, initials } from "@/lib/format";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/* --------------------------------------------------------------- Spotlight */
/**
 * Feeds the pointer position into `--spot-x/--spot-y` so `.spotlight` can pool
 * violet light under the cursor. Written straight to the node rather than
 * through state — this fires on every pointer move, and a re-render per frame
 * would cost far more than the highlight is worth.
 */
export function useSpotlight<T extends HTMLElement>() {
  return useCallback((event: PointerEvent<T>) => {
    const element = event.currentTarget;
    const rect = element.getBoundingClientRect();
    element.style.setProperty("--spot-x", `${((event.clientX - rect.left) / rect.width) * 100}%`);
    element.style.setProperty("--spot-y", `${((event.clientY - rect.top) / rect.height) * 100}%`);
  }, []);
}

/* ------------------------------------------------------------------ Badges */
type Tone = "neutral" | "accent" | "highlight" | "danger" | "mint";

const TONES: Record<Tone, string> = {
  neutral: "bg-panel-strong text-muted border-edge",
  accent: "bg-accent-soft text-accent-text border-accent/30",
  highlight: "bg-highlight/12 text-highlight border-highlight/30",
  danger: "bg-danger/12 text-danger border-danger/30",
  mint: "bg-mint/12 text-mint border-mint/30",
};

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold tracking-wide whitespace-nowrap uppercase",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/* ------------------------------------------------------------ Company chip */
export function CompanyAvatar({ name, size = 34 }: { name: string; size?: number }) {
  // Hues stay inside a narrow band around the brand hue, so companies remain
  // distinguishable without the list turning into a rainbow.
  const hue = companyHue(name);
  return (
    <span
      aria-hidden
      className="relative grid shrink-0 place-items-center overflow-hidden rounded-xl font-semibold tracking-tight text-white"
      style={{
        width: size,
        height: size,
        fontSize: size * 0.38,
        background: `linear-gradient(140deg, oklch(66% 0.19 ${hue}), oklch(42% 0.2 ${hue + 22}))`,
        boxShadow: `inset 0 1px 0 oklch(100% 0 0 / 0.3), inset 0 0 0 1px oklch(100% 0 0 / 0.1), 0 4px 14px -8px oklch(50% 0.2 ${hue} / 0.9)`,
      }}
    >
      {/* Gloss across the top third — the difference between a coloured square
          and something that looks moulded. */}
      <span
        className="pointer-events-none absolute inset-x-0 top-0 h-1/2"
        style={{ background: "linear-gradient(180deg, oklch(100% 0 0 / 0.22), transparent)" }}
      />
      <span className="relative">{initials(name)}</span>
    </span>
  );
}

/* ------------------------------------------------------------------ Buttons */
export function IconButton({
  label,
  onClick,
  children,
  active,
  className,
}: {
  label: string;
  onClick?: () => void;
  children: ReactNode;
  active?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={cx(
        "relative grid size-9 place-items-center rounded-xl border transition-all duration-200",
        "hover:-translate-y-px active:translate-y-0 active:scale-95",
        active
          ? "border-accent/45 bg-accent-soft text-accent-text shadow-[0_4px_14px_-8px_var(--accent-glow)]"
          : "border-edge bg-panel text-muted hover:border-edge-strong hover:bg-panel-hover hover:text-ink",
        className,
      )}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------- Segmented control */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  size = "md",
}: {
  options: { value: T; label: ReactNode; title?: string }[];
  value: T;
  onChange: (value: T) => void;
  size?: "sm" | "md";
}) {
  return (
    <div
      role="tablist"
      className={cx(
        "inline-flex items-center gap-0.5 rounded-xl border border-edge bg-panel p-0.5",
        "shadow-[inset_0_1px_2px_oklch(0%_0_0_/_0.06)]",
        size === "sm" ? "text-[12px]" : "text-[13px]",
      )}
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            role="tab"
            aria-selected={selected}
            title={option.title}
            onClick={() => onChange(option.value)}
            className={cx(
              "rounded-[10px] px-2.5 font-medium whitespace-nowrap transition-all duration-200",
              size === "sm" ? "py-1" : "py-1.5",
              selected
                ? "bg-gradient-to-b from-accent to-accent-strong text-white shadow-[inset_0_1px_0_oklch(100%_0_0_/_0.25),0_3px_12px_-4px_var(--accent-glow)]"
                : "text-muted hover:bg-panel-hover hover:text-ink",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/* ---------------------------------------------------------------- Skeletons */
export function SkeletonRow() {
  return (
    <div className="flex items-center gap-4 border-b border-edge px-5 py-4">
      <div className="skeleton size-9 rounded-xl" />
      <div className="flex-1 space-y-2">
        <div className="skeleton h-3.5 w-1/3 rounded-full" />
        <div className="skeleton h-2.5 w-1/5 rounded-full" />
      </div>
      <div className="skeleton hidden h-3 w-24 rounded-full md:block" />
      <div className="skeleton hidden h-3 w-16 rounded-full lg:block" />
      <div className="skeleton h-7 w-16 rounded-lg" />
    </div>
  );
}

/* -------------------------------------------------------------- Empty state */
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="animate-fade-up flex flex-col items-center justify-center gap-3 px-6 py-20 text-center">
      <div className="relative grid size-16 place-items-center rounded-2xl border border-edge bg-panel text-accent-text">
        {/* Halo behind the glyph, so the empty state reads as considered
            rather than as a failure. */}
        <span className="absolute inset-0 rounded-2xl bg-[radial-gradient(circle_at_50%_40%,var(--accent-soft),transparent_70%)]" />
        <span className="relative">{icon}</span>
      </div>
      <h3 className="text-base font-semibold tracking-tight text-ink">{title}</h3>
      <p className="max-w-sm text-sm leading-relaxed text-muted">{description}</p>
      {action}
    </div>
  );
}

/* ------------------------------------------------------------------- Kbd */
export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded-md border border-edge bg-panel-strong px-1.5 py-0.5 font-mono text-[10px] text-subtle shadow-[0_1px_0_var(--border-strong)]">
      {children}
    </kbd>
  );
}

/* ---------------------------------------------------------------- Divider */
export function VDivider({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cx(
        "h-6 w-px bg-gradient-to-b from-transparent via-edge-strong to-transparent",
        className,
      )}
    />
  );
}
