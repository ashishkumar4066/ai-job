import type { Facets, Filters, Health, IngestStatus, JobDetail, JobList } from "./types";

/**
 * In dev, Vite proxies /api -> http://127.0.0.1:8000 (see vite.config.ts).
 * In prod, point VITE_API_BASE at the deployed backend.
 */
const BASE = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  params?: URLSearchParams,
  init?: RequestInit,
): Promise<T> {
  const url = params?.toString() ? `${BASE}${path}?${params}` : `${BASE}${path}`;

  let response: Response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" }, ...init });
  } catch {
    throw new ApiError(
      "Can't reach the API. Is the backend running on port 8000?",
      0,
    );
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(detail, response.status);
  }

  return response.json() as Promise<T>;
}

/** Translates dashboard filter state into API query parameters. */
export function filtersToParams(
  filters: Filters,
  extra?: { limit?: number; offset?: number; lastVisit?: string | null },
): URLSearchParams {
  const params = new URLSearchParams();

  if (filters.q.trim()) {
    params.set("q", filters.q.trim());
    params.set("q_scope", filters.qScope);
  }
  for (const company of filters.companies) params.append("company", company);
  for (const ats of filters.ats) params.append("ats", ats);
  for (const department of filters.departments) params.append("department", department);
  if (filters.remote !== null) params.set("remote", String(filters.remote));
  params.set("status", filters.status);
  // Only ever sent as `true`. Omitting it means "no eligibility constraint",
  // which is what showing everything requires — sending `false` would invert
  // the filter and show *only* the rejects.
  if (filters.matchesPrefs) params.set("eligibility_pass", "true");
  if (filters.postedWithinDays !== null) {
    params.set("posted_within_days", String(filters.postedWithinDays));
  }
  if (filters.newOnly && extra?.lastVisit) {
    params.set("first_seen_after", extra.lastVisit);
  }
  params.set("sort", filters.sort);
  params.set("order", filters.order);
  if (extra?.limit !== undefined) params.set("limit", String(extra.limit));
  if (extra?.offset !== undefined) params.set("offset", String(extra.offset));

  return params;
}

export const api = {
  jobs: (filters: Filters, page: { limit: number; offset: number }, lastVisit: string | null) =>
    request<JobList>("/jobs", filtersToParams(filters, { ...page, lastVisit })),

  job: (id: number) => request<JobDetail>(`/jobs/${id}`),

  // `matchesPrefs` is passed through so the dropdown counts describe the rows
  // actually on screen. Without it a company with 40 postings and no eligible
  // ones still offers "Acme (40)", which selects and yields an empty table.
  facets: (lastVisit: string | null, status: string, matchesPrefs: boolean) => {
    const params = new URLSearchParams({ status });
    if (lastVisit) params.set("since", lastVisit);
    if (matchesPrefs) params.set("eligibility_pass", "true");
    return request<Facets>("/meta/facets", params);
  },

  health: () => request<Health>("/health"),

  /**
   * Ask the backend to sweep every board.
   *
   * The freshness window is entirely the backend's: it skips the sweep when the
   * last run finished less than 24h ago, so the boards are contacted once a day
   * no matter how often the page is opened. A rolling window needs nothing from
   * this side — no clock, no timezone — so nothing is sent. `force` is the
   * manual refresh button, and is the only way to sweep inside the window.
   * Returns as soon as the run *starts* — follow it with `ingestStatus`.
   */
  refreshIngest: (options?: { force?: boolean }) => {
    const params = new URLSearchParams();
    if (options?.force) params.set("force", "true");
    return request<IngestStatus>("/ingest/refresh", params, { method: "POST" });
  },

  ingestStatus: () => request<IngestStatus>("/ingest/status"),
};
