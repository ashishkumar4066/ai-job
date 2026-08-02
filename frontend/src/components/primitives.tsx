import type { ReactNode } from "react";
import { companyHue, initials } from "@/lib/format";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/* ------------------------------------------------------------------ Badges */
type Tone = "neutral" | "accent" | "highlight" | "danger";

const TONES: Record<Tone, string> = {
  neutral: "bg-panel-strong text-muted border-edge",
  accent: "bg-accent-soft text-accent-text border-accent/25",
  highlight: "bg-highlight/12 text-highlight border-highlight/25",
  danger: "bg-danger/12 text-danger border-danger/25",
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
        "inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap",
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
      className="grid shrink-0 place-items-center rounded-xl border border-white/12 font-semibold tracking-tight text-white"
      style={{
        width: size,
        height: size,
        fontSize: size * 0.4,
        background: `linear-gradient(135deg, oklch(60% 0.15 ${hue}), oklch(48% 0.17 ${hue + 18}))`,
        boxShadow: `0 4px 14px -8px oklch(50% 0.16 ${hue} / 0.9)`,
      }}
    >
      {initials(name)}
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
        "grid size-9 place-items-center rounded-xl border transition-all duration-150",
        "hover:bg-panel-hover active:scale-95",
        active
          ? "border-accent/40 bg-accent-soft text-accent-text"
          : "border-edge bg-panel text-muted hover:text-ink",
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
              "rounded-[10px] px-2.5 font-medium whitespace-nowrap transition-all duration-150",
              size === "sm" ? "py-1" : "py-1.5",
              selected
                ? "bg-accent text-white shadow-[0_2px_10px_-3px_var(--accent)]"
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
      <div className="grid size-14 place-items-center rounded-2xl border border-edge bg-panel text-subtle">
        {icon}
      </div>
      <h3 className="text-base font-semibold text-ink">{title}</h3>
      <p className="max-w-sm text-sm text-muted">{description}</p>
      {action}
    </div>
  );
}

/* ------------------------------------------------------------------- Kbd */
export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded-md border border-edge bg-panel-strong px-1.5 py-0.5 font-mono text-[10px] text-subtle">
      {children}
    </kbd>
  );
}
