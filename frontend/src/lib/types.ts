/** Mirrors the Phase 1 Pydantic schemas (`backend/app/schemas.py`). */

export type JobStatus = "open" | "closed";
export type StatusFilter = JobStatus | "any";
export type SearchScope = "all" | "title";

export interface Job {
  id: number;
  source_key: string;
  ats: string;
  company: string;
  title: string;
  locations: string[];
  remote: boolean;
  department: string | null;
  apply_url: string;
  posted_at: string | null;
  updated_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  status: JobStatus;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  eligibility_pass: boolean;
  eligibility_reasons: string[];
}

export interface JobDetail extends Job {
  description_html: string | null;
  description_text: string | null;
  raw_json: Record<string, unknown> | null;
}

export interface JobList {
  total: number;
  limit: number;
  offset: number;
  items: Job[];
}

export interface FacetEntry {
  value: string;
  count: number;
}

export interface Facets {
  companies: FacetEntry[];
  ats: FacetEntry[];
  departments: FacetEntry[];
  totals: {
    matching: number;
    open: number;
    closed: number;
    remote: number;
    posted_last_7d: number;
    new_since: number;
  };
  last_ingest_finished_at: string | null;
}

/* ------------------------------------------------------------------ Ingest */

export type IngestState = "fresh" | "running" | "idle";
export type SourceState = "pending" | "fetching" | "done" | "failed" | "throttled";

export interface SourceProgress {
  source_id: string;
  company: string;
  ats: string;
  state: SourceState;
  fetched: number;
  new: number;
  eligible: number;
  error: string | null;
}

export interface IngestRunSummary {
  run_id: number;
  started_at: string;
  finished_at: string | null;
  fetched: number;
  eligible: number;
  new: number;
  updated: number;
  closed: number;
  errored: number;
  notified: number;
}

/** Answer shape of both POST /ingest/refresh and GET /ingest/status. */
export interface IngestStatus {
  state: IngestState;
  skipped: boolean;
  reason: string | null;
  run_id: number | null;
  started_at: string | null;
  finished_at: string | null;
  last_finished_at: string | null;
  sources_total: number;
  sources_done: number;
  sources: SourceProgress[];
  result: IngestRunSummary | null;
  error: string | null;
}

export interface Health {
  status: string;
  jobs_total: number;
  jobs_open: number;
  last_ingest_finished_at: string | null;
  supported_ats: string[];
}

export type SortField = "first_seen_at" | "posted_at" | "title" | "company" | "last_seen_at";
export type SortOrder = "asc" | "desc";

/** Every piece of dashboard state that lives in the URL. */
export interface Filters {
  q: string;
  qScope: SearchScope;
  companies: string[];
  ats: string[];
  departments: string[];
  remote: boolean | null;
  status: StatusFilter;
  postedWithinDays: number | null;
  newOnly: boolean;
  /**
   * Show only jobs that cleared the backend eligibility filter — India-
   * eligible, pays at or above the floor when it says, and a wanted
   * engineering role at the right level.
   *
   * Defaults to `true`, which is the one filter here that hides rows by
   * default. That is deliberate: ingest stores every posting it sees and
   * flags the misses rather than dropping them, so the audit trail survives,
   * but the dashboard is a place to find work, not to review the filter.
   * Turning this off is how you inspect what the rules cut, and the drawer
   * shows `eligibility_reasons` for any row.
   */
  matchesPrefs: boolean;
  sort: SortField;
  order: SortOrder;
}

export const DEFAULT_FILTERS: Filters = {
  q: "",
  qScope: "all",
  companies: [],
  ats: [],
  departments: [],
  remote: null,
  status: "open",
  postedWithinDays: null,
  newOnly: false,
  matchesPrefs: true,
  sort: "first_seen_at",
  order: "desc",
};
