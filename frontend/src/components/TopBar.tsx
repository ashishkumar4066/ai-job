import { useEffect, useRef } from "react";
import { Loader2, Menu, Moon, RefreshCw, Search, Sparkles, Sun, X } from "lucide-react";
import type { SearchScope } from "@/lib/types";
import { IconButton, Kbd, Segmented, cx } from "./primitives";

export function TopBar({
  query,
  onQueryChange,
  scope,
  onScopeChange,
  dark,
  onToggleTheme,
  onRefresh,
  refreshing,
  newCount,
  onShowNew,
  onOpenNav,
  title,
}: {
  query: string;
  onQueryChange: (value: string) => void;
  scope: SearchScope;
  onScopeChange: (value: SearchScope) => void;
  dark: boolean;
  onToggleTheme: () => void;
  onRefresh: () => void;
  refreshing: boolean;
  newCount: number;
  onShowNew: () => void;
  /** Opens the off-canvas nav; only rendered on phones, where the rail is hidden. */
  onOpenNav: () => void;
  /**
   * Set on surfaces that the search box does not drive, which take its slot
   * rather than offering a box that quietly filters something off screen.
   */
  title?: string;
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
      {/* The rail carries the brand; on phones it is off-canvas, so the only
          thing the header owes it is a way back. */}
      <IconButton label="Open navigation" onClick={onOpenNav} className="md:hidden">
        <Menu size={16} />
      </IconButton>

      {title ? (
        <h2 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-[-0.02em] text-ink">
          {title}
        </h2>
      ) : (
        /* Search — the focus treatment is driven by :has(), so no React state
           re-renders the whole bar on every focus change. */
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
      )}

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
