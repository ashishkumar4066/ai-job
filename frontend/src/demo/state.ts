/**
 * The demo's writable state: what a visitor changes while clicking around.
 *
 * Applications, preferences and hand edits to a document are real mutations in
 * the live app, and a demo where Apply does nothing reads as broken. They are
 * kept here, in memory and mirrored to `localStorage`, so they survive a reload
 * but never leave the browser.
 *
 * Every read and write is wrapped: `localStorage` throws in a private window and
 * on a blocked-cookies profile, and a demo must not white-screen because someone
 * has third-party storage disabled. A failed read means the visitor starts from
 * the snapshot's seed, which is a perfectly good state to be in.
 */

import type { Application, ApplicationStatus, Job, Prefs, TailoredDocument } from "@/lib/types";

const KEY = "demo.state.v1";

/** Mirrors the backend's own silence threshold (`applications.SILENT_AFTER_DAYS`). */
const SILENT_AFTER_DAYS = 30;

interface Persisted {
  applications: Record<string, Application>;
  prefs: Prefs | null;
  /** Hand-edited LaTeX, by document id. */
  tex: Record<string, string>;
  waitlisted: boolean;
}

const empty: Persisted = { applications: {}, prefs: null, tex: {}, waitlisted: false };

let state: Persisted = { ...empty };
let seeded = false;

function read(): Persisted {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...empty };
    return { ...empty, ...(JSON.parse(raw) as Partial<Persisted>) };
  } catch {
    return { ...empty };
  }
}

function write(): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* Private window or blocked storage — the session still works, it just
       won't survive a reload. Not worth telling the visitor about. */
  }
}

/** Load persisted state and, on a first visit, seed it from the snapshot. */
export function initState(seed: { applications: Application[]; prefs: Prefs | null }): void {
  state = read();
  if (!seeded && Object.keys(state.applications).length === 0) {
    for (const application of seed.applications) {
      state.applications[String(application.job_id)] = application;
    }
  }
  state.prefs ??= seed.prefs;
  seeded = true;
  write();
}

/* ------------------------------------------------------------- applications */

function daysBetween(iso: string, now: number): number {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return 0;
  return Math.max(0, Math.floor((now - then) / 86_400_000));
}

/**
 * Recompute `days_silent` / `silent` on every read, exactly as the backend does.
 *
 * Storing them would let a row that has gone quiet since the snapshot was taken
 * keep claiming it is active, which is the one thing the Dashboard's silence
 * band exists to catch.
 */
function derive(application: Application): Application {
  const now = Date.now();
  const days = daysBetween(application.status_changed_at, now);
  const open = !["rejected", "ghosted", "offer"].includes(application.status);
  return { ...application, days_silent: days, silent: open && days >= SILENT_AFTER_DAYS };
}

export function listApplications(): Application[] {
  return Object.values(state.applications)
    .map(derive)
    .sort((a, b) => Date.parse(b.applied_at) - Date.parse(a.applied_at));
}

export function getApplication(jobId: number): Application | null {
  const found = state.applications[String(jobId)];
  return found ? derive(found) : null;
}

/**
 * Record an application — idempotent, for the same reason the real endpoint is.
 *
 * The Apply button fires on every press, so re-opening a posting to check a
 * question must not drag `interviewing` back to `applied` or restamp a
 * three-week-old application as today's.
 */
export function upsertApplication(
  jobId: number,
  job: Pick<Job, "company" | "title" | "apply_url" | "locations" | "posted_at" | "status">,
  source: string,
): Application {
  const existing = state.applications[String(jobId)];
  if (existing) return derive(existing);

  const now = new Date().toISOString();
  const created: Application = {
    job_id: jobId,
    status: "applied",
    applied_at: now,
    status_changed_at: now,
    source,
    notes: null,
    days_silent: 0,
    silent: false,
    history: [{ status: "applied", at: now }],
    company: job.company,
    title: job.title,
    apply_url: job.apply_url,
    locations: job.locations,
    posted_at: job.posted_at,
    status_of_posting: job.status,
  };
  state.applications[String(jobId)] = created;
  write();
  return created;
}

export function patchApplication(
  jobId: number,
  patch: { status?: ApplicationStatus; notes?: string },
): Application | null {
  const existing = state.applications[String(jobId)];
  if (!existing) return null;

  const now = new Date().toISOString();
  const next: Application = { ...existing };
  if (patch.status && patch.status !== existing.status) {
    next.status = patch.status;
    next.status_changed_at = now;
    next.history = [...existing.history, { status: patch.status, at: now }];
  }
  if (patch.notes !== undefined) next.notes = patch.notes;
  state.applications[String(jobId)] = next;
  write();
  return derive(next);
}

export function deleteApplication(jobId: number): boolean {
  const had = Boolean(state.applications[String(jobId)]);
  delete state.applications[String(jobId)];
  write();
  return had;
}

/** Job id -> stage, for the `application_status` the job rows carry. */
export function applicationIndex(): Map<number, ApplicationStatus> {
  return new Map(
    Object.values(state.applications).map((a) => [a.job_id, a.status] as const),
  );
}

/* ------------------------------------------------------------- preferences */

export function getPrefs(): Prefs | null {
  return state.prefs;
}

/**
 * Merge a patch into stored preferences, the way `PUT /prefs` does.
 *
 * A merge, not a replace: the panel saves one section at a time, so replacing
 * would reset every other section to its default on each save.
 */
export function mergePrefs(patch: Record<string, unknown>): Prefs | null {
  if (!state.prefs) return null;
  const merged: Record<string, unknown> = { ...(state.prefs as unknown as Record<string, unknown>) };
  for (const [key, value] of Object.entries(patch)) {
    if (value === null || value === undefined) continue;
    const current = merged[key];
    if (current && typeof current === "object" && !Array.isArray(current) && typeof value === "object" && !Array.isArray(value)) {
      merged[key] = { ...(current as object), ...(value as object) };
    } else {
      merged[key] = value;
    }
  }
  merged.updated_at = new Date().toISOString();
  state.prefs = merged as unknown as Prefs;
  write();
  return state.prefs;
}

export function setPrefs(prefs: Prefs): Prefs {
  state.prefs = prefs;
  write();
  return prefs;
}

/* ------------------------------------------------------------- documents */

export function getTex(docId: number): string | null {
  return state.tex[String(docId)] ?? null;
}

export function setTex(docId: number, tex: string): void {
  state.tex[String(docId)] = tex;
  write();
}

export function clearTex(docId: number): void {
  delete state.tex[String(docId)];
  write();
}

/** A stored document with any hand edit from this browser applied on top. */
export function withEdits(document: TailoredDocument): TailoredDocument {
  const edited = getTex(document.id);
  if (!edited || edited === document.tex) return document;
  return { ...document, tex: edited, hand_edited: true };
}

/* ------------------------------------------------------------- waitlist */

export function isWaitlisted(): boolean {
  return state.waitlisted;
}

export function markWaitlisted(): void {
  state.waitlisted = true;
  write();
}
