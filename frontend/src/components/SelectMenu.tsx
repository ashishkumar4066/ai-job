import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";

import { cx } from "./primitives";

/**
 * A single-choice dropdown that belongs to this app's palette.
 *
 * Why not `<select>`
 * ------------------
 * A native select's *list* is drawn by the OS, not by the page: on Windows it
 * lands as a square, system-blue, system-font popup over a black-and-violet UI
 * — the one control on screen that looks like it came from somewhere else. CSS
 * cannot reach it. The `option { background-color }` rule in `index.css` (kept,
 * for any select added later) fixes the worst of it — near-white text on a
 * near-white list — and cannot touch the geometry, the highlight colour or the
 * type.
 *
 * So the list is ours: the same popover surface as MultiSelect, the same violet
 * selection, the same radii. The trigger keeps whatever shape its caller gives
 * it, because these sit in four places that look nothing alike — a coloured
 * status pill in a table row, two sort controls, and a field in the profile
 * editor.
 *
 * It is a portal, not an absolutely-positioned child, because three of the four
 * call sites are inside `overflow-hidden` containers — the virtualized job
 * table clips its own rows by design, and a menu rendered inside a row would be
 * clipped along with it.
 */
export type SelectOption<T extends string> = {
  value: T;
  label: ReactNode;
  /** Secondary line, e.g. what a sort actually orders by. */
  hint?: string;
  icon?: ReactNode;
};

const MENU_GAP = 6;
const VIEWPORT_MARGIN = 8;

export function SelectMenu<T extends string>({
  value,
  options,
  onChange,
  label,
  title,
  className,
  menuClassName,
  leading,
  trailing,
  disabled = false,
  align = "start",
  chevronSize = 13,
}: {
  value: T;
  options: SelectOption<T>[];
  onChange: (value: T) => void;
  /** Announced name — several of these can be on screen at once. */
  label: string;
  title?: string;
  /** Trigger styling. The caller owns the trigger's shape entirely. */
  className?: string;
  menuClassName?: string;
  leading?: ReactNode;
  /** Replaces the chevron — a spinner, while a change is in flight. */
  trailing?: ReactNode;
  disabled?: boolean;
  align?: "start" | "end";
  chevronSize?: number;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{
    top: number;
    left: number;
    minWidth: number;
  } | null>(null);

  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );
  const selected = options[selectedIndex];

  /* ------------------------------------------------------------- placement */
  const place = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const menuHeight = menuRef.current?.offsetHeight ?? 0;
    const menuWidth = Math.max(menuRef.current?.offsetWidth ?? 0, rect.width);

    const below = window.innerHeight - rect.bottom - MENU_GAP;
    const above = rect.top - MENU_GAP;
    // Flip only when there is genuinely more room the other way, so a control
    // near the bottom of the table opens upward instead of being crammed.
    const flip = menuHeight > below && above > below;

    const left =
      align === "end"
        ? Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - VIEWPORT_MARGIN)
        : Math.min(rect.left, window.innerWidth - menuWidth - VIEWPORT_MARGIN);

    setPosition({
      top: flip
        ? Math.max(rect.top - MENU_GAP - menuHeight, VIEWPORT_MARGIN)
        : Math.min(rect.bottom + MENU_GAP, window.innerHeight - menuHeight - VIEWPORT_MARGIN),
      left: Math.max(VIEWPORT_MARGIN, left),
      minWidth: rect.width,
    });
  }, [align]);

  // Before paint, so the menu is never visible at the wrong place for a frame.
  useLayoutEffect(() => {
    if (!open) return;
    place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    // `capture`, because the scroller that moves this trigger is the job
    // table's own container, not the window.
    const reposition = () => place();
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open, place]);

  /* ------------------------------------------------------------- dismissal */
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  useEffect(() => {
    if (open) setActive(selectedIndex);
  }, [open, selectedIndex]);

  const commit = (next: T) => {
    setOpen(false);
    triggerRef.current?.focus();
    if (next !== value) onChange(next);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!open) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter") {
        event.preventDefault();
        setOpen(true);
      }
      return;
    }
    switch (event.key) {
      case "Escape":
        // Stopped here: most of these sit inside a dialog or a row that also
        // closes on Escape, and closing both at once loses the edit.
        event.stopPropagation();
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
        break;
      case "ArrowDown":
        event.preventDefault();
        setActive((index) => (index + 1) % options.length);
        break;
      case "ArrowUp":
        event.preventDefault();
        setActive((index) => (index - 1 + options.length) % options.length);
        break;
      case "Home":
        event.preventDefault();
        setActive(0);
        break;
      case "End":
        event.preventDefault();
        setActive(options.length - 1);
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        if (options[active]) commit(options[active].value);
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={label}
        title={title}
        disabled={disabled}
        onClick={() => setOpen((isOpen) => !isOpen)}
        onKeyDown={onKeyDown}
        className={cx(
          "flex min-w-0 cursor-pointer items-center gap-1.5 outline-none disabled:cursor-default disabled:opacity-60",
          className,
        )}
      >
        {leading}
        <span className="min-w-0 truncate">{selected?.label ?? value}</span>
        {trailing ?? (
          <ChevronDown
            size={chevronSize}
            aria-hidden
            className={cx(
              "shrink-0 opacity-55 transition-transform duration-200",
              open && "rotate-180",
            )}
          />
        )}
      </button>

      {open &&
        createPortal(
          <div
            ref={menuRef}
            role="listbox"
            aria-label={label}
            tabIndex={-1}
            onKeyDown={onKeyDown}
            // The trigger keeps focus, so the surrounding row's handlers stay
            // as they are; the menu only has to not leak clicks into them.
            onClick={(event) => event.stopPropagation()}
            style={{
              position: "fixed",
              top: position?.top ?? 0,
              left: position?.left ?? 0,
              minWidth: position?.minWidth,
              visibility: position ? "visible" : "hidden",
            }}
            className={cx(
              "glass-popover glass-sheen animate-fade-up z-[70] max-h-[min(320px,60vh)] overflow-y-auto overscroll-contain rounded-xl p-1.5",
              menuClassName,
            )}
          >
            {options.map((option, index) => {
              const isSelected = option.value === value;
              return (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => commit(option.value)}
                  className={cx(
                    "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] whitespace-nowrap transition-colors",
                    index === active ? "bg-accent-soft text-ink" : "text-muted",
                    isSelected && index !== active && "text-ink",
                  )}
                >
                  {option.icon && <span className="shrink-0 opacity-80">{option.icon}</span>}
                  <span className="flex-1 truncate font-medium">{option.label}</span>
                  {option.hint && (
                    <span className="shrink-0 text-[11px] text-subtle">{option.hint}</span>
                  )}
                  <Check
                    size={13}
                    strokeWidth={3}
                    aria-hidden
                    className={cx("shrink-0 text-accent-text", !isSelected && "opacity-0")}
                  />
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </>
  );
}
