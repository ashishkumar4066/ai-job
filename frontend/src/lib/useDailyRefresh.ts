import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { IngestStatus } from "@/lib/types";

// A run's shape changes fastest in its first seconds (every board flips from
// queued to fetching at once), then barely at all while the slow boards grind.
// Poll on that curve rather than at one compromise interval.
const POLL_MIN_MS = 450;
const POLL_MAX_MS = 2000;
const POLL_GROWTH = 1.35;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export interface DailyRefresh {
  /** False until the first sweep has settled — the dashboard shows nothing yet. */
  ready: boolean;
  /** True while any sweep is in flight, including a manual one after load. */
  syncing: boolean;
  /** Live per-source progress, or null before the first answer comes back. */
  status: IngestStatus | null;
  /** Set when the backend could not be reached or the run itself blew up. */
  error: Error | null;
  /** Manual re-sweep — the only thing that sweeps inside the 24h window. */
  refresh: () => void;
  /** Give up waiting and show whatever is stored. */
  skipWait: () => void;
}

/**
 * Fetch every board at most once per 24h, and hold the first paint if it runs.
 *
 * The freshness decision is entirely the backend's — it knows when the last run
 * actually finished, which survives a cleared browser and counts scheduler runs
 * too. A rolling 24h window (rather than a calendar boundary) means this hook
 * has nothing to contribute to the decision: it asks, and if the answer is
 * `fresh` no board is contacted and the dashboard paints straight from storage.
 * Only `refresh()` — the button and `r` — sweeps inside the window.
 */
export function useDailyRefresh(): DailyRefresh {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<IngestStatus | null>(null);
  const [ready, setReady] = useState(false);
  const [syncing, setSyncing] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  // Refs, not state: they survive StrictMode's mount/unmount/remount in dev, so
  // the sweep is started exactly once and the in-flight poll is never orphaned.
  const bootstrapped = useRef(false);
  const inFlight = useRef(false);

  const sweep = useCallback(
    async (force: boolean) => {
      if (inFlight.current) return;
      inFlight.current = true;
      setSyncing(true);
      setError(null);

      // Tracked locally as well as in state: the `finally` below has to know
      // whether this sweep failed, and a setState is not readable in time.
      let failure: Error | null = null;

      try {
        let current = await api.refreshIngest({ force });
        setStatus(current);

        let wait = POLL_MIN_MS;
        while (current.state === "running") {
          await sleep(wait);
          wait = Math.min(wait * POLL_GROWTH, POLL_MAX_MS);
          current = await api.ingestStatus();
          setStatus(current);
        }

        // Nothing was fetched before the gate opened, so the initial sweep has
        // no cache to bust. A later manual sweep does.
        if (ready) await queryClient.invalidateQueries();
        if (current.error) failure = new Error(current.error);
      } catch (caught) {
        failure = caught instanceof Error ? caught : new Error(String(caught));
      } finally {
        setError(failure);
        inFlight.current = false;
        setSyncing(false);
        // A first sweep that failed keeps the gate shut, so the sync screen can
        // offer a real choice — retry, or fall through to what is stored. Open
        // it regardless and the user just gets an empty table under a banner.
        if (!failure) setReady(true);
      }
    },
    [queryClient, ready],
  );

  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    void sweep(false);
  }, [sweep]);

  const refresh = useCallback(() => void sweep(true), [sweep]);
  const skipWait = useCallback(() => setReady(true), []);

  return { ready, syncing, status, error, refresh, skipWait };
}
