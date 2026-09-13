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

  /** Normalized commitment / work mode. `null` means the board never said —
   *  render as "not stated", never as a negative. Greenhouse publishes
   *  neither, so null is the common case, not an edge one. */
  employment_type: string | null;
  workplace_type: string | null;

  /** `null` is "not yet checked", which is NOT a low score — show no badge
   *  at all rather than a zero. */
  validity_score: number | null;
  validity_reasons: string[];
  /** When the verifier (Phase 2B's deterministic validity pass) last ran. */
  validity_checked_at: string | null;
  /** The LLM has read this exact JD text. */
  llm_read: boolean;
}

export interface JobDetail extends Job {
  description_html: string | null;
  description_text: string | null;
  raw_json: Record<string, unknown> | null;
  llm_validity: LlmValidity | null;
}

/** Coarse validity label. Mirrors `validation_runner._band`. */
export type ValidityBand = "solid" | "ok" | "questionable" | "suspect";

export function validityBand(score: number): ValidityBand {
  if (score >= 85) return "solid";
  if (score >= 70) return "ok";
  if (score >= 50) return "questionable";
  return "suspect";
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
  employment_types?: FacetEntry[];
  workplace_types?: FacetEntry[];
  /** `unscored` is reported apart from the bands because those rows pass
   *  every threshold — folding them in would make them look high-validity. */
  validity?: { bands: Partial<Record<ValidityBand, number>>; unscored: number };
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

/**
 * Which top-level surface the left nav is pointing at. Lives in the URL like
 * everything else here, so a view is linkable and back/forward moves between
 * them.
 */
export type ViewId = "jobs" | "matches";

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
  /** Minimum validity score. `null` = no gate. Rows the validity pass has
   *  never scored always pass, so this never empties a fresh board. */
  minValidity: number | null;
  sort: SortField;
  order: SortOrder;
}

/** Mirrors `job_filters.MAX_POSTING_AGE_DAYS`: the hard posting-age limit. */
export const MAX_POSTING_AGE_DAYS = 30;

export const DEFAULT_FILTERS: Filters = {
  q: "",
  qScope: "all",
  companies: [],
  ats: [],
  departments: [],
  remote: null,
  status: "open",
  // A posting older than a month is no use, so the list starts there. "Any
  // time" is still one click away, and the funnel shows what the window cut.
  postedWithinDays: MAX_POSTING_AGE_DAYS,
  newOnly: false,
  matchesPrefs: true,
  minValidity: null,
  sort: "first_seen_at",
  order: "desc",
};

/* ----------------------------------------------------------------- Matching */

/** Coarse label for a fit score. The panel shows these rather than the raw
 *  number: repeated scoring of the same job moves the number by a few points,
 *  so a 73-vs-68 distinction is noise dressed as precision. */
export type MatchBand = "excellent" | "strong" | "moderate" | "weak" | "poor";

export interface Match {
  job: Job;
  score: number;
  band: MatchBand;
  /** skill / years / family / india / fresh / penalty */
  subscores: Record<string, number>;
  match_reasons: string[];
  /** False when the JD named too few technologies for the score to mean much. */
  confident: boolean;
  matched_skills: string[];
  missing_stacks: string[];
  years_required: number | null;
  /** On the LLM shortlist: every free rule passed. */
  shortlisted: boolean;
  /** Why not, when it is not — e.g. `low_confidence`, `score_below:52<65`. */
  shortlist_reasons: string[];
  /** Hard blockers read from the JD for free, as `key:evidence`. */
  blockers: string[];
  llm_used: boolean;
  /** A current, successful deep read of this job against this profile. */
  llm_read: boolean;
  /** The FIT half of a deep read: band, reasons, strengths, gaps, blocked.
   *  The validity half lives on `Job.validity_*` / the drawer, because it is
   *  cached per JD rather than per (JD, profile). */
  llm_verdict: LlmFitVerdict | null;
  profile_version: string;
  /** Did this row clear the preference gate. Rows that did not are hidden
   *  from Matches by default and remain visible in the Jobs tile. */
  prefs_pass: boolean;
  /** Reasons for passes as well as misses, so the drawer can say
   *  "admitted because it stated no salary". */
  prefs_reasons: string[];
  prefs_version: string;
  /** USD pay, verbatim or formatted, when any source states it. */
  usd_pay: string | null;
  /** Where `usd_pay` was read: board field, deep-read quote, or JD prose. */
  usd_pay_source: "board" | "deep_read" | "jd" | null;
  scored_at: string;
}

/** The fit half of an LLM screen. */
export interface LlmFitVerdict {
  fit_band?: MatchBand;
  fit_reasons?: string[];
  strengths?: string[];
  gaps?: string[];
  /** A hard stop the free pass cannot see: sponsorship demanded, or on-site. */
  blocked?: boolean;
  model?: string;
  error?: string;
}

/** The validity half of an LLM screen, cached on the posting. */
export interface LlmValidity {
  posting_status?: "active" | "evergreen" | "ghost";
  status_reason?: string;
  seniority?: string;
  tech_stack?: string[];
  sponsorship_required?: "yes" | "no" | "unstated";
  location_policy?: string;
  work_mode?: string;
  compensation_text?: string;
  inconsistencies?: string[];
  model?: string;
}

export interface MatchList {
  total: number;
  limit: number;
  offset: number;
  profile_version: string;
  items: Match[];
  /** Histogram over the whole filtered set, not the current page. */
  bands: Partial<Record<MatchBand, number>>;
  shortlisted: number;
  /** Rows before the band selection — the "All" chip. `total` is the selected band. */
  total_all: number;
  /** Blocked jobs "Hide blocked" removes (counted before it applies). */
  blocked: number;
}

export interface MatchRun {
  profile_version: string;
  considered: number;
  /** Eligible jobs inside the scope sent from Jobs. */
  in_transfer: number;
  scored: number;
  updated: number;
  skipped: number;
  /** Counts from here down cover only the jobs in scope (`in_transfer`). */
  shortlisted: number;
  blocked: number;
  /** Verified below 70. */
  low_validity: number;
  low_confidence: number;
  bands: Partial<Record<MatchBand, number>>;
  duration_ms: number;
  error: string | null;
  /** Which preferences gated the pass, and how many rows they admitted.
   *  `considered - matching_prefs` is what preferences filtered out. */
  prefs_version: string;
  matching_prefs: number;
}

export interface ProfileSummary {
  version: string;
  full_name: string;
  location: string;
  total_years: number;
  ai_years: number;
  current_title: string;
  target_titles: string[];
  skills: Record<string, Record<string, number>>;
  gaps: Record<string, number>;
  min_annual_inr: number;
  needs_sponsorship: boolean;
  resume_files: Record<string, string>;
}

/** Sort options for the Matches panel. */
export type MatchSort = "score" | "posted_at" | "first_seen_at";

/* -------------------------------------------------------------- Preferences */

/** `prefs.yaml` — "what do I want out of the board right now?"
 *
 *  A third layer, distinct from the other two and deliberately so:
 *    - `filters.yaml` decides ELIGIBILITY at ingest (can I hold this job at
 *      all?). Not editable from the UI: it gates Telegram alerts.
 *    - `profile.yaml` is WHO I AM, and drives the fit score.
 *    - this is WHAT I WANT, and only gates the Matches list. Editing it costs
 *      a ~10s re-rank and zero LLM calls.
 *
 *  The Jobs tile ignores preferences entirely — it always shows everything.
 */
export interface Prefs {
  version: string;
  work: { workplace_types: string[]; employment_types: string[] };
  experience: { min_years: number; max_years: number };
  compensation: { min_annual_inr: number; require_stated: boolean };
  validity: { min_score: number };
  /** Posted within N days, 1..30. Never wider than a month. */
  freshness: { max_age_days: number };
  scope: { exclude_companies: string[]; exclude_ats: string[] };
  /** The Jobs filters sent with "Send to Matches"; null = every eligible job. */
  transfer: TransferInfo | null;
  /** Silence is a pass. Off only if you want to require stated facts. */
  include_unstated: boolean;
  updated_at: string | null;
  /** Served rather than hardcoded here: a client-side copy is how a valid
   *  option silently disappears from the UI after a backend change. */
  workplace_options: string[];
  employment_options: string[];
}

/** A partial preferences write. The server merges section by section. */
export interface PrefsPatch {
  work?: Partial<Prefs["work"]>;
  experience?: Partial<Prefs["experience"]>;
  compensation?: Partial<Prefs["compensation"]>;
  validity?: Partial<Prefs["validity"]>;
  freshness?: Partial<Prefs["freshness"]>;
  scope?: Partial<Prefs["scope"]>;
  include_unstated?: boolean;
}

/** `job_filters.JobFilterSet` — the Jobs filters as the API names them. */
export interface JobFilterSet {
  company: string[];
  ats: string[];
  department: string[];
  remote: boolean | null;
  q: string | null;
  q_scope: SearchScope;
  posted_within_days: number | null;
  first_seen_after: string | null;
  eligibility_pass: boolean | null;
  min_validity: number | null;
}

export interface TransferInfo {
  filters: JobFilterSet;
  /** Human labels for the filters, e.g. ["My roles", "Posted ≤ 30d", "Remote"]. */
  labels: string[];
  count_at_transfer: number;
  transferred_at: string | null;
}

/* ------------------------------------------------------------------ Funnel */

export interface FunnelStep {
  key: string;
  label: string;
  count: number;
  /** How many rows this step removed from the one before it. */
  dropped: number;
  /** What the dropped rows missed on, e.g. { "work mode": 61 }. */
  breakdown?: Record<string, number>;
  note?: string;
}

export interface JobsFunnel {
  steps: FunnelStep[];
  total: number;
}

export interface MatchesFunnel {
  steps: FunnelStep[];
  /** The stored verdicts predate the current preferences — Run to refresh. */
  stale: boolean;
  transfer: TransferInfo | null;
  shortlisted: number;
  read: number;
  /** Default jobs a pass reads. */
  cap: number;
}

/* ------------------------------------------------------- The "Run" pipeline */

export interface ValidityRun {
  considered: number;
  scored: number;
  unchanged: number;
  suspect: number;
  duplicates: number;
  llm_applied: number;
  bands: Partial<Record<ValidityBand, number>>;
  reasons: Record<string, number>;
  duration_ms: number;
  error: string | null;
}

/** What the deep read would cost. Shown BEFORE anything is spent — the
 *  pipeline never starts it, so this is the confirm dialog's content. */
export interface LlmEstimate {
  /** Jobs on the shortlist. */
  routed: number;
  /** Already read — free. */
  cached: number;
  /** Jobs this pass reads: min(cap, unread). */
  pending: number;
  /** The per-pass read limit this estimate was priced for (≤ 50). */
  cap: number;
  /** Unread shortlisted jobs left for a later pass. */
  over_cap: number;
  est_tokens: number;
  est_minutes: number;
  requests: number;
  /** The provider's long token cap: per day on Groq, per month on Mistral.
   *  0 / "" when the provider declares none. */
  token_cap: number;
  token_cap_period: "day" | "month" | "";
  tokens_used: number;
  /** This pass's share of `token_cap`. */
  cap_budget_pct: number;
  /** Pending rows that fit in what is left of the cap. */
  fits_in_cap: number;
  /** `LLM_PROVIDER` — "groq", "mistral" or "cerebras". */
  provider: string;
  model: string;
  configured: boolean;
  note: string;
}

export type PipelineStage =
  | "idle"
  | "validity"
  | "ranking"
  | "estimating"
  | "done"
  | "failed";

/** Result of a finished deep read (`LLMRunOut`). */
export interface LlmRun {
  profile_version: string;
  routed: number;
  cached: number;
  screened: number;
  failed: number;
  skipped_budget: number;
  requests: number;
  tokens: number;
  bands: Record<string, number>;
  statuses: Record<string, number>;
  blocked: number;
  duration_ms: number;
  error: string | null;
}

/** `GET /matches/llm/status` — polled while a deep read runs. */
export interface LlmStatus {
  state: "idle" | "running" | "done";
  total: number;
  done: number;
  cached: number;
  failed: number;
  tokens: number;
  current: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  result: LlmRun | null;
}

export interface PipelineStatus {
  stage: PipelineStage;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  profile_version: string;
  prefs_version: string;
  validity: ValidityRun | null;
  ranking: MatchRun | null;
  estimate: LlmEstimate | null;
}
