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
 *   **Write** — applied to `state.ts` when the mutation is genuinely local
 *   (applying to a job, editing preferences, hand-editing LaTeX), stood in for
 *   by `fake/` when it would have cost an LLM call, or refused with a message
 *   that says what it would cost.
 *
 * The line between those last two is the whole design of this file. A demo
 * whose Apply button does nothing reads as broken, and a demo whose headline
 * feature answers "not available" is not showing the feature — so the deep
 * read, tailoring, the cover letter and chat all *do* something. What none of
 * them does is claim a model was called: a composed verdict carries
 * `model: "demo-stand-in"`, a generated document is stored `llm_used: false`,
 * and `fake/` only ever re-orders or selects from material the real pipeline
 * already produced. See DEMO.md for what each one composes from.
 */

import type {
  ChatMessage,
  Dashboard,
  DocumentKind,
  IngestStatus,
  LlmEstimate,
  LlmStatus,
  SortField,
  SortOrder,
  TailoredDocument,
} from "@/lib/types";
import {
  loadDescriptions,
  loadSnapshot,
  type JdEntry,
  type MatchRow,
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
import { deepReadTargets, synthesizeVerdict, verdictTokens } from "./fake/screen";
import {
  asDiff,
  asDocument,
  coverLetter,
  keywordsFor,
  tailorResume,
} from "./fake/documents";
import {
  applyProposal,
  emptyChat,
  reply as chatReply,
  suggestionsFor,
  userMessage,
} from "./fake/chat";
import { texToBlocks } from "./fake/latex";
import { LETTER_STYLE, RESUME_STYLE, renderPdf } from "./pdf";
import { dropPdf, storePdf } from "./pdfstore";

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

/* --------------------------------------------------- the replayed run */

/**
 * "Run matching", replayed from the run that produced this snapshot.
 *
 * The exporter runs the real pipeline — validity, then ranking, then pricing
 * the deep read — against its throwaway database copy, and captures the
 * tracker's terminal state. So the numbers this reports are not invented: they
 * are what those passes actually produced over these rows with these
 * preferences.
 *
 * The first version returned the captured status directly. On a machine where
 * no run had happened that day the capture was `stage: "idle"` with every field
 * null, so the button posted, got "nothing is happening" back, and looked
 * broken. Running the pipeline at export time is what fixed it.
 *
 * Clicking walks the stage labels for a few seconds before settling, because
 * the panel renders which pass is running and landing on `done` instantly
 * would skip the part worth seeing. While idle it reports the completed run,
 * which is what the real endpoint does too — a finished run stays readable.
 */
const RUN_MS = 3400;
let runStartedAt: number | null = null;

function pipelineStatus(snapshot: Snapshot): unknown {
  const finished = snapshot.meta.pipeline_status;
  if (!finished) return { stage: "idle", validity: null, ranking: null, estimate: null };
  if (runStartedAt === null) return finished;

  const elapsed = Date.now() - runStartedAt;
  if (elapsed >= RUN_MS) {
    runStartedAt = null;
    return finished;
  }

  // Each pass is revealed only once its stage has passed, so the panel fills in
  // the order the real pipeline fills it rather than all at once.
  const progress = elapsed / RUN_MS;
  const stage = progress < 0.45 ? "validity" : progress < 0.8 ? "ranking" : "estimating";
  return {
    ...finished,
    stage,
    started_at: new Date(runStartedAt).toISOString(),
    finished_at: null,
    validity: stage === "validity" ? null : finished.validity,
    ranking: stage === "estimating" ? finished.ranking : null,
    estimate: null,
  };
}

/* ------------------------------------------------------------ the deep read */

/**
 * The deep read, composed rather than called.
 *
 * `fake/screen.ts` explains what a composed verdict is and is not. This part is
 * only the *pass*: which rows it would have read, in what order, and how long
 * it pretends to take — so the panel's progress line, its finished summary and
 * the rows that light up afterwards all describe the same set.
 *
 * A row's verdict is committed as its turn passes rather than all at the end,
 * so a visitor who closes the panel mid-pass keeps the reads that had already
 * "happened", exactly as a real interrupted pass would.
 */
const READ_MS = 700;

interface DeepRead {
  startedAt: number;
  finishedAt: string | null;
  targets: { jobId: number; title: string; company: string; tokens: number }[];
  committed: number;
  tokens: number;
}

let deepRead: DeepRead | null = null;

function commitReads(snapshot: Snapshot, upTo: number): void {
  if (!deepRead || upTo <= deepRead.committed) return;
  const byId = new Map(snapshot.matches.map((row) => [row.job.id, row]));
  const entries = deepRead.targets.slice(deepRead.committed, upTo).flatMap((target) => {
    const row = byId.get(target.jobId);
    return row ? [{ jobId: target.jobId, verdict: synthesizeVerdict(row), tokens: target.tokens }] : [];
  });
  state.recordVerdicts(entries);
  deepRead.tokens += entries.reduce((total, entry) => total + entry.tokens, 0);
  deepRead.committed = upTo;
}

function llmStatusNow(snapshot: Snapshot): LlmStatus {
  if (!deepRead) return snapshot.meta.llm_status ?? { ...IDLE_LLM };

  const total = deepRead.targets.length;
  const elapsed = Date.now() - deepRead.startedAt;
  const done = Math.min(total, Math.floor(elapsed / READ_MS));
  commitReads(snapshot, done);

  const running = done < total;
  if (!running && !deepRead.finishedAt) deepRead.finishedAt = new Date().toISOString();

  const bands: Record<string, number> = {};
  let blocked = 0;
  for (const target of deepRead.targets.slice(0, deepRead.committed)) {
    const verdict = state.getVerdicts()[String(target.jobId)];
    const band = verdict?.fit_band ?? "moderate";
    bands[band] = (bands[band] ?? 0) + 1;
    if (verdict?.blocked) blocked += 1;
  }

  return {
    state: running ? "running" : "done",
    total,
    done: deepRead.committed,
    cached: 0,
    failed: 0,
    tokens: deepRead.tokens,
    current: running ? `${deepRead.targets[done]?.title} — ${deepRead.targets[done]?.company}` : null,
    started_at: new Date(deepRead.startedAt).toISOString(),
    finished_at: deepRead.finishedAt,
    error: null,
    result: running
      ? null
      : {
          profile_version: snapshot.meta.profile?.version ?? "",
          routed: total,
          cached: 0,
          screened: deepRead.committed,
          failed: 0,
          skipped_budget: 0,
          requests: total,
          tokens: deepRead.tokens,
          bands,
          statuses: {},
          blocked,
          duration_ms: total * READ_MS,
          error: null,
        },
  };
}

const IDLE_LLM: LlmStatus = {
  state: "idle",
  total: 0,
  done: 0,
  cached: 0,
  failed: 0,
  tokens: 0,
  current: null,
  started_at: null,
  finished_at: null,
  error: null,
  result: null,
};

function startDeepRead(snapshot: Snapshot, limit: number): LlmStatus {
  const read = new Set(Object.keys(state.getVerdicts()).map(Number));
  const targets = deepReadTargets(snapshot.matches, limit, read).map((row) => ({
    jobId: row.job.id,
    title: row.job.title,
    company: row.job.company,
    tokens: verdictTokens(row.job.id),
  }));
  deepRead = { startedAt: Date.now(), finishedAt: null, targets, committed: 0, tokens: 0 };
  // Nothing left to read is a finished pass, not a stuck one: with no targets
  // the poll would report `running` with 0 of 0 and never settle.
  if (targets.length === 0) deepRead.finishedAt = new Date().toISOString();
  return llmStatusNow(snapshot);
}

/**
 * Re-price the captured estimate against what this browser has already read.
 *
 * The snapshot's estimate was true when it was taken. Left alone it would keep
 * offering the same 15 unread jobs after a pass had just read them, and the
 * confirm dialog would contradict the summary directly above it.
 */
function estimateNow(snapshot: Snapshot, limit: number): LlmEstimate | null {
  const captured =
    snapshot.meta.llm_estimate[String(limit)] ?? Object.values(snapshot.meta.llm_estimate)[0];
  if (!captured) return null;

  const read = new Set(Object.keys(state.getVerdicts()).map(Number));
  const shortlisted = snapshot.matches.filter((row) => row.shortlisted);
  const cached = shortlisted.filter((row) => row.llm_read || read.has(row.job.id)).length;
  const unread = shortlisted.length - cached;
  const pending = Math.min(limit, unread);
  const perRow = captured.pending > 0 ? captured.est_tokens / captured.pending : 1800;

  return {
    ...captured,
    cap: limit,
    routed: shortlisted.length,
    cached,
    pending,
    over_cap: Math.max(0, unread - pending),
    requests: pending,
    est_tokens: Math.round(pending * perRow),
    est_minutes: Math.max(0.1, Math.round((pending * READ_MS) / 6000) / 10),
    tokens_used: captured.tokens_used + state.llmTokensSpent(),
    fits_in_cap: pending,
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
type Decoratable = { id: number; application_status: string | null; llm_read?: boolean };

function decorateOne<T extends Decoratable>(row: T): T {
  const stage = state.applicationIndex().get(row.id);
  const read = state.hasVerdict(row.id);
  if (!stage && !read) return row;
  return {
    ...row,
    ...(stage ? { application_status: stage } : {}),
    // The row's own "LLM read" marker, so the check badges agree with Matches.
    ...(read ? { llm_read: true } : {}),
  };
}

function decorate<T extends Decoratable>(rows: T[]): T[] {
  const index = state.applicationIndex();
  const verdicts = state.getVerdicts();
  if (index.size === 0 && Object.keys(verdicts).length === 0) return rows;
  return rows.map((row) =>
    index.has(row.id) || verdicts[String(row.id)] ? decorateOne(row) : row,
  );
}

/** Match rows carrying whatever this browser's deep read produced for them. */
function withVerdicts(rows: MatchRow[]): MatchRow[] {
  const verdicts = state.getVerdicts();
  if (Object.keys(verdicts).length === 0) return rows;
  return rows.map((row) => {
    const verdict = verdicts[String(row.job.id)];
    if (!verdict || row.llm_read) return row;
    return { ...row, llm_used: true, llm_read: true, llm_verdict: verdict };
  });
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
  const result = buildMatchList(withVerdicts(snapshot.matches), query, {
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
  const stored = snapshot.meta.dashboard;
  if (!stored) return refuse("No dashboard payload in this snapshot", 404);

  // The budget band has to move with the reads this browser ran, or the panel
  // would report yesterday's spend beside a deep read that just happened.
  const captured: Dashboard = {
    ...stored,
    tokens_today: stored.tokens_today + state.llmTokensSpent(),
  };

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
  const found = snapshot.documents.byKey[`${jobId}:${kind}`] ?? state.getDocument(jobId, kind);
  if (!found) return refuse("No document stored for this job", 404);
  return json(state.withEdits(found));
}

/** A stored document, or one this browser generated. */
function anyDocument(snapshot: Snapshot, id: number): TailoredDocument | null {
  return snapshot.documents.byId[String(id)] ?? state.documentById(id);
}

/* --------------------------------------------------- generated documents */

/** The contact line, read off the base résumé so the letter cannot drift from it. */
function contactLines(snapshot: Snapshot): string[] {
  const tex = snapshot.meta.base_resume?.tex ?? "";
  const start = tex.indexOf("\\address{");
  if (start === -1) return [];
  const end = tex.indexOf("\n}", start);
  return tex
    .slice(start + "\\address{".length, end === -1 ? undefined : end)
    .split(/\\\\/)
    .map((line) => line.trim())
    .filter(Boolean);
}

/**
 * Lay a generated document out and put the PDF where the preview can find it.
 *
 * The iframe is a navigation, so `pdfstore` hands the bytes to a service
 * worker; see `public/demo-sw.js`. If that worker cannot run, the caller says
 * so rather than showing an empty pane.
 */
async function publishPdf(document: TailoredDocument, tex: string): Promise<boolean> {
  const style = document.kind === "cover_letter" ? LETTER_STYLE : RESUME_STYLE;
  return storePdf(document.id, renderPdf(texToBlocks(tex), style));
}

/**
 * Generate a résumé or a cover letter for one posting.
 *
 * `fake/documents.ts` holds the part worth reading: the résumé is the base
 * template re-ordered against this posting's keywords, and the letter is
 * assembled from sentences the profile already states. Nothing is written by a
 * model, and `llm_used` stays false so no surface claims otherwise.
 */
async function generateDocument(
  snapshot: Snapshot,
  jobId: number,
  kind: DocumentKind,
): Promise<Response> {
  const existing = state.getDocument(jobId, kind);
  if (existing) return json(state.withEdits(existing));

  const row = matchRowFor(snapshot, jobId);
  if (!row) return refuse("Job not found", 404);

  const base = snapshot.meta.base_resume;
  if (kind === "resume" && !base?.tex) {
    return refuse("No base résumé in this snapshot", 404);
  }

  const jd = await loadDescriptions();
  const keywords = keywordsFor(row, jd[String(jobId)]?.description_text ?? null);
  const profileVersion = snapshot.meta.profile?.version ?? "";
  const id = state.nextDocumentId();

  const built =
    kind === "resume"
      ? tailorResume(base!.tex, keywords)
      : coverLetter(row, keywords, contactLines(snapshot));

  const document = asDocument(
    id,
    row,
    kind,
    profileVersion,
    built.tex,
    built.notes,
    keywords,
    "dropped" in built ? built.dropped : [],
  );

  const gaps = Object.keys(snapshot.meta.profile?.gaps ?? {});
  state.saveDocument(
    document,
    "diff" in built ? asDiff(id, built.diff) : null,
    emptyChat(id, suggestionsFor(document, gaps)),
  );
  await publishPdf(document, document.tex);
  return json(document);
}

/** The scored row for a posting, or a bare stand-in when it was never scored. */
function matchRowFor(snapshot: Snapshot, jobId: number): MatchRow | null {
  const scored = snapshot.matches.find((row) => row.job.id === jobId);
  if (scored) return scored;
  const job = snapshot.jobs.find((row) => row.id === jobId);
  if (!job) return null;
  // Everything downstream reads only these fields, and an unscored posting
  // genuinely has nothing to say for the rest.
  return {
    job,
    score: 0,
    band: "moderate",
    subscores: {},
    match_reasons: [],
    confident: false,
    matched_skills: [],
    missing_stacks: [],
    years_required: null,
    shortlisted: false,
    shortlist_reasons: [],
    blockers: [],
    llm_used: false,
    llm_read: false,
    llm_verdict: null,
    profile_version: snapshot.meta.profile?.version ?? "",
    prefs_pass: false,
    prefs_reasons: [],
    prefs_version: "",
    usd_pay: null,
    usd_pay_source: null,
    scored_at: new Date().toISOString(),
  };
}

/* ------------------------------------------------------------------- chat */

function chatFor(snapshot: Snapshot, document: TailoredDocument): ReturnType<typeof emptyChat> {
  const stored = state.getChat(document.id);
  if (stored) return stored;
  const captured = snapshot.documents.chats[String(document.id)];
  if (captured) return captured;
  return emptyChat(document.id, suggestionsFor(document, Object.keys(snapshot.meta.profile?.gaps ?? {})));
}

/** Append a turn and its reply. The reply never edits the document. */
function sendChat(snapshot: Snapshot, document: TailoredDocument, text: string): Response {
  const current = state.withEdits(document);
  const chat = chatFor(snapshot, document);
  const answer = chatReply({
    document: current,
    message: text,
    gaps: Object.keys(snapshot.meta.profile?.gaps ?? {}),
  });
  const messages: ChatMessage[] = [...chat.messages, userMessage(text), answer];
  return json(
    state.setChat(document.id, {
      ...chat,
      messages,
      tokens: chat.tokens + answer.tokens,
    }),
  );
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
        if (second === "llm" && third === "status") return json(llmStatusNow(snapshot));
        if (second === "llm" && third === "estimate") {
          const estimate = estimateNow(snapshot, Number(params.get("limit")) || 15);
          return estimate ? json(estimate) : refuse("No estimate in this snapshot", 404);
        }
        if (second === "run" && third === "status") return json(pipelineStatus(snapshot));
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
          const diff = snapshot.documents.diffs[String(docId)] ?? state.getDiff(docId);
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
          const document = anyDocument(snapshot, docId);
          return document
            ? json(chatFor(snapshot, document))
            : json({ document_id: docId, messages: [], suggestions: [], tokens: 0 });
        }
        if (third === "tex") {
          const document = anyDocument(snapshot, docId);
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
          // Likewise: the validity half of the captured run, in its own shape.
          return json(
            meta.pipeline_status?.validity ?? {
              considered: snapshot.jobs.length,
              scored: snapshot.jobs.filter((job) => job.validity_score !== null).length,
              unchanged: 0,
              suspect: 0,
              duplicates: 0,
            },
          );
        }
        break;
      case "matches":
        if (second === "score") {
          // The ranking half of the captured run, in its own `MatchRunOut`
          // shape. The earlier version spread the whole pipeline status into
          // this response, which is a different schema entirely.
          return json(
            meta.pipeline_status?.ranking ?? {
              profile_version: meta.profile?.version ?? "",
              considered: snapshot.matches.length,
              scored: 0,
              updated: 0,
              skipped: snapshot.matches.length,
              shortlisted: 0,
              low_confidence: 0,
            },
          );
        }
        if (second === "run") {
          runStartedAt = Date.now();
          return json(pipelineStatus(snapshot));
        }
        if (second === "llm") {
          return json(startDeepRead(snapshot, Number(params.get("limit")) || 15));
        }
        if (second && third === "llm") {
          // The drawer's one-row read. Instant rather than paced: it is a
          // single call, and the panel that shows progress is not on screen.
          const jobId = Number(second);
          const row = snapshot.matches.find((match) => match.job.id === jobId);
          if (!row) return refuse("Job not found", 404);
          if (!state.hasVerdict(jobId) && !row.llm_read) {
            state.recordVerdicts([
              { jobId, verdict: synthesizeVerdict(row), tokens: verdictTokens(jobId) },
            ]);
          }
          return json({
            ...IDLE_LLM,
            state: "done",
            total: 1,
            done: 1,
            tokens: verdictTokens(jobId),
            finished_at: new Date().toISOString(),
          });
        }
        if (second && third === "document") {
          const kind = (params.get("kind") ?? "resume") as DocumentKind;
          const existing = snapshot.documents.byKey[`${second}:${kind}`];
          if (existing) return json(state.withEdits(existing));
          return generateDocument(snapshot, Number(second), kind);
        }
        break;
      case "documents": {
        const docId = Number(second);
        const document = anyDocument(snapshot, docId);
        if (!document) return refuse("Document not found", 404);
        if (third === "revert") {
          // Genuinely free, and genuinely correct: reverting replays the stored
          // tailoring, which is exactly what the snapshot holds. Dropping the
          // rendered PDF with it is what lets a snapshot document go back to
          // its exported Tectonic file rather than the browser's rendering.
          state.clearTex(docId);
          await dropPdf(docId);
          if (state.getDocument(document.job_id, document.kind)) {
            await publishPdf(document, document.tex);
          }
          return json(document);
        }
        if (third === "compile") {
          // An unedited snapshot document needs no work: the file in
          // `/demo/pdf/` IS the compile of this LaTeX, done by the exporter
          // with the real Tectonic.
          const edited = state.getTex(docId);
          const generated = state.documentById(docId) !== null;
          if (!edited && !generated) {
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

          // Anything else is laid out here. `src/demo/pdf.ts` is not LaTeX and
          // does not pretend to be — it renders the document's structure, so a
          // hand-edited page looks a little different from a Tectonic one.
          const tex = edited ?? document.tex;
          const ok = await publishPdf(document, tex);
          return json({
            ok,
            errors: ok
              ? []
              : [
                  "The demo lays generated documents out in the browser, and this one needs a service worker to serve the result.",
                  "That worker could not start here — a private window or blocked site data will do it.",
                ],
            log: ok ? "Rendered in the browser by src/demo/pdf.ts — not a LaTeX compile." : "",
            duration_s: 0,
            pdf_hash: texHash(tex),
            fact_warnings: [],
          });
        }
        if (third === "chat" && !fourth) {
          const text = ((body ?? {}) as { message?: string }).message ?? "";
          if (!text.trim()) return refuse("No message in the request", 422);
          return sendChat(snapshot, document, text);
        }
        if (third === "chat" && fourth) {
          const chat = chatFor(snapshot, document);
          const target = chat.messages.find((message) => message.id === fourth);
          if (!target?.proposal) return refuse("No such proposal", 404);

          const action = segments[4];
          if (action === "dismiss") {
            return json(
              state.setChat(docId, {
                ...chat,
                messages: chat.messages.map((message) =>
                  message.id === fourth
                    ? { ...message, proposal: { ...target.proposal!, status: "dismissed" } }
                    : message,
                ),
              }),
            );
          }

          // Applying: blocked changes can never be accepted, whatever was
          // ticked — that is the fact check, and it is the point of the panel.
          const asked = ((body ?? {}) as { accept?: string[] | null }).accept;
          const allowed = target.proposal.changes
            .filter((change) => change.status !== "blocked")
            .map((change) => change.region_id);
          const accepted = new Set(asked ? asked.filter((id) => allowed.includes(id)) : allowed);
          const wantsOrder = !asked || asked.includes("order");

          const current = state.withEdits(document);
          const next = applyProposal(
            current,
            { ...target.proposal, order: wantsOrder ? target.proposal.order : [] },
            accepted,
          );
          state.setTex(docId, next);
          await publishPdf(document, next);

          const updated = state.setChat(docId, {
            ...chat,
            messages: chat.messages.map((message) =>
              message.id === fourth
                ? {
                    ...message,
                    proposal: {
                      ...target.proposal!,
                      status: "applied",
                      applied: [...accepted, ...(wantsOrder && target.proposal!.order.length ? ["order"] : [])],
                    },
                  }
                : message,
            ),
          });
          return json({ document: state.withEdits(document), chat: updated });
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
        const document = anyDocument(snapshot, Number(second));
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
      const docId = Number(second);
      const document = anyDocument(snapshot, docId);
      const suggestions = document
        ? chatFor(snapshot, document).suggestions
        : [];
      return json(state.setChat(docId, { document_id: docId, messages: [], suggestions, tokens: 0 }));
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
