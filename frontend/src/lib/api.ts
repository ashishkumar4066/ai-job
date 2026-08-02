import type { Facets, Filters, Health, JobDetail, JobList } from "./types";

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

async function request<T>(path: string, params?: URLSearchParams): Promise<T> {
  const url = params?.toString() ? `${BASE}${path}?${params}` : `${BASE}${path}`;

  let response: Response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" } });
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

  facets: (lastVisit: string | null, status: string) => {
    const params = new URLSearchParams({ status });
    if (lastVisit) params.set("since", lastVisit);
    return request<Facets>("/meta/facets", params);
  },

  health: () => request<Health>("/health"),

  runIngest: async (): Promise<{ new: number; fetched: number; errored: number }> => {
    const response = await fetch(`${BASE}/ingest/run?notify=false`, { method: "POST" });
    if (!response.ok) {
      const message =
        response.status === 409
          ? "An ingest run is already in progress."
          : `Ingest failed (${response.status})`;
      throw new ApiError(message, response.status);
    }
    return response.json();
  },
};
