import { useEffect, useRef } from "react";
import { Loader2, Moon, RadarIcon, RefreshCw, Search, Sparkles, Sun, X } from "lucide-react";
import { relativeTime } from "@/lib/format";
import type { SearchScope } from "@/lib/types";
import { IconButton, Kbd, Segmented, VDivider, cx } from "./primitives";

export function TopBar({
  query,
  onQueryChange,
  scope,
  onScopeChange,
  dark,
  onToggleTheme,
  onRefresh,
  refreshing,
  lastIngest,
  newCount,
  onShowNew,
}: {
  query: string;
  onQueryChange: (value: string) => void;
  scope: SearchScope;
  onScopeChange: (value: SearchScope) => void;
  dark: boolean;
  onToggleTheme: () => void;
  onRefresh: () => void;
  refreshing: boolean;
  lastIngest: string | null;
  newCount: number;
  onShowNew: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  // "/" jumps to search from anywhere.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target?.isContentEditable;
      if (event.key === "/" && !typing) {
        event.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <header className="glass-strong glass-sheen relative z-30 flex flex-wrap items-center gap-3 rounded-2xl px-3.5 py-3 md:gap-4">
      {/* Brand */}
      <div className="flex items-center gap-3">
        <span className="rim relative grid size-10 place-items-center overflow-hidden rounded-xl bg-gradient-to-br from-accent to-accent-strong text-white shadow-[inset_0_1px_0_oklch(100%_0_0_/_0.3),0_8px_24px_-10px_var(--accent-glow)]">
          <span
            aria-hidden
            className="pointer-events-none absolute inset-x-0 top-0 h-1/2 bg-gradient-to-b from-white/25 to-transparent"
          />
          <RadarIcon size={19} strokeWidth={2.2} className="relative" />
        </span>
        <div className="leading-tight">
          <h1 className="text-[15px] font-semibold tracking-[-0.02em] text-ink">
            Job<span className="text-gradient"> Radar</span>
          </h1>
          <p className="hidden items-center gap-1.5 text-[11px] text-subtle sm:flex">
            <span
              aria-hidden
              className={cx(
                "size-1.5 rounded-full",
                refreshing ? "animate-breathe bg-accent" : lastIngest ? "bg-mint" : "bg-subtle",
              )}
            />
            {refreshing
              ? "Sweeping boards…"
              : lastIngest
                ? `Synced ${relativeTime(lastIngest)}`
                : "Not synced yet"}
          </p>
        </div>
      </div>

      <VDivider className="hidden md:block" />

      {/* Search — the focus treatment is driven by :has(), so no React state
          re-renders the whole bar on every focus change. */}
      <div
        className={cx(
          // Full width on its own line on phones; inline from md up.
          "order-last flex h-10 min-w-0 basis-full items-center gap-2 rounded-xl border border-edge bg-panel px-3",
          "transition-[border-color,box-shadow,background-color] duration-200 hover:bg-panel-hover",
          "has-[input:focus]:border-accent/55 has-[input:focus]:bg-panel-strong",
          "has-[input:focus]:shadow-[0_0_0_4px_var(--accent-soft),0_8px_24px_-14px_var(--accent-glow)]",
          "md:order-none md:h-11 md:flex-1 md:basis-auto",
        )}
      >
        <Search size={15} className="shrink-0 text-subtle transition-colors" />
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="Search roles, companies, descriptions…"
          className="min-w-0 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-subtle"
        />
        {query ? (
          <button
            onClick={() => onQueryChange("")}
            aria-label="Clear search"
            className="grid size-5 place-items-center rounded-md text-subtle transition-colors hover:bg-panel-hover hover:text-ink"
          >
            <X size={14} />
          </button>
        ) : (
          <Kbd>/</Kbd>
        )}
        <span className="hidden sm:block">
          <Segmented
            size="sm"
            value={scope}
            onChange={onScopeChange}
            options={[
              { value: "all", label: "All", title: "Search titles, companies and descriptions" },
              { value: "title", label: "Title", title: "Search job titles only — far less noisy" },
            ]}
          />
        </span>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2">
        {newCount > 0 && (
          <button
            onClick={onShowNew}
            className="animate-pulse-ring flex h-9 items-center gap-1.5 rounded-xl border border-highlight/35 bg-highlight/12 px-3 text-[13px] font-semibold text-highlight transition-all duration-200 hover:-translate-y-px hover:bg-highlight/20"
          >
            <Sparkles size={14} />
            {newCount.toLocaleString()} new
          </button>
        )}
        <IconButton label="Fetch latest jobs now" onClick={onRefresh} active={refreshing}>
          {refreshing ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={16} />}
        </IconButton>
        <IconButton
          label={dark ? "Switch to light mode" : "Switch to dark mode"}
          onClick={onToggleTheme}
        >
          {dark ? <Sun size={16} /> : <Moon size={16} />}
        </IconButton>
      </div>
    </header>
  );
}
