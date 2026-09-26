import { useCallback } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { markApplied, patchApplication, unmarkApplied } from "./api";
import type { ApplicationStatus } from "./types";

/**
 * Recording and correcting an application, shared by every surface with an
 * Apply button — the job table, the job drawer and the Matches list.
 *
 * One hook rather than three copies of the mutation, because the invalidation
 * set is the part that is easy to get subtly wrong: a status change has to
 * refresh the Dashboard's counts, its application list, *and* the job rows that
 * render the button's own state. Miss one and the button keeps saying "Apply"
 * on a job already applied to.
 *
 * `mark` is fire-and-forget on purpose. It runs beside the browser opening the
 * ATS page in a new tab, so a slow request never delays the thing the user
 * actually clicked for. A failure leaves the counts untouched and the button
 * unchanged, which is the honest outcome — nothing was recorded.
 */
export function useApplications() {
  const queryClient = useQueryClient();

  const invalidate = useCallback(() => {
    // `jobs` and `matches` carry `application_status` on every row, so they are
    // as much a view of this data as the Dashboard is.
    for (const key of [["dashboard"], ["applications"], ["jobs"], ["matches"]]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  }, [queryClient]);

  const mark = useMutation({
    mutationFn: (jobId: number) => markApplied(jobId, "apply_click"),
    onSuccess: invalidate,
  });

  const setStatus = useMutation({
    mutationFn: ({ jobId, status }: { jobId: number; status: ApplicationStatus }) =>
      patchApplication(jobId, { status }),
    onSuccess: invalidate,
  });

  const unmark = useMutation({
    mutationFn: (jobId: number) => unmarkApplied(jobId),
    onSuccess: invalidate,
  });

  return {
    /** Record an application. Idempotent server-side, so a re-click is safe. */
    mark: mark.mutate,
    markPending: mark.isPending,
    setStatus: setStatus.mutate,
    statusPending: setStatus.isPending,
    /** The job whose stage is being written, or null. Per-row, for the same
     *  reason as `unmarkingId`. */
    statusPendingId: setStatus.isPending ? (setStatus.variables?.jobId ?? null) : null,
    unmark: unmark.mutate,
    unmarkPending: unmark.isPending,
    /**
     * The job currently being un-applied, or null. The list surfaces render one
     * undo button per row and memoize the rows, so a single `isPending` flag
     * would spin every Applied pill on the page at once and re-render 3000 rows
     * to do it.
     */
    unmarkingId: unmark.isPending ? (unmark.variables ?? null) : null,
    error: (mark.error ?? setStatus.error ?? unmark.error) as Error | null,
  };
}
