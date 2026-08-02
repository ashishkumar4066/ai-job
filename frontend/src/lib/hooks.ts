import { useCallback, useEffect, useRef, useState } from "react";

const THEME_KEY = "jr:theme";
const VISIT_KEY = "jr:last-visit";

/** Dark/light with a persisted choice; the boot script in index.html avoids FOUC. */
export function useTheme() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));

  const toggle = useCallback(() => {
    setDark((previous) => {
      const next = !previous;
      document.documentElement.classList.toggle("dark", next);
      try {
        localStorage.setItem(THEME_KEY, next ? "dark" : "light");
      } catch {
        /* private mode — theme just won't persist */
      }
      return next;
    });
  }, []);

  return { dark, toggle };
}

/**
 * "New since last visit."
 *
 * The stored timestamp is frozen for the whole session so rows don't stop
 * being "new" while you're reading them; it advances only on unload.
 */
export function useLastVisit() {
  const [lastVisit] = useState<string | null>(() => {
    try {
      return localStorage.getItem(VISIT_KEY);
    } catch {
      return null;
    }
  });

  const stamp = useCallback(() => {
    try {
      localStorage.setItem(VISIT_KEY, new Date().toISOString());
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    window.addEventListener("beforeunload", stamp);
    return () => {
      window.removeEventListener("beforeunload", stamp);
      stamp();
    };
  }, [stamp]);

  const markSeenNow = useCallback(() => {
    stamp();
    window.location.reload();
  }, [stamp]);

  return { lastVisit, markSeenNow };
}

export function useDebounced<T>(value: T, delay = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

/** Closes popovers on outside click and on Escape. */
export function useDismissable<T extends HTMLElement>(open: boolean, onClose: () => void) {
  const ref = useRef<T>(null);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  return ref;
}

/** Global shortcuts. Ignored while typing so they never eat real input. */
export function useHotkeys(handlers: Record<string, (event: KeyboardEvent) => void>) {
  const latest = useRef(handlers);
  latest.current = handlers;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target?.isContentEditable;

      if (typing && event.key !== "Escape") return;

      const handler = latest.current[event.key];
      if (handler) handler(event);
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
