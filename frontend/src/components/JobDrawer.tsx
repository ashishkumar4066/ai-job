import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowUpRight,
  CalendarDays,
  Check,
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
import { api } from "@/lib/api";
import { absoluteDate, formatSalary, relativeTime, titleCase } from "@/lib/format";
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

        {/* Meta grid */}
        {job && (
          <div className="relative grid grid-cols-2 gap-x-4 gap-y-3.5 border-b border-edge px-5 py-4 sm:grid-cols-3">
            <Meta
              icon={job.remote ? <Globe2 size={13} /> : <MapPin size={13} />}
              label="Location"
              value={job.locations.length ? job.locations.join(" · ") : job.remote ? "Remote" : "—"}
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
            <p className="rounded-xl border border-danger/30 bg-danger/10 p-4 text-[13px] text-danger">
              Couldn't load this description: {(error as Error).message}
            </p>
          ) : data?.description_html ? (
            // Server-rendered ATS markup. Same-origin backend, personal-scale
            // tool; Phase 2b sanitizes this when the LLM validator lands.
            <div className="jd" dangerouslySetInnerHTML={{ __html: data.description_html }} />
          ) : data?.description_text ? (
            <p className="jd whitespace-pre-wrap">{data.description_text}</p>
          ) : (
            <p className="text-[13px] text-subtle">This posting has no description on the board.</p>
          )}
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
              Apply on {titleCase(job.ats)}
              <ArrowUpRight size={15} />
            </a>
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
