/**
 * Answers every API call the dashboard makes, from the static snapshot.
 *
 * Three kinds of route, handled differently on purpose:
 *
 *   **Read, filter-dependent** (`/jobs`, `/meta/funnel`, `/matches`) — computed
 *   from the snapshot rows by `query.ts`, because the answer depends on what the
 *   visitor has selected and a captured payload could only ever be right for one
 *   combination.
 *
 *   **Read, fixed** (`/dashboard`, `/prefs`, `/profile`, `/documents/base`, the
 *   captured documents and their diffs) — served back verbatim. These were
 *   recorded from the real API, so they are right by construction.
 *
 *   **Write** — either applied to `state.ts` when the mutation is genuinely
 *   local (applying to a job, editing preferences, hand-editing LaTeX), or
 *   refused with a message that says what it would cost. A demo that silently
 *   pretended to run a deep read would be claiming an LLM call happened; a demo
 *   whose Apply button does nothing reads as broken. The line between those two
 *   is the whole design of this file.
 */

import type { Dashboard, IngestStatus, SortField, SortOrder } from "@/lib/types";
import {
  loadDescriptions,
  loadSnapshot,
  type JdEntry,
  type Snapshot,
} from "./snapshot";
import * as state from "./state";
import {
  buildMatchList,
  jobsFunnel,
  matchesFilters,
  parseFilters,
  parseMatchQuery,
  sortJobs,
} from "./query";

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/** A refusal the UI already knows how to render: `detail` becomes the message. */
const refuse = (detail: string, status = 400): Response => json({ detail }, status);

/** A short, stable digest of a .tex — the preview's cache-buster.
 *
 *  FNV-1a rather than anything cryptographic: this only has to change when the
 *  document does, and it has to be identical across reloads. */
function texHash(tex: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < tex.length; i += 1) {
    hash ^= tex.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

const NEEDS_BACKEND =
  "This is a static demo snapshot, so it has no backend to run that against.";

/* --------------------------------------------------------- the fake sweep */

/**
 * The sync screen, driven by a fabricated run.
 *
 * `/ingest/refresh` normally answers `fresh` and the dashboard paints from
 * storage — which is exactly what should happen on load here, since the
 * snapshot is the storage. But the sync screen is a real feature worth seeing,
 * so a *forced* refresh (the button, or `r`) walks a fabricated run through the
 * same states a real one does, sourced from the snapshot's own board list. It
 * finishes; it just never contacts a board.
 */
const SWEEP_MS = 5200;
let sweepStartedAt: number | null = null;

function boardList(snapshot: Snapshot): { source_id: string; company: string; ats: string }[] {
  const seen = new Map<string, { source_id: string; company: string; ats: string }>();
  for (const job of snapshot.jobs) {
    if (!seen.has(job.ats)) {
      seen.set(job.ats, { source_id: job.ats, company: job.ats, ats: job.ats });
    }
  }
  return [...seen.values()];
}

function sweepStatus(snapshot: Snapshot): IngestStatus {
  const boards = boardList(snapshot);
  const captured = snapshot.meta.ingest_status;
  const lastFinished = captured?.last_finished_at ?? snapshot.manifest.generated_at;

  if (sweepStartedAt === null) {
    return {
      state: "fresh",
      skipped: true,
      reason: `Demo snapshot taken ${new Date(snapshot.manifest.generated_at).toUTCString()} — no board is contacted.`,
      run_id: captured?.run_id ?? null,
      started_at: null,
      finished_at: null,
      last_finished_at: lastFinished,
      sources_total: boards.length,
      sources_done: boards.length,
      sources: boards.map((board) => ({ ...board, state: "done" as const, fetched: 0, new: 0, eligible: 0, error: null })),
      result: captured?.result ?? null,
      error: null,
    };
  }

  const elapsed = Date.now() - sweepStartedAt;
  const progress = Math.min(1, elapsed / SWEEP_MS);
  const done = Math.floor(progress * boards.length);
  const running = progress < 1;

  // Per-board counts come from the snapshot itself, so the numbers the sync
  // screen reports are the numbers that board actually contributed.
  const fetchedByAts = new Map<string, number>();
  const eligibleByAts = new Map<string, number>();
  for (const job of snapshot.jobs) {
    fetchedByAts.set(job.ats, (fetchedByAts.get(job.ats) ?? 0) + 1);
    if (job.eligibility_pass) {
      eligibleByAts.set(job.ats, (eligibleByAts.get(job.ats) ?? 0) + 1);
    }
  }

  const sources = boards.map((board, index) => {
    const finished = index < done;
    const active = index === done && running;
    return {
      ...board,
      state: finished ? ("done" as const) : active ? ("fetching" as const) : ("pending" as const),
      fetched: finished ? (fetchedByAts.get(board.ats) ?? 0) : 0,
      new: 0,
      eligible: finished ? (eligibleByAts.get(board.ats) ?? 0) : 0,
      error: null,
    };
  });

  if (!running) sweepStartedAt = null;

  return {
    state: running ? "running" : "idle",
    skipped: false,
    reason: running ? null : "Demo snapshot — nothing was re-fetched.",
    run_id: captured?.run_id ?? 1,
    started_at: new Date(Date.now() - elapsed).toISOString(),
    finished_at: running ? null : new Date().toISOString(),
    last_finished_at: lastFinished,
    sources_total: boards.length,
    sources_done: running ? done : boards.length,
    sources,
    result: captured?.result ?? null,
    error: null,
  };
}

/* ------------------------------------------------------------- decoration */

/**
 * Overlay this browser's application stages onto snapshot rows.
 *
 * `application_status` was captured as it stood when the snapshot was taken, so
 * without this a visitor's own Apply click would not show up on the row they
 * just clicked — and the Dashboard's counts would disagree with the table's
 * badges.
 */
type Decoratable = { id: number; application_status: string | null };

function decorateOne<T extends Decoratable>(row: T): T {
  const stage = state.applicationIndex().get(row.id);
  return stage ? { ...row, application_status: stage } : row;
}

function decorate<T extends Decoratable>(rows: T[]): T[] {
  const index = state.applicationIndex();
  if (index.size === 0) return rows;
  return rows.map((row) =>
    index.has(row.id) ? { ...row, application_status: index.get(row.id)! } : row,
  );
}

/* ----------------------------------------------------------------- reads */

async function jobsRoute(snapshot: Snapshot, params: URLSearchParams): Promise<Response> {
  const filters = parseFilters(params);
  const status = params.get("status") ?? "open";
  const limit = Math.min(Number(params.get("limit")) || 100, 500);
  const offset = Number(params.get("offset")) || 0;
  const sort = (params.get("sort") as SortField) ?? "first_seen_at";
  const order = (params.get("order") as SortOrder) ?? "desc";

  // A full-JD search needs the descriptions, so this is one of the two places
  // that pulls the lazy chunk. The funnel below is handed the same `jd`, which
  // is what keeps its keyword step equal to this list's total.
  const jd = filters.q && filters.q_scope === "all" ? await loadDescriptions() : snapshot.jd;
  const options = { status, now: Date.now(), jd };

  const matched = snapshot.jobs.filter((job) => matchesFilters(job, filters, options));
  const ordered = sortJobs(matched, sort, order);
  return json({
    total: matched.length,
    limit,
    offset,
    items: decorate(ordered.slice(offset, offset + limit)),
  });
}

async function jobDetailRoute(snapshot: Snapshot, id: number): Promise<Response> {
  const job = snapshot.jobs.find((row) => row.id === id);
  if (!job) return refuse("Job not found", 404);

  const jd = await loadDescriptions();
  const entry: JdEntry | undefined = jd[String(id)];

  // A row whose entry exists but whose `description_html` is null is NOT a gap
  // in the snapshot — 1,237 scored postings genuinely have no HTML in the real
  // database, only text, and the drawer already falls back to
  // `description_text` for exactly that case. Substituting a placeholder here
  // would override that fallback and hide a description the demo is carrying.
  // The placeholder belongs only where the snapshot really has nothing.
  const placeholder =
    "<p><em>The full description for this posting is not part of the demo " +
    "snapshot. Descriptions ship for the postings the default view shows, and " +
    "for every scored or tailored one.</em></p>";

  return json({
    ...decorateOne(job),
    description_html: entry ? entry.description_html : placeholder,
    description_text: entry?.description_text ?? null,
    llm_validity: entry?.llm_validity ?? null,
    raw_json: null,
  });
}

function facetsRoute(snapshot: Snapshot, params: URLSearchParams): Response {
  const gated = params.get("eligibility_pass") === "true";
  const facets = gated ? snapshot.meta.facets.eligible : snapshot.meta.facets.all;
  const since = params.get("since");
  if (!since) return json(facets);

  // `new_since` is the one facet number that depends on a client-supplied
  // instant, so it is the one that has to be recounted rather than replayed.
  const after = Date.parse(since);
  const newSince = Number.isNaN(after)
    ? facets.totals.new_since
    : snapshot.jobs.filter((job) => {
        if (gated && !job.eligibility_pass) return false;
        const seen = Date.parse(job.first_seen_at);
        return !Number.isNaN(seen) && seen > after;
      }).length;

  return json({ ...facets, totals: { ...facets.totals, new_since: newSince } });
}

async function funnelRoute(snapshot: Snapshot, params: URLSearchParams): Promise<Response> {
  const filters = parseFilters(params);
  const status = params.get("status") ?? "open";
  const jd = filters.q && filters.q_scope === "all" ? await loadDescriptions() : snapshot.jd;
  return json(jobsFunnel(snapshot.jobs, filters, { status, now: Date.now(), jd }));
}

async function matchesRoute(snapshot: Snapshot, params: URLSearchParams): Promise<Response> {
  const query = parseMatchQuery(params);
  const jd = query.q && query.q_scope === "all" ? await loadDescriptions() : snapshot.jd;
  const result = buildMatchList(snapshot.matches, query, {
    now: Date.now(),
    jd,
    appliedJobs: state.applicationIndex(),
  });

  return json({
    total: result.total,
    limit: query.limit,
    offset: query.offset,
    profile_version: snapshot.meta.profile?.version ?? "",
    // Rows embed their job, so the Apply badge has to be overlaid there too.
    items: result.items.map((row) => ({ ...row, job: decorateOne(row.job) })),
    bands: result.bands,
    llm_bands: result.llm_bands,
    validity_bands: result.validity_bands,
    shortlisted: result.shortlisted,
    total_all: result.total_all,
    blocked: result.blocked,
    applied: result.applied,
  });
}

/**
 * The Dashboard panel, with its application half recomputed.
 *
 * The board half (open, eligible, scored, shortlisted, token spend) is replayed
 * from the capture — those are facts about the snapshot. The application half is
 * derived from this browser's state, because a visitor who clicks Apply and then
 * opens the Dashboard must not be told they have no applications.
 */
function dashboardRoute(snapshot: Snapshot): Response {
  const captured = snapshot.meta.dashboard;
  if (!captured) return refuse("No dashboard payload in this snapshot", 404);

  const applications = state.listApplications();
  if (applications.length === 0) return json(captured);

  const byStatus: Record<string, number> = {};
  for (const application of applications) {
    byStatus[application.status] = (byStatus[application.status] ?? 0) + 1;
  }
  const now = Date.now();
  const within = (days: number) =>
    applications.filter((a) => now - Date.parse(a.applied_at) <= days * 86_400_000).length;

  const responded = applications.filter((a) =>
    a.history.some((entry) => entry.status && entry.status !== "applied" && entry.status !== "ghosted"),
  ).length;

  const next: Dashboard = {
    ...captured,
    by_status: byStatus,
    total_applications: applications.length,
    awaiting: byStatus.applied ?? 0,
    active: applications.filter((a) => !["rejected", "ghosted"].includes(a.status)).length,
    silent: applications.filter((a) => a.silent).length,
    applied_last_7d: within(7),
    applied_last_30d: within(30),
    response_rate: applications.length ? responded / applications.length : null,
  };
  return json(next);
}

function documentFor(snapshot: Snapshot, jobId: number, kind: string): Response {
  const found = snapshot.documents.byKey[`${jobId}:${kind}`];
  if (!found) return refuse("No document stored for this job", 404);
  return json(state.withEdits(found));
}

/* ----------------------------------------------------------------- writes */

function applyRoute(snapshot: Snapshot, body: unknown): Response {
  const payload = (body ?? {}) as { job_id?: number; source?: string };
  const jobId = Number(payload.job_id);
  const job = snapshot.jobs.find((row) => row.id === jobId);
  if (!job) return refuse("Job not found", 404);
  return json(state.upsertApplication(jobId, job, payload.source ?? "apply_click"));
}

function transferRoute(snapshot: Snapshot, body: unknown): Response {
  const prefs = state.getPrefs() ?? snapshot.meta.prefs;
  if (!prefs) return refuse("No preferences in this snapshot", 404);
  const filters = body as Record<string, unknown>;
  return json(
    state.mergePrefs({
      transfer: { filters, transferred_at: new Date().toISOString() },
    }) ?? prefs,
  );
}

/* ---------------------------------------------------------------- the router */

interface Parsed {
  method: string;
  segments: string[];
  params: URLSearchParams;
  body: unknown;
}

async function route(snapshot: Snapshot, request: Parsed): Promise<Response> {
  const { method, segments, params, body } = request;
  const [head, second, third, fourth] = segments;
  const meta = snapshot.meta;

  if (method === "GET") {
    switch (head) {
      case "health":
        return json(meta.health ?? { status: "demo" });
      case "companies":
        return json(meta.companies ?? []);
      case "jobs":
        return second ? jobDetailRoute(snapshot, Number(second)) : jobsRoute(snapshot, params);
      case "meta":
        if (second === "facets") return facetsRoute(snapshot, params);
        if (second === "funnel") return funnelRoute(snapshot, params);
        break;
      case "ingest":
        if (second === "status") return json(sweepStatus(snapshot));
        if (second === "runs") return json([]);
        break;
      case "profile":
        if (second === "document") {
          return meta.profile_document
            ? json(meta.profile_document)
            : refuse("No profile document in this snapshot", 404);
        }
        // A missing profile answers 200 with `configured: false`, because a
        // first run legitimately has none and the dashboard has to tell "not
        // set up" from "the server is broken".
        return json(meta.profile ?? { configured: false });
      case "prefs":
        return json(state.getPrefs() ?? meta.prefs ?? {});
      case "applications":
        return json({ total: state.listApplications().length, items: state.listApplications() });
      case "dashboard":
        return dashboardRoute(snapshot);
      case "matches":
        if (second === "funnel") {
          return meta.matches_funnel
            ? json(meta.matches_funnel)
            : refuse("No matches funnel in this snapshot", 404);
        }
        if (second === "llm" && third === "status") return json(meta.llm_status ?? {});
        if (second === "llm" && third === "estimate") {
          const limit = params.get("limit") ?? "15";
          const estimate = meta.llm_estimate[limit] ?? Object.values(meta.llm_estimate)[0];
          return estimate ? json(estimate) : refuse("No estimate in this snapshot", 404);
        }
        if (second === "run" && third === "status") return json(meta.pipeline_status ?? {});
        if (second && third === "document") {
          return documentFor(snapshot, Number(second), params.get("kind") ?? "resume");
        }
        return matchesRoute(snapshot, params);
      case "documents": {
        if (second === "base") {
          return meta.base_resume
            ? json(meta.base_resume)
            : refuse("No base résumé in this snapshot", 404);
        }
        const docId = Number(second);
        if (third === "diff") {
          const diff = snapshot.documents.diffs[String(docId)];
          return json(
            diff ?? {
              document_id: docId,
              kind: "resume",
              available: false,
              note: "No diff was captured for this document.",
              rows: [],
              reworded: 0,
              dropped: 0,
              moved: 0,
              unchanged: 0,
              added: 0,
            },
          );
        }
        if (third === "chat") {
          return json(
            snapshot.documents.chats[String(docId)] ?? {
              document_id: docId,
              messages: [],
              suggestions: [],
              tokens: 0,
            },
          );
        }
        if (third === "tex") {
          const document = snapshot.documents.byId[String(docId)];
          if (!document) return refuse("Document not found", 404);
          return new Response(state.withEdits(document).tex, {
            headers: { "Content-Type": "application/x-tex" },
          });
        }
        break;
      }
    }
  }

  if (method === "POST") {
    switch (head) {
      case "ingest":
        if (second === "refresh") {
          // Only a forced refresh runs the fabricated sweep. An unforced one
          // must answer `fresh`, or every page load would sit behind five
          // seconds of theatre.
          if (params.get("force") === "true") sweepStartedAt = Date.now();
          return json(sweepStatus(snapshot));
        }
        break;
      case "applications":
        return applyRoute(snapshot, body);
      case "validity":
        if (second === "run") {
          return json({
            scored: snapshot.jobs.filter((job) => job.validity_score !== null).length,
            skipped: 0,
            duplicates: 0,
            duration_s: 0,
            note: "Replayed from the snapshot — the verifier already ran on these rows.",
          });
        }
        break;
      case "matches":
        if (second === "score") {
          return json({
            ...(meta.pipeline_status ?? {}),
            scored: snapshot.matches.length,
            note: "Replayed from the snapshot — every row here is already scored.",
          });
        }
        if (second === "run") return json(meta.pipeline_status ?? {});
        if (second === "llm") {
          return refuse(
            `Running a deep read costs real LLM tokens. ${NEEDS_BACKEND} ` +
              `${snapshot.matches.filter((row) => row.llm_read).length} rows in this snapshot were read for real — filter by "LLM read" to see them.`,
          );
        }
        if (second && third === "llm") {
          return refuse(`Reading one job costs an LLM call. ${NEEDS_BACKEND}`);
        }
        if (second && third === "document") {
          const kind = params.get("kind") ?? "resume";
          const existing = snapshot.documents.byKey[`${second}:${kind}`];
          if (existing) return json(state.withEdits(existing));
          const available = Object.keys(snapshot.documents.byKey).length;
          return refuse(
            `Tailoring a document costs an LLM call. ${NEEDS_BACKEND} ` +
              `${available} documents were generated for real and ship with the demo — open one from a row that shows the Tailor badge.`,
          );
        }
        break;
      case "documents": {
        const docId = Number(second);
        const document = snapshot.documents.byId[String(docId)];
        if (!document) return refuse("Document not found", 404);
        if (third === "revert") {
          // Genuinely free, and genuinely correct: reverting replays the stored
          // tailoring, which is exactly what the snapshot holds.
          state.clearTex(docId);
          return json(document);
        }
        if (third === "compile") {
          // The PDF in `/demo/pdf/` IS the compile of this document's stored
          // LaTeX, done by the exporter with the real Tectonic. So an unedited
          // document compiles successfully here — saying otherwise would put an
          // error pane over a PDF the demo is already serving.
          //
          // A hand edit is the honest failure: the stored PDF no longer matches
          // the text on screen, and nothing in the browser can rebuild it.
          const edited = state.getTex(docId);
          if (!edited || edited === document.tex) {
            return json({
              ok: true,
              errors: [],
              log: "",
              duration_s: 0,
              // Stable for a given .tex, so the preview's cache-buster only
              // changes when the document does.
              pdf_hash: texHash(document.tex),
              fact_warnings: [],
            });
          }
          return json({
            ok: false,
            errors: [
              "Recompiling runs Tectonic on the server, and a static demo has none.",
              "Revert to see the exported PDF again — that one is a real compile of the stored LaTeX.",
            ],
            log: "",
            duration_s: 0,
            pdf_hash: texHash(document.tex),
            fact_warnings: [],
          });
        }
        if (third === "chat" && !fourth) {
          return refuse(`Each chat message costs an LLM call. ${NEEDS_BACKEND}`);
        }
        if (third === "chat" && fourth) {
          return refuse(`Applying a chat proposal needs the backend's fact-checker. ${NEEDS_BACKEND}`);
        }
        break;
      }
      case "profile":
        if (second === "upload") {
          return refuse(`Reading a résumé into a profile costs an LLM call. ${NEEDS_BACKEND}`);
        }
        break;
    }
  }

  if (method === "PUT") {
    switch (head) {
      case "prefs":
        return json(state.mergePrefs((body ?? {}) as Record<string, unknown>) ?? meta.prefs ?? {});
      case "profile":
        // Accepted, but the version is deliberately NOT bumped: every score and
        // document in the snapshot is keyed to this profile_version, and a bump
        // would correctly mark all of them stale and empty the Matches list.
        return json({ ...(meta.profile ?? {}), note: "Saved for this browser only — the demo keeps one profile version so the stored scores stay current." });
      case "matches":
        if (second === "transfer") return transferRoute(snapshot, body);
        break;
      case "documents": {
        const document = snapshot.documents.byId[String(Number(second))];
        if (!document) return refuse("Document not found", 404);
        const tex = ((body ?? {}) as { tex?: string }).tex;
        if (!tex) return refuse("No LaTeX in the request", 422);
        state.setTex(document.id, tex);
        return json(state.withEdits(document));
      }
    }
  }

  if (method === "PATCH" && head === "applications" && second) {
    const updated = state.patchApplication(Number(second), (body ?? {}) as { status?: never; notes?: string });
    return updated ? json(updated) : refuse("No application recorded for this job", 404);
  }

  if (method === "DELETE") {
    if (head === "applications" && second) {
      return json({ removed: state.deleteApplication(Number(second)) });
    }
    if (head === "matches" && second === "transfer") {
      const prefs = state.mergePrefs({ transfer: null });
      return json(prefs ?? meta.prefs ?? {});
    }
    if (head === "documents" && third === "chat") {
      return json({ document_id: Number(second), messages: [], suggestions: [], tokens: 0 });
    }
  }

  return refuse(`The demo has no handler for ${method} /${segments.join("/")}.`, 404);
}

/** Entry point for the fetch shim. `path` is already stripped of the API base. */
export async function handle(
  method: string,
  path: string,
  params: URLSearchParams,
  body: unknown,
): Promise<Response> {
  const snapshot = await loadSnapshot();
  state.initState({
    applications: snapshot.meta.applications?.items ?? [],
    prefs: snapshot.meta.prefs,
  });
  const segments = path.split("/").filter(Boolean);
  try {
    return await route(snapshot, { method, segments, params, body });
  } catch (error) {
    return refuse(
      `The demo hit an error answering ${method} /${segments.join("/")}: ${
        error instanceof Error ? error.message : String(error)
      }`,
      500,
    );
  }
}
