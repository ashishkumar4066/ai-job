/**
 * The demo's filtering, sorting and funnel arithmetic.
 *
 * This is a deliberate port of `backend/app/job_filters.py`, not a
 * re-imagining. That module exists because the dashboard once showed four
 * numbers that could not be reconciled, and the cure was that every count on
 * screen is built from one filter definition. A demo that filtered "roughly the
 * same way" would reintroduce exactly the bug the original design removed: the
 * funnel's last step must equal the table's total *by construction*, and facet
 * counts must match the rows they claim to describe.
 *
 * Where the port has to differ, it differs explicitly and says so:
 *   - Full-JD keyword search can only see the descriptions the snapshot carries
 *     (`jd.json`, the eligible rows). `matchesKeyword()` is the single place that
 *     knows this, so the list and the funnel are wrong together or right
 *     together — never in disagreement.
 *   - SQLite's default string ordering is byte-wise and case-sensitive. Plain
 *     `<`/`>` on the raw strings is used rather than `localeCompare`, because
 *     the latter is case-insensitive and would sort a title list differently
 *     from the live app.
 */

import type { Job, SearchScope, SortField, SortOrder } from "@/lib/types";
import type { JdEntry, MatchRow } from "./snapshot";

/** The `/jobs` query parameters, parsed. Mirrors `JobFilterSet`. */
export interface DemoFilters {
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

export const emptyFilters: DemoFilters = {
  company: [],
  ats: [],
  department: [],
  remote: null,
  q: null,
  q_scope: "all",
  posted_within_days: null,
  first_seen_after: null,
  eligibility_pass: null,
  min_validity: null,
};

export function parseFilters(params: URLSearchParams): DemoFilters {
  const number = (key: string): number | null => {
    const raw = params.get(key);
    if (raw === null || raw === "") return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const bool = (key: string): boolean | null => {
    const raw = params.get(key);
    if (raw === null) return null;
    return raw === "true" || raw === "1";
  };
  const q = params.get("q")?.trim() || null;
  return {
    company: params.getAll("company").filter(Boolean),
    ats: params.getAll("ats").filter(Boolean),
    department: params.getAll("department").filter(Boolean),
    remote: bool("remote"),
    q,
    q_scope: (params.get("q_scope") as SearchScope) ?? "all",
    posted_within_days: number("posted_within_days"),
    first_seen_after: params.get("first_seen_after"),
    eligibility_pass: bool("eligibility_pass"),
    min_validity: number("min_validity"),
  };
}

/**
 * "Remote" means one thing, as it does server-side: the board's own
 * `workplace_type` when it published one, else the keyword flag. The two used
 * to disagree and agreed on the live board only by coincidence.
 */
export function isRemote(job: Job): boolean {
  if (job.workplace_type != null) return job.workplace_type === "remote";
  return job.remote;
}

const time = (iso: string | null): number | null => {
  if (!iso) return null;
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? null : parsed;
};

/**
 * Posted in the last `days`, falling back to first-seen when undated — the same
 * fallback the SQL has, so a row with no date cannot become immortal.
 */
function postedWithin(job: Job, days: number, now: number): boolean {
  const cutoff = now - days * 86_400_000;
  const posted = time(job.posted_at);
  if (posted !== null) return posted >= cutoff;
  const seen = time(job.first_seen_at);
  return seen !== null && seen >= cutoff;
}

const lower = (value: string) => value.toLowerCase();

/**
 * Does a keyword hit this row? See the note at the top of the file on what the
 * `all` scope can actually see here.
 *
 * The fields are tested separately rather than joined into one string, because
 * joining lets a query match across a boundary: "acme engineer" would hit a row
 * titled "Engineer" at a company called "Acme" purely by accident of
 * concatenation order, which the SQL's OR of three ILIKEs never does.
 */
function matchesKeyword(
  job: Job,
  needle: string,
  scope: SearchScope,
  jd: Record<string, JdEntry> | null,
): boolean {
  const target = lower(needle);
  if (lower(job.title).includes(target)) return true;
  if (scope === "title") return false;
  if (lower(job.company).includes(target)) return true;
  const description = jd?.[String(job.id)]?.description_text;
  return description ? lower(description).includes(target) : false;
}

export function matchesFilters(
  job: Job,
  filters: DemoFilters,
  options: { status: string; now: number; jd: Record<string, JdEntry> | null },
): boolean {
  const { status, now, jd } = options;
  if (status !== "any" && job.status !== status) return false;

  if (filters.company.length) {
    const wanted = filters.company.map(lower);
    if (!wanted.includes(lower(job.company))) return false;
  }
  if (filters.ats.length) {
    const wanted = filters.ats.map(lower);
    if (!wanted.includes(lower(job.ats))) return false;
  }
  if (filters.remote !== null && isRemote(job) !== filters.remote) return false;
  if (filters.eligibility_pass !== null && job.eligibility_pass !== filters.eligibility_pass) {
    return false;
  }
  if (filters.min_validity) {
    // A row the validity pass never scored (null) passes. Treating null as 0
    // would empty the table the moment anyone touched this filter — "not yet
    // checked" is not "worthless".
    if (job.validity_score !== null && job.validity_score < filters.min_validity) return false;
  }
  if (filters.department.length) {
    const wanted = filters.department.map(lower);
    if (!job.department || !wanted.includes(lower(job.department))) return false;
  }
  if (filters.posted_within_days !== null && !postedWithin(job, filters.posted_within_days, now)) {
    return false;
  }
  if (filters.first_seen_after) {
    const after = time(filters.first_seen_after);
    const seen = time(job.first_seen_at);
    if (after !== null && (seen === null || seen <= after)) return false;
  }
  if (filters.q && !matchesKeyword(job, filters.q, filters.q_scope, jd)) return false;
  return true;
}

/* ----------------------------------------------------------------- sorting */

/**
 * `/jobs`'s exact ordering, tie-breaks included.
 *
 * The `posted_at` fallback is not decoration: a bulk first ingest stamps every
 * row with an identical `first_seen_at`, and an id-only tie-break would collapse
 * the default view to a single company. The final tie-break on id is what stops
 * pagination repeating or skipping a row.
 */
export function sortJobs(rows: Job[], sort: SortField, order: SortOrder): Job[] {
  const direction = order === "desc" ? -1 : 1;
  const key = (job: Job): number | string | null => {
    switch (sort) {
      case "title":
        return job.title;
      case "company":
        return job.company;
      case "posted_at":
        return time(job.posted_at);
      case "last_seen_at":
        return time(job.last_seen_at);
      default:
        return time(job.first_seen_at);
    }
  };

  const compare = (a: number | string | null, b: number | string | null): number => {
    // NULLs last in either direction, matching SQLite's NULLS-last ordering on
    // a DESC sort — the default view's common case.
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    if (a === b) return 0;
    return a < b ? -1 * direction : 1 * direction;
  };

  return [...rows].sort((a, b) => {
    const primary = compare(key(a), key(b));
    if (primary !== 0) return primary;
    if (sort !== "posted_at") {
      const posted = time(b.posted_at) ?? -Infinity;
      const other = time(a.posted_at) ?? -Infinity;
      if (posted !== other) return posted - other > 0 ? 1 : -1;
    }
    return b.id - a.id;
  });
}

/* ------------------------------------------------------------------ funnel */

/** `FUNNEL_ORDER` from `job_filters.py`, in the same sequence. */
const FUNNEL_ORDER: { key: keyof DemoFilters; label: string }[] = [
  { key: "eligibility_pass", label: "Eligible (My roles)" },
  { key: "posted_within_days", label: "Posted recently" },
  { key: "remote", label: "Work mode" },
  { key: "min_validity", label: "Validity" },
  { key: "company", label: "Company" },
  { key: "ats", label: "Platform" },
  { key: "department", label: "Department" },
  { key: "q", label: "Keyword" },
  { key: "first_seen_after", label: "New since last visit" },
];

/**
 * An active filter is one the server would have put in the chain.
 *
 * `remote: false` counts as active even though it is falsy — "Not remote" is a
 * real choice, and the Python has the same explicit exception.
 */
function isActive(key: keyof DemoFilters, value: unknown): boolean {
  if (key === "remote") return value !== null && value !== undefined;
  if (Array.isArray(value)) return value.length > 0;
  return value !== null && value !== undefined && value !== "" && value !== false;
}

export function jobsFunnel(
  jobs: Job[],
  filters: DemoFilters,
  options: { status: string; now: number; jd: Record<string, JdEntry> | null },
): { steps: { key: string; label: string; count: number; dropped: number }[]; total: number } {
  const startLabel =
    options.status === "open" ? "Open jobs" : options.status === "closed" ? "Closed jobs" : "All jobs";
  const start = jobs.filter((job) => options.status === "any" || job.status === options.status).length;

  const steps = [{ key: "start", label: startLabel, count: start, dropped: 0 }];
  let previous = start;
  const applied: DemoFilters = { ...emptyFilters, q_scope: filters.q_scope };

  for (const { key, label } of FUNNEL_ORDER) {
    const value = filters[key];
    if (!isActive(key, value)) continue;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (applied as any)[key] = value;

    let stepLabel = label;
    if (key === "posted_within_days") stepLabel = `Posted ≤ ${value as number}d`;
    else if (key === "remote") stepLabel = value ? "Remote" : "Not remote";
    else if (key === "min_validity") stepLabel = `Validity ≥ ${value as number}`;

    const count = jobs.filter((job) => matchesFilters(job, applied, options)).length;
    steps.push({ key, label: stepLabel, count, dropped: previous - count });
    previous = count;
  }

  return { steps, total: previous };
}

/* ----------------------------------------------------------------- matches */

/** `validityBand`'s inverse: which band label a score falls in. */
function validityBandOf(score: number | null): string {
  if (score === null) return "unchecked";
  if (score >= 85) return "solid";
  if (score >= 70) return "ok";
  if (score >= 50) return "questionable";
  return "suspect";
}

export interface MatchQuery {
  company: string[];
  ats: string[];
  department: string[];
  q: string | null;
  q_scope: SearchScope;
  posted_within_days: number | null;
  min_score: number;
  fit_band: string[];
  llm_band: string[];
  validity: string[];
  shortlisted: boolean | null;
  hide_blocked: boolean;
  applied: boolean | null;
  match_prefs: boolean;
  sort: "score" | "posted_at" | "first_seen_at";
  order: SortOrder;
  limit: number;
  offset: number;
}

export function parseMatchQuery(params: URLSearchParams): MatchQuery {
  const bool = (key: string): boolean | null => {
    const raw = params.get(key);
    if (raw === null) return null;
    return raw === "true" || raw === "1";
  };
  const int = (key: string, fallback: number): number => {
    const parsed = Number(params.get(key));
    return Number.isFinite(parsed) ? parsed : fallback;
  };
  return {
    company: params.getAll("company").filter(Boolean),
    ats: params.getAll("ats").filter(Boolean),
    department: params.getAll("department").filter(Boolean),
    q: params.get("q")?.trim() || null,
    q_scope: (params.get("q_scope") as SearchScope) ?? "all",
    posted_within_days: params.get("posted_within_days") ? int("posted_within_days", 30) : null,
    min_score: int("min_score", 0),
    fit_band: params.getAll("fit_band").filter(Boolean),
    llm_band: params.getAll("llm_band").filter(Boolean),
    validity: params.getAll("validity").filter(Boolean),
    shortlisted: bool("shortlisted"),
    hide_blocked: bool("hide_blocked") === true,
    applied: bool("applied"),
    // Default ON server-side; the client only sends it when switching off.
    match_prefs: params.get("match_prefs") !== "false",
    sort: (params.get("sort") as MatchQuery["sort"]) ?? "score",
    order: (params.get("order") as SortOrder) ?? "desc",
    limit: int("limit", 100),
    offset: int("offset", 0),
  };
}

/**
 * Build the whole `/matches` answer, in the order the real endpoint builds it.
 *
 * The order is the specification, not an implementation detail, because three
 * of the returned numbers are explicitly counted at a particular point in the
 * chain:
 *
 *   1. job filters + preference gate + `min_score`
 *   2. `blocked` counted here — it is what "Hide blocked" *would* remove
 *   3. `hide_blocked`, then the `shortlisted` filter
 *   4. `total_all`, `shortlisted` and `applied` counted here, and each band
 *      histogram counted with the OTHER two band filters applied but not its
 *      own — so a chip says how many rows selecting it would add, instead of
 *      collapsing to the current selection
 *   5. the band filters, then the `applied` filter
 *   6. `total` counted last
 *
 * Counting any of them one step earlier or later changes what the header says.
 */
export interface MatchListResult {
  items: MatchRow[];
  total: number;
  total_all: number;
  bands: Record<string, number>;
  llm_bands: Record<string, number>;
  validity_bands: Record<string, number>;
  shortlisted: number;
  blocked: number;
  applied: number;
}

const FIT_BAND_OF = (score: number): string => {
  if (score >= 80) return "excellent";
  if (score >= 65) return "strong";
  if (score >= 50) return "moderate";
  if (score >= 35) return "weak";
  return "poor";
};

/** The LLM's band, only where a current, successful deep read exists. */
const LLM_BAND_OF = (row: MatchRow): string =>
  row.llm_read && row.llm_verdict?.fit_band ? String(row.llm_verdict.fit_band) : "unread";

export function buildMatchList(
  rows: MatchRow[],
  query: MatchQuery,
  options: {
    now: number;
    jd: Record<string, JdEntry> | null;
    appliedJobs: Map<number, unknown>;
  },
): MatchListResult {
  const jobFilters: DemoFilters = {
    ...emptyFilters,
    company: query.company,
    ats: query.ats,
    department: query.department,
    q: query.q,
    q_scope: query.q_scope,
    posted_within_days: query.posted_within_days,
    // Matching only ever scores open+eligible rows, and the real endpoint
    // re-applies the gate so a job that closed after the pass drops out.
    eligibility_pass: true,
  };
  const jobOptions = { status: "open", now: options.now, jd: options.jd };

  // Step 1.
  let working = rows.filter(
    (row) =>
      matchesFilters(row.job, jobFilters, jobOptions) &&
      row.score >= query.min_score &&
      (!query.match_prefs || row.prefs_pass),
  );

  // Step 2 — before "Hide blocked" applies.
  const blocked = working.filter((row) => row.blockers.length > 0).length;

  // Step 3.
  if (query.hide_blocked) working = working.filter((row) => row.blockers.length === 0);
  if (query.shortlisted !== null) {
    working = working.filter((row) => row.shortlisted === query.shortlisted);
  }

  // Step 4 — every headline count, taken here.
  const total_all = working.length;
  const shortlisted = working.filter((row) => row.shortlisted).length;
  const applied = working.filter((row) => options.appliedJobs.has(row.job.id)).length;

  const wantFit = new Set(query.fit_band.map((b) => b.toLowerCase()));
  const wantLlm = new Set(query.llm_band.map((b) => b.toLowerCase()));
  const wantValidity = new Set(query.validity.map((b) => b.toLowerCase()));

  const bands: Record<string, number> = {};
  const llm_bands: Record<string, number> = {};
  const validity_bands: Record<string, number> = {};

  for (const row of working) {
    const keys = [FIT_BAND_OF(row.score), LLM_BAND_OF(row), validityBandOf(row.job.validity_score)];
    const hits = [
      wantFit.size === 0 || wantFit.has(keys[0]!),
      wantLlm.size === 0 || wantLlm.has(keys[1]!),
      wantValidity.size === 0 || wantValidity.has(keys[2]!),
    ];
    const counters = [bands, llm_bands, validity_bands];
    for (let i = 0; i < 3; i += 1) {
      if (hits.every((hit, j) => j === i || hit)) {
        const key = keys[i]!;
        counters[i]![key] = (counters[i]![key] ?? 0) + 1;
      }
    }
  }

  // Step 5.
  if (wantFit.size) working = working.filter((row) => wantFit.has(FIT_BAND_OF(row.score)));
  if (wantLlm.size) working = working.filter((row) => wantLlm.has(LLM_BAND_OF(row)));
  if (wantValidity.size) {
    working = working.filter((row) => wantValidity.has(validityBandOf(row.job.validity_score)));
  }
  if (query.applied !== null) {
    working = working.filter((row) => options.appliedJobs.has(row.job.id) === query.applied);
  }

  // Step 6.
  const ordered = sortMatches(working, query.sort, query.order);
  return {
    items: ordered.slice(query.offset, query.offset + query.limit),
    total: working.length,
    total_all,
    bands,
    llm_bands,
    validity_bands,
    shortlisted,
    blocked,
    applied,
  };
}

export function sortMatches(rows: MatchRow[], sort: MatchQuery["sort"], order: SortOrder): MatchRow[] {
  const direction = order === "desc" ? -1 : 1;
  const key = (row: MatchRow): number | null => {
    if (sort === "score") return row.score;
    if (sort === "posted_at") return time(row.job.posted_at);
    return time(row.job.first_seen_at);
  };
  return [...rows].sort((a, b) => {
    const left = key(a);
    const right = key(b);
    if (left === right) {
      // Ties on score fall back to recency, so an arbitrary id order does not
      // bury a fresh posting under a months-old one with the same number.
      if (sort === "score") {
        const postedA = time(a.job.posted_at) ?? -Infinity;
        const postedB = time(b.job.posted_at) ?? -Infinity;
        if (postedA !== postedB) return postedB - postedA > 0 ? 1 : -1;
      }
      return b.job.id - a.job.id;
    }
    if (left === null) return 1;
    if (right === null) return -1;
    return left < right ? -1 * direction : 1 * direction;
  });
}
