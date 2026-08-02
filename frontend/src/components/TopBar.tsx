import { useEffect, useRef, useState } from "react";
import { Loader2, Moon, RadarIcon, RefreshCw, Search, Sparkles, Sun, X } from "lucide-react";
import { relativeTime } from "@/lib/format";
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
  const [focused, setFocused] = useState(false);

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
    <header className="glass-strong glass-sheen relative z-30 flex flex-wrap items-center gap-3 rounded-2xl px-4 py-3">
      {/* Brand */}
      <div className="flex items-center gap-2.5">
        <span className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-accent to-accent-strong text-white shadow-[0_6px_18px_-6px_var(--accent)]">
          <RadarIcon size={18} strokeWidth={2.2} />
        </span>
        <div className="leading-tight">
          <h1 className="text-[15px] font-semibold tracking-tight text-ink">Job Radar</h1>
          <p className="hidden text-[11px] text-subtle sm:block">
            {lastIngest ? `Synced ${relativeTime(lastIngest)}` : "Not synced yet"}
          </p>
        </div>
      </div>

      {/* Search */}
      <div
        className={cx(
          // Full width on its own line on phones; inline from md up.
          "order-last flex h-10 min-w-0 basis-full items-center gap-2 rounded-xl border px-3 transition-all duration-200 md:order-none md:flex-1 md:basis-auto",
          focused
            ? "border-accent/50 bg-panel-strong shadow-[0_0_0_4px_var(--accent-soft)]"
            : "border-edge bg-panel hover:bg-panel-hover",
        )}
      >
        <Search size={15} className="shrink-0 text-subtle" />
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder="Search roles, companies, descriptions…"
          className="min-w-0 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-subtle"
        />
        {query ? (
          <button onClick={() => onQueryChange("")} aria-label="Clear search">
            <X size={15} className="text-subtle transition-colors hover:text-ink" />
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
            className="animate-pulse-ring flex h-9 items-center gap-1.5 rounded-xl border border-highlight/30 bg-highlight/12 px-3 text-[13px] font-medium text-highlight transition-colors hover:bg-highlight/20"
          >
            <Sparkles size={14} />
            {newCount} new
          </button>
        )}
        <IconButton label="Fetch latest jobs now" onClick={onRefresh}>
          {refreshing ? (
            <Loader2 size={16} className="animate-spin" />
          ) : (
            <RefreshCw size={16} />
          )}
        </IconButton>
        <IconButton label={dark ? "Switch to light mode" : "Switch to dark mode"} onClick={onToggleTheme}>
          {dark ? <Sun size={16} /> : <Moon size={16} />}
        </IconButton>
      </div>
    </header>
  );
}
