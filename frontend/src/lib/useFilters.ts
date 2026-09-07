import { useCallback, useEffect, useMemo, useState } from "react";
import { DEFAULT_FILTERS, type Filters, type SortField, type SortOrder } from "./types";
import type { SearchScope, StatusFilter } from "./types";

/**
 * Filter state lives in the URL so every view is bookmarkable and shareable.
 *
 * Only non-default values are serialized, which keeps links short and makes
 * `parse(serialize(f)) === f` easy to reason about.
 */

const PARAM = {
  q: "q",
  scope: "scope",
  company: "company",
  ats: "ats",
  department: "dept",
  remote: "remote",
  status: "status",
  posted: "posted",
  newOnly: "new",
  matchesPrefs: "prefs",
  sort: "sort",
  order: "order",
  job: "job",
} as const;

const SORTS: SortField[] = ["first_seen_at", "posted_at", "title", "company", "last_seen_at"];
const STATUSES: StatusFilter[] = ["open", "closed", "any"];

export function parseFilters(search: string): Filters {
  const params = new URLSearchParams(search);

  const sort = params.get(PARAM.sort) as SortField | null;
  const order = params.get(PARAM.order);
  const status = params.get(PARAM.status) as StatusFilter | null;
  const scope = params.get(PARAM.scope);
  const remote = params.get(PARAM.remote);
  const posted = params.get(PARAM.posted);
  const postedDays = posted ? Number.parseInt(posted, 10) : Number.NaN;

  return {
    q: params.get(PARAM.q) ?? "",
    qScope: scope === "title" ? "title" : "all",
    companies: params.getAll(PARAM.company).filter(Boolean),
    ats: params.getAll(PARAM.ats).filter(Boolean),
    departments: params.getAll(PARAM.department).filter(Boolean),
    remote: remote === "true" ? true : remote === "false" ? false : null,
    status: status && STATUSES.includes(status) ? status : "open",
    postedWithinDays: Number.isFinite(postedDays) && postedDays > 0 ? postedDays : null,
    newOnly: params.get(PARAM.newOnly) === "1",
    // Defaults ON, so its ABSENCE means on and `prefs=0` means off.
    matchesPrefs: params.get(PARAM.matchesPrefs) !== "0",
    sort: sort && SORTS.includes(sort) ? sort : "first_seen_at",
    order: order === "asc" ? "asc" : "desc",
  };
}

export function serializeFilters(filters: Filters, jobId: number | null): string {
  const params = new URLSearchParams();

  if (filters.q.trim()) {
    params.set(PARAM.q, filters.q.trim());
    if (filters.qScope !== DEFAULT_FILTERS.qScope) params.set(PARAM.scope, filters.qScope);
  }
  for (const value of filters.companies) params.append(PARAM.company, value);
  for (const value of filters.ats) params.append(PARAM.ats, value);
  for (const value of filters.departments) params.append(PARAM.department, value);
  if (filters.remote !== null) params.set(PARAM.remote, String(filters.remote));
  if (filters.status !== DEFAULT_FILTERS.status) params.set(PARAM.status, filters.status);
  if (filters.postedWithinDays !== null) {
    params.set(PARAM.posted, String(filters.postedWithinDays));
  }
  if (filters.newOnly) params.set(PARAM.newOnly, "1");
  if (!filters.matchesPrefs) params.set(PARAM.matchesPrefs, "0");
  if (filters.sort !== DEFAULT_FILTERS.sort) params.set(PARAM.sort, filters.sort);
  if (filters.order !== DEFAULT_FILTERS.order) params.set(PARAM.order, filters.order);
  if (jobId !== null) params.set(PARAM.job, String(jobId));

  const query = params.toString();
  return query ? `?${query}` : "";
}

function parseJobId(search: string): number | null {
  const raw = new URLSearchParams(search).get(PARAM.job);
  if (!raw) return null;
  const id = Number.parseInt(raw, 10);
  return Number.isFinite(id) ? id : null;
}

/** How many filters are narrowing the result set (drives the "clear" badge). */
export function countActive(filters: Filters): number {
  let count = 0;
  if (filters.q.trim()) count++;
  count += filters.companies.length;
  count += filters.ats.length;
  count += filters.departments.length;
  if (filters.remote !== null) count++;
  if (filters.status !== DEFAULT_FILTERS.status) count++;
  if (filters.postedWithinDays !== null) count++;
  if (filters.newOnly) count++;
  // Counted only when turned OFF: the badge tracks deviation from the
  // default view, and this filter is on in the default view.
  if (!filters.matchesPrefs) count++;
  return count;
}

export function useFilters() {
  const [search, setSearch] = useState(() => window.location.search);

  // Back/forward must restore the previous view.
  useEffect(() => {
    const onPopState = () => setSearch(window.location.search);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const filters = useMemo(() => parseFilters(search), [search]);
  const selectedJobId = useMemo(() => parseJobId(search), [search]);

  const write = useCallback((next: Filters, jobId: number | null, push: boolean) => {
    const query = serializeFilters(next, jobId);
    const url = `${window.location.pathname}${query}`;
    // Filter changes replace history; opening a job pushes, so Esc/back closes it.
    window.history[push ? "pushState" : "replaceState"](null, "", url);
    setSearch(query);
  }, []);

  const patch = useCallback(
    (changes: Partial<Filters>) => {
      // Changing a filter closes the drawer: the open job may no longer match.
      write({ ...parseFilters(window.location.search), ...changes }, null, false);
    },
    [write],
  );

  const reset = useCallback(() => write(DEFAULT_FILTERS, null, false), [write]);

  const selectJob = useCallback(
    (id: number | null) => {
      const current = parseFilters(window.location.search);
      write(current, id, id !== null);
    },
    [write],
  );

  const toggleInList = useCallback(
    (key: "companies" | "ats" | "departments", value: string) => {
      const current = parseFilters(window.location.search);
      const list = current[key];
      const next = list.includes(value)
        ? list.filter((item) => item !== value)
        : [...list, value];
      write({ ...current, [key]: next }, null, false);
    },
    [write],
  );

  const setSort = useCallback(
    (sort: SortField, order: SortOrder) => patch({ sort, order }),
    [patch],
  );

  const setScope = useCallback((qScope: SearchScope) => patch({ qScope }), [patch]);

  return {
    filters,
    selectedJobId,
    patch,
    reset,
    selectJob,
    toggleInList,
    setSort,
    setScope,
    activeCount: countActive(filters),
  };
}
