import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Building2,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  Clock,
  ExternalLink,
  Globe2,
  Layers,
  MapPin,
  Server,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import { absoluteDate, relativeTime, titleCase } from "@/lib/format";
import type { Job } from "@/lib/types";
import { Badge, CompanyAvatar, cx } from "./primitives";

export function JobDrawer({
  jobId,
  summary,
  onClose,
  onPrev,
  onNext,
  hasPrev,
  hasNext,
}: {
  jobId: number;
  summary: Job | undefined;
  onClose: () => void;
  onPrev: () => void;
  onNext: () => void;
  hasPrev: boolean;
  hasNext: boolean;
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId),
    staleTime: 5 * 60_000,
  });

  // Escape closes the drawer, matching the browser-back behaviour.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const job = data ?? summary;

  return (
    <>
      <div
        onClick={onClose}
        className="fixed inset-0 z-40 bg-black/35 backdrop-blur-[3px] transition-opacity"
        aria-hidden
      />

      <aside
        role="dialog"
        aria-modal="true"
        aria-label={job?.title ?? "Job details"}
        className="glass-strong animate-slide-in fixed top-0 right-0 z-50 flex h-full w-full max-w-[640px] flex-col rounded-l-3xl border-l border-edge-strong"
      >
        {/* Header */}
        <div className="flex items-start gap-3 border-b border-edge px-5 py-4">
          {job && <CompanyAvatar name={job.company} size={42} />}
          <div className="min-w-0 flex-1">
            {job ? (
              <>
                <h2 className="text-[17px] leading-snug font-semibold tracking-tight text-ink">
                  {job.title}
                </h2>
                <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted">
                  <span className="font-medium">{job.company}</span>
                  <span className="opacity-40">·</span>
                  <span className="capitalize">{job.ats}</span>
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
              className="grid size-8 place-items-center rounded-lg border border-edge text-muted transition-colors hover:bg-panel-hover hover:text-ink disabled:opacity-30 disabled:hover:bg-transparent"
            >
              <ChevronUp size={15} />
            </button>
            <button
              onClick={onNext}
              disabled={!hasNext}
              aria-label="Next job (j)"
              title="Next job — j"
              className="grid size-8 place-items-center rounded-lg border border-edge text-muted transition-colors hover:bg-panel-hover hover:text-ink disabled:opacity-30 disabled:hover:bg-transparent"
            >
              <ChevronDown size={15} />
            </button>
            <button
              onClick={onClose}
              aria-label="Close (Esc)"
              className="ml-1 grid size-8 place-items-center rounded-lg border border-edge text-muted transition-colors hover:bg-panel-hover hover:text-ink"
            >
              <X size={15} />
            </button>
          </div>
        </div>

        {/* Meta grid */}
        {job && (
          <div className="grid grid-cols-2 gap-x-4 gap-y-3 border-b border-edge px-5 py-4 sm:grid-cols-3">
            <Meta
              icon={job.remote ? <Globe2 size={13} /> : <MapPin size={13} />}
              label="Location"
              value={job.locations.length ? job.locations.join(" · ") : job.remote ? "Remote" : "—"}
            />
            <Meta icon={<Layers size={13} />} label="Department" value={job.department ?? "—"} />
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
            <Meta icon={<Server size={13} />} label="Source" value={job.source_key} mono />
          </div>
        )}

        {/* Description */}
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-5">
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
            <p className="rounded-xl border border-danger/25 bg-danger/10 p-4 text-[13px] text-danger">
              Couldn't load this description: {(error as Error).message}
            </p>
          ) : data?.description_html ? (
            // Server-rendered ATS markup. Same-origin backend, personal-scale
            // tool; Phase 2b sanitizes this when the LLM validator lands.
            <div className="jd" dangerouslySetInnerHTML={{ __html: data.description_html }} />
          ) : data?.description_text ? (
            <p className="jd whitespace-pre-wrap">{data.description_text}</p>
          ) : (
            <p className="text-[13px] text-subtle">
              This posting has no description on the board.
            </p>
          )}
        </div>

        {/* Footer */}
        {job && (
          <div className="flex items-center gap-3 border-t border-edge px-5 py-4">
            <a
              href={job.apply_url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-accent to-accent-strong px-4 py-2.5 text-[14px] font-semibold text-white shadow-[0_8px_24px_-10px_var(--accent)] transition-all hover:opacity-95 active:scale-[0.99]"
            >
              <Building2 size={15} />
              Apply on {titleCase(job.ats)}
              <ExternalLink size={14} />
            </a>
            <button
              onClick={() => navigator.clipboard?.writeText(job.apply_url)}
              className="rounded-xl border border-edge bg-panel px-3.5 py-2.5 text-[13px] font-medium text-muted transition-colors hover:bg-panel-hover hover:text-ink"
              title="Copy the application link"
            >
              Copy link
            </button>
          </div>
        )}
      </aside>
    </>
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
      <p className="flex items-center gap-1.5 text-[11px] font-medium tracking-wide text-subtle uppercase">
        <span className="opacity-70">{icon}</span>
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
