import type {
  Facets,
  Filters,
  Health,
  IngestStatus,
  JobDetail,
  JobFilterSet,
  JobList,
  JobsFunnel,
  LlmEstimate,
  LlmStatus,
  MatchBandFilters,
  MatchesFunnel,
  MatchList,
  MatchRun,
  MatchSort,
  PipelineStatus,
  Prefs,
  PrefsPatch,
  ProfileSummary,
  ValidityRun,
} from "./types";

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
  if (filters.minValidity) params.set("min_validity", String(filters.minValidity));
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

/** The Jobs filters in the API's own shape — the body of "Send to Matches". */
export function filtersToFilterSet(filters: Filters, lastVisit: string | null): JobFilterSet {
  return {
    company: filters.companies,
    ats: filters.ats,
    department: filters.departments,
    remote: filters.remote,
    q: filters.q.trim() || null,
    q_scope: filters.qScope,
    posted_within_days: filters.postedWithinDays,
    first_seen_after: filters.newOnly && lastVisit ? lastVisit : null,
    // Sent only as `true`, for the reason `filtersToParams` gives.
    eligibility_pass: filters.matchesPrefs ? true : null,
    min_validity: filters.minValidity,
  };
}

export const api = {
  jobs: (filters: Filters, page: { limit: number; offset: number }, lastVisit: string | null) =>
    request<JobList>("/jobs", filtersToParams(filters, { ...page, lastVisit })),

  /** Same parameters as `jobs`, so the last step equals the list's total. */
  jobsFunnel: (filters: Filters, lastVisit: string | null) =>
    request<JobsFunnel>("/meta/funnel", filtersToParams(filters, { lastVisit })),

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

/* ----------------------------------------------------------------- Matching */

export function fetchProfile(): Promise<ProfileSummary> {
  return request<ProfileSummary>("/profile");
}

/** Run the deterministic scoring pass. No LLM calls — pure CPU, ~9s for 917. */
export function runMatchScoring(force = false): Promise<MatchRun> {
  const params = new URLSearchParams();
  if (force) params.set("force", "true");
  return request<MatchRun>("/matches/score", params, { method: "POST" });
}

export function fetchMatches(opts: {
  minScore?: number;
  /** Ranker fit, LLM fit and Validator bands — OR within one, AND across. */
  bands?: MatchBandFilters;
  /** true = only jobs on the LLM shortlist. */
  shortlisted?: boolean | null;
  /** true = drop jobs whose JD states a hard blocker. */
  hideBlocked?: boolean;
  /** false = show every scored row, preference misses included. */
  matchPrefs?: boolean;
  sort?: MatchSort;
  limit?: number;
  offset?: number;
}): Promise<MatchList> {
  const p = new URLSearchParams();
  // The live Jobs filters are deliberately NOT sent. Matches works on the
  // scope sent with "Send to Matches", which the server re-applies on Run —
  // sending the URL's filters too would narrow the list a second time by
  // whatever the Jobs tab happens to show now, and the funnel would stop
  // adding up.
  if (opts.minScore) p.set("min_score", String(opts.minScore));
  for (const b of opts.bands?.fit ?? []) p.append("fit_band", b);
  for (const b of opts.bands?.llm ?? []) p.append("llm_band", b);
  for (const b of opts.bands?.validity ?? []) p.append("validity", b);
  if (opts.shortlisted != null) p.set("shortlisted", String(opts.shortlisted));
  if (opts.hideBlocked) p.set("hide_blocked", "true");
  // Default ON server-side. Sent explicitly only when switched off, so the
  // common request stays short.
  if (opts.matchPrefs === false) p.set("match_prefs", "false");
  if (opts.sort) p.set("sort", opts.sort);
  p.set("limit", String(opts.limit ?? 100));
  p.set("offset", String(opts.offset ?? 0));
  return request<MatchList>("/matches", p);
}

/* -------------------------------------------------------------- Preferences */

export function fetchPrefs(): Promise<Prefs> {
  return request<Prefs>("/prefs");
}

/**
 * Save preferences. A **merge** — the server folds each section into what is
 * already on disk, so the panel can save one section without resetting the
 * rest. `prefs.yaml` stays the source of truth on disk.
 *
 * Saving does not re-score, and does not need to: preferences select rows,
 * they never change a score. The next `runPipeline()` applies them, and that
 * pass is free.
 */
export function savePrefs(patch: PrefsPatch): Promise<Prefs> {
  return request<Prefs>("/prefs", undefined, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

/* ------------------------------------------------------- The "Run" pipeline */

/**
 * Start validity + ranking in the background and return at once.
 *
 * This never starts the deep read. It returns what the deep read WOULD cost
 * in `estimate`, and `startLlmScreen()` is the separate confirmation — a
 * ~2-hour, several-hundred-request pass is not something one click should
 * commit to silently.
 */
export function runPipeline(force = false): Promise<PipelineStatus> {
  const p = new URLSearchParams();
  if (force) p.set("force", "true");
  return request<PipelineStatus>("/matches/run", p, { method: "POST" });
}

export function pipelineStatus(): Promise<PipelineStatus> {
  return request<PipelineStatus>("/matches/run/status");
}

/** The deep read. Call only after the user has seen `LlmEstimate`. */
export function startLlmScreen(opts: { force?: boolean; limit?: number } = {}) {
  const p = new URLSearchParams();
  if (opts.force) p.set("force", "true");
  if (opts.limit != null) p.set("limit", String(opts.limit));
  return request<LlmStatus>("/matches/llm", p, { method: "POST" });
}

export function llmStatus(): Promise<LlmStatus> {
  return request<LlmStatus>("/matches/llm/status");
}

/** Re-price the deep read for a different per-pass cap (10/15/20/50). */
export function fetchLlmEstimate(limit: number): Promise<LlmEstimate> {
  return request<LlmEstimate>("/matches/llm/estimate", new URLSearchParams({ limit: String(limit) }));
}

/** Deep-read one job on request — one call, shortlisted or not. */
export function startLlmOne(jobId: number): Promise<LlmStatus> {
  return request<LlmStatus>(`/matches/${jobId}/llm`, undefined, { method: "POST" });
}

export function fetchMatchesFunnel(): Promise<MatchesFunnel> {
  return request<MatchesFunnel>("/matches/funnel");
}

/** "Send to Matches": these Jobs filters become the scope Matches works on. */
export function transferToMatches(filters: JobFilterSet): Promise<Prefs> {
  return request<Prefs>("/matches/transfer", undefined, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
}

export function clearTransfer(): Promise<Prefs> {
  return request<Prefs>("/matches/transfer", undefined, { method: "DELETE" });
}

/** The validity pass alone — for the Jobs tile, which wants badges without
 *  ranking anything. Free, no network, ~12s for 6,317 rows. */
export function runValidity(force = false): Promise<ValidityRun> {
  const p = new URLSearchParams();
  if (force) p.set("force", "true");
  return request<ValidityRun>("/validity/run", p, { method: "POST" });
}
