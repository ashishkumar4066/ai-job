/**
 * Loads the static snapshot the demo build answers from.
 *
 * The snapshot is written by `backend/scripts/export_demo.py`, which captures
 * what the real API answered rather than inventing payloads — so everything
 * here is already in the exact shape `lib/types.ts` declares.
 *
 * The files are fetched, not imported. Importing 12MB of JSON would inline it
 * into a JS chunk that the browser has to parse as source; fetching it means
 * Vercel serves it gzipped as a static asset and the browser uses its native
 * JSON parser. It also keeps the snapshot out of the app bundle entirely, so a
 * non-demo build carries none of it.
 */

import type {
  ApplicationList,
  Dashboard,
  Facets,
  Job,
  JobDetail,
  LlmEstimate,
  LlmStatus,
  MatchList,
  PipelineStatus,
  Prefs,
  ProfileDocument,
  ProfileSummary,
  BaseResume,
  Health,
  DocumentDiff,
  ResumeChat,
  TailoredDocument,
  IngestStatus,
  MatchesFunnel,
} from "@/lib/types";

/** `window.fetch` as it was before the shim wrapped it.
 *
 *  Captured at module load, which is before `install()` runs. Without this the
 *  snapshot request would be intercepted by the shim that is trying to load the
 *  snapshot. */
export const realFetch: typeof fetch = window.fetch.bind(window);

export type MatchRow = MatchList["items"][number];

export interface JdEntry {
  description_html: string | null;
  description_text: string | null;
  llm_validity: JobDetail["llm_validity"];
  raw_json: null;
}

export interface Manifest {
  generated_at: string;
  counts: {
    jobs: number;
    eligible: number;
    descriptions: number;
    matches: number;
    documents: number;
  };
  excluded_ats: string[];
  pdfs: Record<string, string>;
}

export interface MetaPayloads {
  facets: { eligible: Facets; all: Facets };
  matches_funnel: MatchesFunnel | null;
  dashboard: Dashboard | null;
  prefs: Prefs | null;
  profile: ProfileSummary | null;
  profile_document: ProfileDocument | null;
  base_resume: BaseResume | null;
  health: Health | null;
  llm_status: LlmStatus | null;
  pipeline_status: PipelineStatus | null;
  ingest_status: IngestStatus | null;
  companies: unknown;
  /** Seeds the demo tracker so the Dashboard opens populated. */
  applications: ApplicationList | null;
  llm_estimate: Record<string, LlmEstimate>;
}

export interface DocumentStore {
  /** Keyed `"{jobId}:{kind}"` — how the UI asks for one. */
  byKey: Record<string, TailoredDocument>;
  byId: Record<string, TailoredDocument>;
  diffs: Record<string, DocumentDiff>;
  chats: Record<string, ResumeChat>;
}

export interface Snapshot {
  manifest: Manifest;
  jobs: Job[];
  matches: MatchRow[];
  meta: MetaPayloads;
  documents: DocumentStore;
  /** Job id -> full JD. Lazily loaded; see `loadDescriptions`. */
  jd: Record<string, JdEntry> | null;
}

const BASE = `${import.meta.env.BASE_URL ?? "/"}demo/`.replace(/\/{2,}/g, "/");

async function loadJson<T>(name: string): Promise<T> {
  const response = await realFetch(`${BASE}${name}`, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`demo snapshot: ${name} -> HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

let snapshot: Snapshot | null = null;
let loading: Promise<Snapshot> | null = null;

/** Load (once) the parts every surface needs. Concurrent callers share one load. */
export function loadSnapshot(): Promise<Snapshot> {
  if (snapshot) return Promise.resolve(snapshot);
  loading ??= (async () => {
    const [manifest, jobs, matches, meta, documents] = await Promise.all([
      loadJson<Manifest>("manifest.json"),
      loadJson<Job[]>("jobs.json"),
      loadJson<MatchRow[]>("matches.json"),
      loadJson<MetaPayloads>("meta.json"),
      loadJson<DocumentStore>("documents.json"),
    ]);
    snapshot = { manifest, jobs, matches, meta, documents, jd: null };
    return snapshot;
  })();
  return loading;
}

let jdLoading: Promise<Record<string, JdEntry>> | null = null;

/**
 * Load the job descriptions, on first need rather than at boot.
 *
 * They are ~4MB — a third of the snapshot — and nothing shows one until a
 * drawer opens or a full-text search runs. Loading them up front would add a
 * second of dead time to the first paint for a payload most visitors never see.
 */
export async function loadDescriptions(): Promise<Record<string, JdEntry>> {
  const loaded = await loadSnapshot();
  if (loaded.jd) return loaded.jd;
  jdLoading ??= loadJson<Record<string, JdEntry>>("jd.json").then((jd) => {
    loaded.jd = jd;
    return jd;
  });
  return jdLoading;
}

/** The snapshot if it is already in memory, else null. Never triggers a load. */
export function peekSnapshot(): Snapshot | null {
  return snapshot;
}
