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
  sort: "first_seen_at",
  order: "desc",
};
