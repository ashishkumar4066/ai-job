import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpRight,
  BrainCircuit,
  Briefcase,
  Loader2,
  CalendarDays,
  FileText,
  Check,
  ShieldCheck,
  ChevronDown,
  ChevronUp,
  Clock,
  Globe2,
  Layers,
  Link2,
  MapPin,
  Server,
  Wallet,
  X,
} from "lucide-react";
import { api, llmStatus, startLlmOne } from "@/lib/api";
import { LlmBadge, VerifierBadge, fitBand } from "./CheckBadges";
import { absoluteDate, formatSalary, relativeTime, sourceLabel, titleCase } from "@/lib/format";
import { JOB_DETAIL_STALE_MS } from "@/lib/hooks";
import { sanitizeHtml } from "@/lib/sanitize";
import type { Job, LlmValidity, Match } from "@/lib/types";
import { validityBand } from "@/lib/types";
import { Badge, CompanyAvatar, cx } from "./primitives";

export function JobDrawer({
  jobId,
  summary,
  match,
  onClose,
  onTailor,
  onPrev,
  onNext,
  hasPrev,
  hasNext,
}: {
  jobId: number;
  summary: Job | undefined;
  /** Present when opened from Matches: carries the ranker and LLM fit verdicts. */
  match?: Match;
  onClose: () => void;
  /** Only from Matches — the Jobs tile has no profile-scored row to tailor against. */
  onTailor?: () => void;
  onPrev: () => void;
  onNext: () => void;
  hasPrev: boolean;
  hasNext: boolean;
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId),
    staleTime: JOB_DETAIL_STALE_MS,
  });

  const [copied, setCopied] = useState(false);

  // Escape closes the drawer, matching the browser-back behaviour.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  // Reset the copy confirmation when you navigate to another job.
  useEffect(() => setCopied(false), [jobId]);

  const job = data ?? summary;

  // A full DOM parse of the JD — once per job, not on every drawer render
  // (the copy toast and the deep-read poll both re-render it).
  const descriptionHtml = data?.description_html;
  const safeHtml = useMemo(
    () => (descriptionHtml ? sanitizeHtml(descriptionHtml) : ""),
    [descriptionHtml],
  );

  return (
    <>
      <div
        onClick={onClose}
        className="animate-fade-in fixed inset-0 z-40 bg-black/55 backdrop-blur-[4px]"
        aria-hidden
      />

      <aside
        role="dialog"
        aria-modal="true"
        aria-label={job?.title ?? "Job details"}
        className="glass-strong animate-slide-in fixed top-0 right-0 z-50 flex h-full w-full max-w-[660px] flex-col rounded-l-3xl border-l border-edge-strong"
      >
        {/* Violet bloom at the top of the panel — the drawer's own light source. */}
        <span
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-40 rounded-tl-3xl bg-[radial-gradient(80%_100%_at_50%_0%,var(--accent-soft),transparent_75%)]"
        />

        {/* Header */}
        <div className="relative flex items-start gap-3 border-b border-edge px-5 py-4">
          {job && <CompanyAvatar name={job.company} size={44} />}
          <div className="min-w-0 flex-1">
            {job ? (
              <>
                <h2 className="text-[18px] leading-snug font-semibold tracking-[-0.02em] text-ink">
                  {job.title}
                </h2>
                <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted">
                  <span className="font-medium text-ink">{job.company}</span>
                  <span className="opacity-40">·</span>
                  <span>{sourceLabel(job.ats)}</span>
                  {job.status === "closed" && <Badge tone="danger">Closed</Badge>}
                  {job.remote && <Badge tone="accent">Remote</Badge>}
                </p>
              </>
            ) : (
              <div className="space-y-2">
                <div className="skeleton h-4 w-2/3 rounded-full" />
                <div className="skeleton h-3 w-1/3 rounded-full" />
              </div>
            )}
          </div>

          <div className="flex shrink-0 items-center gap-1">
            <button
              onClick={onPrev}
              disabled={!hasPrev}
              aria-label="Previous job (k)"
              title="Previous job — k"
              className="grid size-8 place-items-center rounded-lg border border-edge bg-panel text-muted transition-all hover:bg-panel-hover hover:text-ink disabled:opacity-25 disabled:hover:bg-panel"
            >
              <ChevronUp size={15} />
            </button>
            <button
              onClick={onNext}
              disabled={!hasNext}
              aria-label="Next job (j)"
              title="Next job — j"
              className="grid size-8 place-items-center rounded-lg border border-edge bg-panel text-muted transition-all hover:bg-panel-hover hover:text-ink disabled:opacity-25 disabled:hover:bg-panel"
            >
              <ChevronDown size={15} />
            </button>
            <button
              onClick={onClose}
              aria-label="Close (Esc)"
              className="ml-1 grid size-8 place-items-center rounded-lg border border-edge bg-panel text-muted transition-all hover:border-danger/40 hover:bg-danger/10 hover:text-danger"
            >
              <X size={15} />
            </button>
          </div>
        </div>

        {/* One scroll region for everything between header and footer. Only
            the JD used to scroll, so a tall fit/validity section squeezed it
            to zero height and the rest of the drawer was unreachable. */}
        <div className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {/* Meta grid */}
          {job && (
            <div className="relative grid grid-cols-2 gap-x-4 gap-y-3.5 border-b border-edge px-5 py-4 sm:grid-cols-3">
              <Meta
                icon={job.remote ? <Globe2 size={13} /> : <MapPin size={13} />}
                label="Location"
                value={
                  job.locations.length ? job.locations.join(" · ") : job.remote ? "Remote" : "—"
                }
              />
              <Meta icon={<Layers size={13} />} label="Department" value={job.department ?? "—"} />
              {/* The hint carries the filter's own pay reasoning — including when
                it read a bare number as an hourly or monthly rate — so a
                surprising verdict can be checked rather than just trusted. */}
              <Meta
                icon={<Wallet size={13} />}
                label="Salary"
                value={
                  formatSalary(job.salary_min, job.salary_max, job.salary_currency) ?? "Not stated"
                }
                hint={job.eligibility_reasons
                  .find((reason) => reason.startsWith("pay_"))
                  ?.split(":")
                  .slice(1)
                  .join(":")}
              />
              <Meta
                icon={<CalendarDays size={13} />}
                label="Posted"
                value={job.posted_at ? absoluteDate(job.posted_at) : "Not stated"}
                hint={job.posted_at ? relativeTime(job.posted_at) : undefined}
              />
              <Meta
                icon={<Clock size={13} />}
                label="First seen"
                value={relativeTime(job.first_seen_at)}
                hint={absoluteDate(job.first_seen_at)}
              />
              <Meta
                icon={<Clock size={13} />}
                label="Last seen"
                value={relativeTime(job.last_seen_at)}
                hint="Confirmed live on the board"
              />
              {/* "Not stated" rather than a guess: Greenhouse publishes
                neither of these for any of its 2,224 rows, and a blank is
                honest where "On-site" would be an invention. */}
              <Meta
                icon={<Briefcase size={13} />}
                label="Commitment"
                value={
                  job.employment_type
                    ? titleCase(job.employment_type.replace(/_/g, " "))
                    : "Not stated"
                }
              />
              <Meta
                icon={<Globe2 size={13} />}
                label="Work mode"
                value={job.workplace_type ? titleCase(job.workplace_type) : "Not stated"}
              />
              <Meta icon={<Server size={13} />} label="Source" value={job.source_key} mono />
            </div>
          )}

          {job && <ChecksBar job={job} />}

          {match && <FitSection match={match} />}

          {/* Validity — the deterministic verdict, plus the deep read when one
            exists. Shown ABOVE the JD because it is the thing that decides
            whether the JD is worth reading at all. */}
          {job && job.validity_score != null && (
            <ValiditySection job={job} llm={data?.llm_validity ?? null} />
          )}

          {/* Description */}
          <div className="px-5 py-5">
            {isLoading && !summary ? (
              <div className="space-y-3">
                {Array.from({ length: 9 }, (_, index) => (
                  <div
                    key={index}
                    className="skeleton h-3 rounded-full"
                    style={{ width: `${65 + ((index * 37) % 35)}%` }}
                  />
                ))}
              </div>
            ) : isError ? (
              <p className="rounded-xl border border-danger/30 bg-danger/10 p-4 text-[13px] text-danger">
                Couldn't load this description: {(error as Error).message}
              </p>
            ) : safeHtml ? (
              // Sanitized before it reaches the DOM. The board carries
              // aggregator feeds (Himalayas, Remotive, Wellfound, Jobicy, The Muse, YC) whose HTML is
              // written by whoever posted the job rather than by a vetted ATS,
              // so this is third-party markup executing on the same origin as
              // the API. `sanitizeHtml` is allowlist-only — see its module docs.
              <div className="jd" dangerouslySetInnerHTML={{ __html: safeHtml }} />
            ) : data?.description_text ? (
              <p className="jd whitespace-pre-wrap">{data.description_text}</p>
            ) : (
              <p className="text-[13px] text-subtle">
                This posting has no description on the board.
              </p>
            )}
          </div>
        </div>

        {/* Footer */}
        {job && (
          <div className="flex items-center gap-2.5 border-t border-edge bg-panel-strong/30 px-5 py-4">
            <a
              href={job.apply_url}
              target="_blank"
              rel="noopener noreferrer"
              className="btn-primary flex flex-1 items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-[14px] font-semibold"
            >
              Apply on {sourceLabel(job.ats)}
              <ArrowUpRight size={15} />
            </a>
            {onTailor && (
              <button
                onClick={onTailor}
                title="Rewrite your resume to lead with what this posting asks for"
                className="flex items-center gap-1.5 rounded-xl border border-edge bg-panel px-3.5 py-2.5 text-[13px] font-medium text-muted transition-all duration-200 hover:border-accent/45 hover:bg-accent-soft hover:text-accent-text"
              >
                <FileText size={14} />
                Tailor resume
              </button>
            )}
            <button
              onClick={() => {
                navigator.clipboard?.writeText(job.apply_url);
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1800);
              }}
              className={cx(
                "flex items-center gap-1.5 rounded-xl border px-3.5 py-2.5 text-[13px] font-medium transition-all duration-200",
                copied
                  ? "border-mint/40 bg-mint/12 text-mint"
                  : "border-edge bg-panel text-muted hover:bg-panel-hover hover:text-ink",
              )}
              title="Copy the application link"
            >
              {copied ? <Check size={14} /> : <Link2 size={14} />}
              {copied ? "Copied" : "Copy link"}
            </button>
          </div>
        )}
      </aside>
    </>
  );
}

/**
 * Verifier and LLM state, plus the one way to spend a call on a job the
 * shortlist did not pick — typically a low-confidence JD written in prose.
 * One click, one call, never a batch.
 */
function ChecksBar({ job }: { job: Job }) {
  const qc = useQueryClient();
  const [polling, setPolling] = useState(false);

  const status = useQuery({
    queryKey: ["llm"],
    queryFn: llmStatus,
    refetchInterval: polling ? 2000 : false,
    enabled: polling,
  });

  const read = useMutation({
    mutationFn: () => startLlmOne(job.id),
    // Seed the poll with this run's own "running" status, so a "done" left
    // over from an earlier pass cannot end the wait before it starts.
    onSuccess: (started) => {
      qc.setQueryData(["llm"], started);
      setPolling(true);
    },
  });

  useEffect(() => {
    if (polling && status.data?.state === "done") {
      setPolling(false);
      for (const key of ["job", "jobs", "matches", "matches-funnel", "jobs-funnel"]) {
        qc.invalidateQueries({ queryKey: [key] });
      }
    }
  }, [polling, status.data?.state, qc]);

  const busy = read.isPending || polling;
  const canRead = job.status === "open" && job.eligibility_pass && !job.llm_read;

  return (
    <div className="relative flex flex-wrap items-center gap-2 border-b border-edge px-5 py-2.5">
      <span className="text-[10.5px] font-semibold tracking-[0.08em] text-subtle uppercase">
        Checks
      </span>
      <VerifierBadge
        score={job.validity_score}
        reasons={job.validity_reasons}
        checkedAt={job.validity_checked_at}
      />
      <LlmBadge read={job.llm_read} />
      {canRead && (
        <button
          type="button"
          disabled={busy}
          onClick={() => read.mutate()}
          title="Send this one job to the LLM — a single call"
          className="ml-auto flex items-center gap-1.5 rounded-lg border border-accent/35 bg-accent/10 px-2.5 py-1 text-[12px] font-medium text-accent-text transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <BrainCircuit size={12} />}
          {busy ? "Reading…" : "Deep read this job"}
        </button>
      )}
      {(read.error || status.data?.error) && (
        <span className="w-full text-[11.5px] text-danger">
          {(read.error as Error | null)?.message ?? status.data?.error}
        </span>
      )}
    </div>
  );
}

function Meta({
  icon,
  label,
  value,
  hint,
  mono,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <p className="flex items-center gap-1.5 text-[10.5px] font-semibold tracking-[0.08em] text-subtle uppercase">
        <span className="text-accent-text opacity-80">{icon}</span>
        {label}
      </p>
      <p
        className={cx("mt-1 truncate text-[13px] text-ink", mono && "font-mono text-[11px]")}
        title={value}
      >
        {value}
      </p>
      {hint && <p className="truncate text-[11px] text-subtle">{hint}</p>}
    </div>
  );
}

/* ----------------------------------------------------------------------- fit */

/**
 * Both fit verdicts side by side, each named for its source, so a Strong from
 * the keyword ranker and a Moderate from the LLM read as two opinions rather
 * than one contradictory number.
 */
function FitSection({ match }: { match: Match }) {
  const ranker = fitBand(match.band);
  const verdict = match.llm_read ? match.llm_verdict : null;
  const llm = fitBand(verdict?.fit_band);
  // Older verdicts carry one undivided `gaps` list; show it as must-have.
  const mustHaveGaps = verdict?.must_have_gaps ?? verdict?.gaps ?? [];
  const niceGaps = verdict?.nice_to_have_gaps ?? [];

  return (
    <div className="border-b border-edge px-5 py-4">
      <div className="mb-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[12.5px]">
        <span
          className="flex items-center gap-1.5"
          title="Free keyword ranker (matching.py): skill + years + role family + India + freshness − penalties. No LLM."
        >
          <span className="text-subtle">Ranker fit</span>
          <span className={cx("font-semibold", ranker?.tone)}>
            {match.score}
            <span className="ml-1 text-[11px] font-medium">{ranker?.label}</span>
          </span>
        </span>
        <span className="flex items-center gap-1.5">
          <BrainCircuit size={13} className="text-accent-text" />
          <span className="text-subtle">LLM fit</span>
          {llm ? (
            <span className={cx("font-semibold", llm.tone)}>{llm.label}</span>
          ) : (
            <span className="text-subtle">not read yet</span>
          )}
        </span>
        {verdict?.model && (
          <span className="ml-auto text-[10.5px] text-subtle">{verdict.model}</span>
        )}
      </div>

      {verdict && (
        <div className="space-y-2 text-[11.5px]">
          {verdict.fit_reasons && verdict.fit_reasons.length > 0 && (
            <ul className="ml-4 list-disc space-y-0.5 text-muted">
              {verdict.fit_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
          {((verdict.strengths?.length ?? 0) > 0 ||
            mustHaveGaps.length > 0 ||
            niceGaps.length > 0) && (
            <div className="flex flex-wrap gap-1">
              {verdict.strengths?.map((s) => (
                <span
                  key={`s-${s}`}
                  className="rounded-md border border-success/25 bg-success/10 px-1.5 py-0.5 text-[10.5px] text-success"
                >
                  {s}
                </span>
              ))}
              {mustHaveGaps.map((g) => (
                <span
                  key={`g-${g}`}
                  title="Required by the posting, not proven by the profile"
                  className="flex items-center gap-0.5 rounded-md border border-danger/25 bg-danger/8 px-1.5 py-0.5 text-[10.5px] text-danger"
                >
                  <X size={8} />
                  {g}
                </span>
              ))}
              {niceGaps.map((g) => (
                <span
                  key={`n-${g}`}
                  title="Nice-to-have, not proven by the profile"
                  className="flex items-center gap-0.5 rounded-md border border-edge-strong px-1.5 py-0.5 text-[10.5px] text-subtle"
                >
                  <X size={8} />
                  {g}
                </span>
              ))}
            </div>
          )}
          {verdict.blocked && (
            <p className="flex items-start gap-1.5 text-danger">
              <AlertTriangle size={12} className="mt-px shrink-0" />
              The JD states a hard blocker: sponsorship required, excludes India, or on-site.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ validity */

const VALIDITY_TONE: Record<string, { label: string; text: string; bg: string }> = {
  solid: {
    label: "Solid",
    text: "text-success",
    bg: "bg-success/12 border-success/25",
  },
  ok: {
    label: "OK",
    text: "text-accent-text",
    bg: "bg-accent/10 border-accent/25",
  },
  questionable: {
    label: "Questionable",
    text: "text-highlight",
    bg: "bg-highlight/12 border-highlight/25",
  },
  suspect: {
    label: "Suspect",
    text: "text-danger",
    bg: "bg-danger/10 border-danger/25",
  },
};

/** Human wording for the reason codes `validation.py` emits.
 *
 *  Each stored reason keeps its evidence after a colon and its penalty after
 *  `:-` — `stale:67d:-20` — because the whole point of this layer is that its
 *  verdicts can be argued with. The raw string is kept in the tooltip. */
const VALIDITY_REASONS: Record<string, string> = {
  fresh: "Posted recently",
  aging: "Getting old",
  stale: "Stale",
  very_stale: "Very stale",
  no_posted_date: "No posting date",
  seen_in_last_sweep: "Confirmed in the latest fetch",
  missed_last_sweep: "Missing from the latest fetch",
  closed: "Closed",
  unchanged_since_first_seen: "Unchanged since we first saw it",
  evergreen_language: "Reads as a perpetual talent-pool post",
  contentless_description: "Description says nothing concrete",
  no_description: "No description",
  thin_description: "Very short description",
  no_apply_url: "No apply link",
  duplicate_same_company: "Duplicate of another posting at this company",
  duplicate_cross_company: "Same description appears under another company",
  "llm:ghost": "Deep read: likely a ghost posting",
  "llm:evergreen": "Deep read: evergreen posting",
  "llm:evergreen_confirmed": "Deep read confirms: evergreen",
  "llm:active": "Deep read: a real, active opening",
  "llm:inconsistent": "Deep read found internal contradictions",
};

function describeValidityReason(raw: string): {
  label: string;
  penalty: number | null;
} {
  const [body, penaltyPart] = raw.split(":-");
  const parts = (body ?? "").split(":");
  // `llm:` reasons are two segments before any evidence.
  const key = parts[0] === "llm" ? `llm:${parts[1] ?? ""}` : (parts[0] ?? "");
  const evidence = parts[0] === "llm" ? parts[2] : parts[1];
  const label = VALIDITY_REASONS[key] ?? key.replace(/_/g, " ");
  return {
    label: evidence ? `${label} (${evidence})` : label,
    penalty: penaltyPart ? Number.parseInt(penaltyPart, 10) : null,
  };
}

function ValiditySection({ job, llm }: { job: Job; llm: LlmValidity | null }) {
  const score = job.validity_score ?? 100;
  const tone = VALIDITY_TONE[validityBand(score)] ?? VALIDITY_TONE.ok!;
  const reasons = (job.validity_reasons ?? []).map((r) => ({
    raw: r,
    ...describeValidityReason(r),
  }));
  // Penalties first — they are what the score is explaining.
  reasons.sort((a, b) => (b.penalty ?? 0) - (a.penalty ?? 0));

  return (
    <div className="border-b border-edge px-5 py-4">
      <div className="mb-2.5 flex items-center gap-2">
        <span className={cx("grid size-6 place-items-center rounded-md border", tone.bg)}>
          <ShieldCheck size={13} className={tone.text} />
        </span>
        <div className="text-[12.5px] font-semibold text-ink">Validity</div>
        <span className={cx("text-[12.5px] font-semibold", tone.text)}>
          {score}
          <span className="ml-1 text-[11px] font-medium">{tone.label}</span>
        </span>
        {llm && (
          <span className="ml-auto text-[10.5px] text-subtle">
            deep read · {llm.model ?? "llm"}
          </span>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5">
        {reasons.map((r) => (
          <span
            key={r.raw}
            title={r.raw}
            className={cx(
              "rounded-md border px-1.5 py-0.5 text-[10.5px]",
              r.penalty
                ? "border-danger/25 bg-danger/8 text-danger"
                : "border-edge bg-panel text-muted",
            )}
          >
            {r.label}
            {r.penalty ? <span className="ml-1 font-mono">−{r.penalty}</span> : null}
          </span>
        ))}
      </div>

      {/* The deep read's extracted fields. Cached per JD (not per profile), so
          these survive a résumé edit and are present even when the fit half
          has been invalidated. */}
      {llm && (
        <div className="mt-3 space-y-1.5 border-t border-edge pt-2.5 text-[11.5px]">
          {llm.status_reason && (
            <p className="text-muted">
              <span className="text-subtle">Read as {llm.posting_status}: </span>
              {llm.status_reason}
            </p>
          )}
          {llm.sponsorship_required === "yes" && (
            <p className="flex items-start gap-1.5 text-danger">
              <AlertTriangle size={12} className="mt-px shrink-0" />
              The posting requires work authorization you do not have.
            </p>
          )}
          {llm.tech_stack && llm.tech_stack.length > 0 && (
            <p className="text-muted">
              <span className="text-subtle">Stack asked for: </span>
              {llm.tech_stack.join(", ")}
            </p>
          )}
          {llm.seniority && (
            <p className="text-muted">
              <span className="text-subtle">Level: </span>
              {llm.seniority}
            </p>
          )}
          {llm.compensation_text && (
            <p className="text-muted">
              <span className="text-subtle">Pay in the text: </span>
              {llm.compensation_text}
            </p>
          )}
          {llm.inconsistencies && llm.inconsistencies.length > 0 && (
            <ul className="ml-4 list-disc text-highlight">
              {llm.inconsistencies.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
