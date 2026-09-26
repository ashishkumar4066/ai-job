import { useCallback, useEffect, useState } from "react";
import {
  Briefcase,
  LayoutDashboard,
  MessagesSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Target,
  X,
  type LucideIcon,
} from "lucide-react";
import { relativeTime } from "@/lib/format";
import type { ViewId } from "@/lib/types";
import { BrandGlyph } from "./BrandMark";
import { cx } from "./primitives";

const COLLAPSE_KEY = "jr:nav-collapsed";

type NavTile = {
  id: ViewId;
  label: string;
  hint: string;
  icon: LucideIcon;
  /** Shown instead of a count while a surface is still being built. */
  badge?: string;
};

const TILES: NavTile[] = [
  {
    id: "dashboard",
    label: "Dashboard",
    hint: "Applications, pipeline & budget",
    icon: LayoutDashboard,
  },
  {
    id: "jobs",
    label: "Jobs",
    hint: "Everything the boards swept",
    icon: Briefcase,
  },
  {
    id: "matches",
    label: "Matches",
    hint: "Fit, tailored résumé & letter",
    icon: Target,
  },
  {
    id: "prep",
    label: "Interview Prep",
    hint: "Not designed yet",
    icon: MessagesSquare,
    badge: "Soon",
  },
];

/**
 * The app's left rail.
 *
 * Three layouts out of one element, because the alternative is three copies of
 * the nav that drift apart:
 *   - desktop expanded  — a 248px panel, label + hint per tile
 *   - desktop collapsed — a 74px icon rail (choice persisted in localStorage)
 *   - phone             — an off-canvas drawer over a scrim, opened from TopBar
 */
export function SideNav({
  view,
  onViewChange,
  open,
  onClose,
  jobCount,
  lastIngest,
  syncing,
}: {
  view: ViewId;
  onViewChange: (view: ViewId) => void;
  open: boolean;
  onClose: () => void;
  jobCount: number | null;
  lastIngest: string | null;
  syncing: boolean;
}) {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });

  const toggleCollapsed = useCallback(() => {
    setCollapsed((previous) => {
      const next = !previous;
      try {
        localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
      } catch {
        /* private mode — the rail just will not remember */
      }
      return next;
    });
  }, []);

  // Escape closes the phone drawer. Desktop never opens it, so this is inert there.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  // Collapsed is a desktop-only state: inside the drawer there is room for labels.
  const rail = collapsed;

  const syncLabel = syncing
    ? "Sweeping boards…"
    : lastIngest
      ? `Synced ${relativeTime(lastIngest)}`
      : "Not synced yet";

  return (
    <>
      {open && (
        <div
          onClick={onClose}
          aria-hidden
          className="animate-fade-in fixed inset-0 z-40 bg-black/55 backdrop-blur-[4px] md:hidden"
        />
      )}

      <aside
        aria-label="Primary"
        className={cx(
          "glass-strong glass-sheen z-50 flex shrink-0 flex-col overflow-hidden rounded-2xl p-2.5",
          "transition-[transform,width] duration-300 ease-out",
          // Phone: an overlay pinned inside the same gutter as the app.
          "fixed inset-y-3 left-3 w-[264px]",
          open ? "translate-x-0" : "-translate-x-[calc(100%+1rem)]",
          // Desktop: back in flow, always visible, width driven by the rail state.
          "md:static md:translate-x-0",
          rail ? "md:w-[74px]" : "md:w-[248px]",
        )}
      >
        {/* Brand ------------------------------------------------------------ */}
        <div className={cx("flex items-center gap-2.5 px-1 pt-1 pb-3", rail && "md:justify-center")}>
          <span className="rim relative grid size-10 shrink-0 place-items-center overflow-hidden rounded-xl bg-gradient-to-br from-accent to-accent-strong text-white shadow-[inset_0_1px_0_oklch(100%_0_0_/_0.3),0_8px_24px_-10px_var(--accent-glow)]">
            <span
              aria-hidden
              className="pointer-events-none absolute inset-x-0 top-0 h-1/2 bg-gradient-to-b from-white/25 to-transparent"
            />
            <BrandGlyph className="relative size-full" />
          </span>
          <h1
            className={cx(
              "min-w-0 flex-1 truncate text-[15px] leading-tight font-semibold tracking-[-0.02em] text-ink",
              rail && "md:hidden",
            )}
          >
            Job<span className="text-gradient"> Radar</span>
          </h1>
          <button
            onClick={onClose}
            aria-label="Close navigation"
            className="grid size-8 shrink-0 place-items-center rounded-lg text-subtle transition-colors hover:bg-panel-hover hover:text-ink md:hidden"
          >
            <X size={16} />
          </button>
        </div>

        {/* Tiles ------------------------------------------------------------ */}
        <nav className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto">
          {TILES.map((tile, index) => (
            <Tile
              key={tile.id}
              tile={tile}
              index={index}
              rail={rail}
              active={view === tile.id}
              count={tile.id === "jobs" ? jobCount : null}
              onSelect={() => {
                onViewChange(tile.id);
                onClose();
              }}
            />
          ))}
        </nav>

        {/* Status + collapse ------------------------------------------------ */}
        <div className="mt-2 border-t border-edge pt-2.5">
          <div
            title={syncLabel}
            className={cx(
              "flex items-center gap-2 rounded-xl px-2 py-1.5 text-[11px] text-subtle",
              rail && "md:justify-center md:px-0",
            )}
          >
            <span
              aria-hidden
              className={cx(
                "size-1.5 shrink-0 rounded-full",
                syncing ? "animate-breathe bg-accent" : lastIngest ? "bg-mint" : "bg-subtle",
              )}
            />
            <span className={cx("min-w-0 flex-1 truncate", rail && "md:hidden")}>{syncLabel}</span>
          </div>

          <button
            onClick={toggleCollapsed}
            aria-label={rail ? "Expand navigation" : "Collapse navigation"}
            title={rail ? "Expand navigation" : "Collapse navigation"}
            className={cx(
              "mt-1 hidden w-full items-center gap-2 rounded-xl px-2 py-2 text-[12px] font-medium text-subtle",
              "transition-colors hover:bg-panel-hover hover:text-ink md:flex",
              rail && "md:justify-center md:px-0",
            )}
          >
            {rail ? (
              <PanelLeftOpen size={16} className="shrink-0" />
            ) : (
              <PanelLeftClose size={16} className="shrink-0" />
            )}
            <span className={cx(rail && "md:hidden")}>Collapse</span>
          </button>
        </div>
      </aside>
    </>
  );
}

function Tile({
  tile,
  index,
  rail,
  active,
  count,
  onSelect,
}: {
  tile: NavTile;
  index: number;
  rail: boolean;
  active: boolean;
  count: number | null;
  onSelect: () => void;
}) {
  const Icon = tile.icon;

  return (
    <button
      onClick={onSelect}
      aria-current={active ? "page" : undefined}
      title={rail ? tile.label : undefined}
      style={{ animationDelay: `${index * 60}ms` }}
      className={cx(
        "animate-fade-up group relative flex items-center gap-2.5 rounded-xl border p-2 text-left",
        "transition-all duration-200 hover:-translate-y-px active:translate-y-0",
        active
          ? "border-accent/40 bg-accent-soft shadow-[0_6px_20px_-12px_var(--accent-glow)]"
          : "border-transparent hover:border-edge hover:bg-panel-hover",
        rail && "md:justify-center md:p-1.5",
      )}
    >
      {/* Active marker on the left edge — the rail still reads at a glance
          once it is collapsed to icons. */}
      <span
        aria-hidden
        className={cx(
          "absolute top-1/2 -left-px h-6 w-[3px] -translate-y-1/2 rounded-r-full bg-gradient-to-b from-accent to-accent-strong transition-opacity duration-200",
          active ? "opacity-100" : "opacity-0",
        )}
      />
      <span
        className={cx(
          "grid size-9 shrink-0 place-items-center rounded-lg border transition-colors duration-200",
          active
            ? "border-accent/35 bg-gradient-to-br from-accent to-accent-strong text-white shadow-[inset_0_1px_0_oklch(100%_0_0_/_0.25)]"
            : "border-edge bg-panel text-muted group-hover:text-ink",
        )}
      >
        <Icon size={17} strokeWidth={2} />
      </span>

      <span className={cx("min-w-0 flex-1", rail && "md:hidden")}>
        <span className="flex items-center gap-1.5">
          <span
            className={cx(
              "truncate text-[13.5px] font-semibold",
              active ? "text-accent-text" : "text-ink",
            )}
          >
            {tile.label}
          </span>
          {tile.badge ? (
            <span className="rounded-full border border-highlight/30 bg-highlight/12 px-1.5 py-px text-[9.5px] font-semibold tracking-wide text-highlight uppercase">
              {tile.badge}
            </span>
          ) : (
            count !== null && (
              <span className="ml-auto shrink-0 font-mono text-[10.5px] text-subtle tabular-nums">
                {count.toLocaleString()}
              </span>
            )
          )}
        </span>
        <span className="mt-0.5 block truncate text-[11px] text-subtle">{tile.hint}</span>
      </span>
    </button>
  );
}
