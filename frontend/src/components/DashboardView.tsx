import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpRight,
  CheckCircle2,
  Clock,
  Coins,
  FileText,
  Inbox,
  Loader2,
  Radar,
  Send,
  Target,
} from "lucide-react";

import { fetchApplications, fetchDashboard, patchApplication, unmarkApplied } from "@/lib/api";
import { ApplicationControl, STATUS_META } from "./ApplicationControl";
import { compactNumber, relativeTime } from "@/lib/format";
import {
  APPLICATION_STATUSES,
  CLOSED_STATUSES,
  STATUS_LABELS,
  type Application,
  type ApplicationStatus,
  type Dashboard,
} from "@/lib/types";
import { CompanyAvatar, EmptyState, cx } from "./primitives";

/**
 * The Dashboard — what I have sent, what is still open, and whether the machine
 * behind it is still healthy.
 *
 * Three bands, in the order the questions actually get asked:
 *
 *   1. **Applications** — the pipeline, and the list that needs chasing. This is
 *      the reason the panel exists.
 *   2. **Pipeline health** — is the board still feeding me, and how far does it
 *      narrow? Read from counts other surfaces already compute.
 *   3. **Budget** — tokens against today's cap, which is what decides whether
 *      another tailored document is affordable.
 *
 * Everything comes from one `/dashboard` request. Separate calls per band would
 * let the numbers on screen disagree with each other, and the panel is not
 * useful half-populated.
 *
 * Nothing here writes except the status control on a row, and clicking a job
 * opens it in Jobs rather than duplicating the drawer.
 */


export function DashboardView({
  onOpenJob,
  onBrowseJobs,
}: {
  onOpenJob: (jobId: number) => void;
  onBrowseJobs: () => void;
}) {
  const board = useQuery({
    queryKey: ["dashboard"],
    queryFn: fetchDashboard,
    staleTime: 30_000,
  });

  if (board.isLoading) {
    return (
      <div className="glass-strong flex flex-1 items-center justify-center gap-2 rounded-2xl text-[13px] text-subtle">
        <Loader2 size={16} className="animate-spin" /> Reading your pipeline…
      </div>
    );
  }

  if (board.isError) {
    return (
      <div className="glass-strong flex flex-1 items-center justify-center rounded-2xl">
        <EmptyState
          icon={<AlertTriangle size={24} />}
          title="Couldn't load the dashboard"
          description={(board.error as Error).message}
          action={
            <button
              onClick={() => board.refetch()}
              className="btn-primary mt-1 rounded-xl px-4 py-2 text-[13px] font-semibold"
            >
              Retry
            </button>
          }
        />
      </div>
    );
  }

  const data = board.data!;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pb-2">
      <Headline data={data} />
      <Pipeline data={data} onBrowseJobs={onBrowseJobs} />
      <ApplicationsList data={data} onOpenJob={onOpenJob} onBrowseJobs={onBrowseJobs} />
      <div className="grid gap-3 lg:grid-cols-2">
        <Activity data={data} />
        <div className="flex flex-col gap-3">
          <Health data={data} />
          <Budget data={data} />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- the headline */

function Headline({ data }: { data: Dashboard }) {
  const cards = [
    {
      label: "Applied",
      value: data.total_applications,
      hint: `${data.applied_last_7d} in the last 7 days`,
      icon: Send,
      tone: "text-accent-text",
    },
    {
      label: "Awaiting a reply",
      value: data.awaiting,
      hint: data.silent
        ? `${data.silent} quiet for ${data.silent_after_days}d+`
        : "all within the window",
      icon: Inbox,
      tone: data.silent > 0 ? "text-amber-200" : "text-ink",
    },
    {
      label: "Still in play",
      value: data.active,
      hint: "not offered, rejected or ghosted",
      icon: Clock,
      tone: "text-ink",
    },
    {
      label: "Heard back",
      value: data.response_rate === null ? "—" : `${Math.round(data.response_rate * 100)}%`,
      // A rejection after an interview is still a response, so this is read off
      // the status history rather than the current stage.
      hint: data.response_rate === null ? "no applications yet" : "of everything sent",
      icon: CheckCircle2,
      tone: "text-ink",
    },
  ];

  return (
    <div className="grid shrink-0 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {cards.map((card) => (
        <div key={card.label} className="glass-strong rounded-2xl px-4 py-3.5">
          <div className="flex items-center gap-2 text-[11.5px] text-subtle">
            <card.icon size={13} />
            {card.label}
          </div>
          <div className={cx("mt-1 text-[26px] leading-none font-semibold tabular-nums", card.tone)}>
            {typeof card.value === "number" ? card.value.toLocaleString() : card.value}
          </div>
          <div className="mt-1.5 text-[11px] text-subtle">{card.hint}</div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------- the pipeline */

function Pipeline({ data, onBrowseJobs }: { data: Dashboard; onBrowseJobs: () => void }) {
  const total = data.total_applications;
  if (total === 0) {
    return (
      <div className="glass-strong shrink-0 rounded-2xl px-4 py-6">
        <EmptyState
          icon={<Send size={22} />}
          title="No applications tracked yet"
          description="Applying from a job row records it here automatically. Nothing else to set up."
          action={
            <button
              onClick={onBrowseJobs}
              className="btn-primary mt-1 flex items-center gap-1.5 rounded-xl px-4 py-2 text-[13px] font-semibold"
            >
              Find something to apply to
              <ArrowUpRight size={14} />
            </button>
          }
        />
      </div>
    );
  }

  return (
    <section className="glass-strong shrink-0 rounded-2xl px-4 py-3.5">
      <h2 className="text-[12.5px] font-semibold text-ink">Pipeline</h2>
      <div className="mt-3 flex flex-wrap gap-2">
        {APPLICATION_STATUSES.map((status) => {
          const count = data.by_status[status] ?? 0;
          const meta = STATUS_META[status];
          const share = total ? Math.round((count / total) * 100) : 0;
          return (
            <div
              key={status}
              className={cx(
                "min-w-[116px] flex-1 rounded-xl border px-3 py-2.5",
                count > 0 ? meta.ring : "border-edge bg-white/[0.02]",
              )}
            >
              <div
                className={cx(
                  "flex items-center gap-1.5 text-[11px]",
                  count > 0 ? meta.tone : "text-subtle",
                )}
              >
                <meta.icon size={12} />
                {STATUS_LABELS[status]}
              </div>
              <div
                className={cx(
                  "mt-1 text-[20px] leading-none font-semibold tabular-nums",
                  count > 0 ? "text-ink" : "text-subtle",
                )}
              >
                {count}
              </div>
              <div className="mt-1 h-1 overflow-hidden rounded-full bg-white/8">
                <div
                  className={cx("h-full rounded-full", count > 0 ? "bg-current" : "")}
                  style={{ width: `${share}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/* ---------------------------------------------------------- the application list */

type Scope = "open" | "silent" | "all";

function ApplicationsList({
  data,
  onOpenJob,
  onBrowseJobs,
}: {
  data: Dashboard;
  onOpenJob: (jobId: number) => void;
  onBrowseJobs: () => void;
}) {
  // Defaults to what needs chasing when something has gone quiet, and to the
  // open set otherwise — the list is a worklist, not an archive.
  const [scope, setScope] = useState<Scope>(data.silent > 0 ? "silent" : "open");

  const openStatuses = useMemo(
    () => APPLICATION_STATUSES.filter((s) => !CLOSED_STATUSES.includes(s)),
    [],
  );

  const list = useQuery({
    queryKey: ["applications", scope],
    queryFn: () =>
      fetchApplications(
        scope === "all"
          ? {}
          : scope === "silent"
            ? { silentOnly: true }
            : { status: openStatuses },
      ),
    staleTime: 15_000,
  });

  if (data.total_applications === 0) return null;

  const items = list.data?.items ?? [];
  const tabs: { id: Scope; label: string; count?: number }[] = [
    { id: "open", label: "Open", count: data.active },
    { id: "silent", label: `Quiet ${data.silent_after_days}d+`, count: data.silent },
    { id: "all", label: "All", count: data.total_applications },
  ];

  return (
    <section className="glass-strong shrink-0 rounded-2xl">
      <header className="flex flex-wrap items-center gap-2 border-b border-edge px-4 py-2.5">
        <h2 className="mr-1 text-[12.5px] font-semibold text-ink">Applications</h2>
        <div className="flex gap-1">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setScope(tab.id)}
              className={cx(
                "rounded-lg px-2.5 py-1 text-[11.5px] font-medium transition-colors",
                scope === tab.id
                  ? "bg-accent/15 text-accent-text"
                  : "text-subtle hover:bg-panel-hover hover:text-ink",
              )}
            >
              {tab.label}
              {tab.count !== undefined && (
                <span className="ml-1.5 tabular-nums opacity-70">{tab.count}</span>
              )}
            </button>
          ))}
        </div>
        {list.isFetching && <Loader2 size={13} className="animate-spin text-subtle" />}
      </header>

      {items.length === 0 ? (
        <p className="px-4 py-6 text-center text-[12.5px] text-subtle">
          {scope === "silent"
            ? `Nothing has been quiet for ${data.silent_after_days} days. `
            : "Nothing here. "}
          {scope !== "all" && (
            <button
              onClick={() => setScope("all")}
              className="underline underline-offset-2 hover:text-ink"
            >
              Show all
            </button>
          )}
        </p>
      ) : (
        <ul className="divide-y divide-edge/60">
          {items.map((application) => (
            <ApplicationRow
              key={application.job_id}
              application={application}
              onOpenJob={onOpenJob}
            />
          ))}
        </ul>
      )}

      <footer className="border-t border-edge px-4 py-2">
        <button
          onClick={onBrowseJobs}
          className="flex items-center gap-1 text-[11.5px] text-subtle transition-colors hover:text-ink"
        >
          Browse the board
          <ArrowUpRight size={12} />
        </button>
      </footer>
    </section>
  );
}

function ApplicationRow({
  application,
  onOpenJob,
}: {
  application: Application;
  onOpenJob: (jobId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    void queryClient.invalidateQueries({ queryKey: ["applications"] });
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    void queryClient.invalidateQueries({ queryKey: ["matches"] });
  };

  const move = useMutation({
    mutationFn: (status: ApplicationStatus) => patchApplication(application.job_id, { status }),
    onSuccess: invalidate,
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  const remove = useMutation({
    mutationFn: () => unmarkApplied(application.job_id),
    onSuccess: invalidate,
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5">
      <CompanyAvatar name={application.company} size={30} />

      <button
        onClick={() => onOpenJob(application.job_id)}
        className="min-w-0 flex-1 text-left"
        title="Open this posting"
      >
        <span className="block truncate text-[13px] font-medium text-ink">{application.title}</span>
        <span className="block truncate text-[11.5px] text-subtle">
          {application.company}
          {application.status_of_posting === "closed" && (
            <span className="ml-1.5 text-amber-200/80">· posting closed</span>
          )}
        </span>
      </button>

      <span
        className="shrink-0 text-[11px] text-subtle tabular-nums"
        title={`Applied ${relativeTime(application.applied_at)}${
          application.source === "apply_click" ? " (recorded from the Apply button)" : ""
        }`}
      >
        {relativeTime(application.applied_at)}
      </span>

      {application.silent && (
        <span
          className="shrink-0 rounded-md border border-amber-400/35 bg-amber-400/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-200"
          title={`No movement for ${application.days_silent} days. Mark it ghosted if you have given up on it — nothing does that automatically.`}
        >
          quiet {application.days_silent}d
        </span>
      )}

      <ApplicationControl
        compact
        className="shrink-0"
        status={application.status}
        applyUrl={application.apply_url}
        jobTitle={application.title}
        onStatus={(status) => move.mutate(status)}
        onUnapply={() => remove.mutate()}
        statusPending={move.isPending}
        unapplying={remove.isPending}
      />

      {error && <span className="w-full text-[11px] text-danger">{error}</span>}
    </li>
  );
}

/* ---------------------------------------------------------------- activity */

function Activity({ data }: { data: Dashboard }) {
  const peak = Math.max(1, ...data.activity.map((w) => Math.max(w.applications, w.jobs_found)));
  const totalApplications = data.activity.reduce((sum, w) => sum + w.applications, 0);

  return (
    <section className="glass-strong rounded-2xl px-4 py-3.5">
      <div className="flex items-baseline justify-between">
        <h2 className="text-[12.5px] font-semibold text-ink">Last 12 weeks</h2>
        <span className="text-[11px] text-subtle">
          {totalApplications} applied · {compactNumber(
            data.activity.reduce((sum, w) => sum + w.jobs_found, 0),
          )}{" "}
          found
        </span>
      </div>

      {/* Two bars per week rather than a line chart: twelve points is too few
          for a line to mean anything, and the comparison being made is
          "how many did I send against how many were there", which is paired. */}
      <div className="mt-3 flex h-28 items-end gap-1.5">
        {data.activity.map((week) => (
          <div key={week.week} className="group flex min-w-0 flex-1 flex-col items-center gap-1">
            <div className="flex h-24 w-full items-end justify-center gap-[2px]">
              <span
                title={`${week.jobs_found} jobs found in the week of ${week.week}`}
                className="w-1/2 rounded-t bg-white/12 transition-colors group-hover:bg-white/20"
                style={{ height: `${(week.jobs_found / peak) * 100}%` }}
              />
              <span
                title={`${week.applications} applications in the week of ${week.week}`}
                className="w-1/2 rounded-t bg-accent/70 transition-colors group-hover:bg-accent"
                style={{ height: `${(week.applications / peak) * 100}%` }}
              />
            </div>
            <span className="truncate text-[9px] text-subtle">
              {week.week.slice(5).replace("-", "/")}
            </span>
          </div>
        ))}
      </div>

      <div className="mt-2 flex items-center gap-3 text-[10.5px] text-subtle">
        <span className="flex items-center gap-1">
          <span className="size-2 rounded-sm bg-accent/70" /> applied
        </span>
        <span className="flex items-center gap-1">
          <span className="size-2 rounded-sm bg-white/12" /> jobs found
        </span>
      </div>
    </section>
  );
}

/* ----------------------------------------------------------- pipeline health */

function Health({ data }: { data: Dashboard }) {
  const rows = [
    { label: "Open on the board", value: data.jobs_open, icon: Radar },
    { label: "Eligible for me", value: data.jobs_eligible, icon: Target },
    { label: `Eligible & fresh`, value: data.jobs_fresh, icon: Clock },
    { label: "Scored", value: data.matches_scored, icon: Target },
    { label: "Documents drafted", value: data.documents, icon: FileText },
  ];

  return (
    <section className="glass-strong rounded-2xl px-4 py-3.5">
      <div className="flex items-baseline justify-between">
        <h2 className="text-[12.5px] font-semibold text-ink">Pipeline health</h2>
        <span className="text-[11px] text-subtle" title="Last completed sweep of every board">
          {data.last_sweep_at
            ? `swept ${relativeTime(data.last_sweep_at)}`
            : "never swept"}
        </span>
      </div>
      <dl className="mt-2.5 space-y-1.5">
        {rows.map((row) => (
          <div key={row.label} className="flex items-center gap-2 text-[12px]">
            <row.icon size={12} className="shrink-0 text-subtle" />
            <dt className="min-w-0 flex-1 truncate text-muted">{row.label}</dt>
            <dd className="shrink-0 font-medium text-ink tabular-nums">
              {row.value.toLocaleString()}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/* --------------------------------------------------------------- llm budget */

function Budget({ data }: { data: Dashboard }) {
  const cap = data.tokens_per_day;
  const share = cap ? Math.min(100, Math.round((data.tokens_today / cap) * 100)) : null;
  // Past ~80% the next document may not fit, which is the decision this panel
  // exists to inform.
  const tight = share !== null && share >= 80;

  return (
    <section className="glass-strong rounded-2xl px-4 py-3.5">
      <div className="flex items-baseline justify-between">
        <h2 className="flex items-center gap-1.5 text-[12.5px] font-semibold text-ink">
          <Coins size={13} className="text-subtle" />
          Today's budget
        </h2>
        <span className="truncate text-[11px] text-subtle" title={data.model}>
          {data.provider}
        </span>
      </div>

      <div className="mt-2 flex items-baseline gap-1.5">
        <span
          className={cx(
            "text-[22px] leading-none font-semibold tabular-nums",
            tight ? "text-amber-200" : "text-ink",
          )}
        >
          {compactNumber(data.tokens_today)}
        </span>
        {cap && <span className="text-[12px] text-subtle">/ {compactNumber(cap)} tokens</span>}
      </div>

      {share !== null && (
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/8">
          <div
            className={cx("h-full rounded-full", tight ? "bg-amber-300/80" : "bg-accent/70")}
            style={{ width: `${share}%` }}
          />
        </div>
      )}

      <p className="mt-2 text-[11px] text-subtle">
        {data.requests_today.toLocaleString()} calls
        {data.requests_per_day
          ? ` of ${compactNumber(data.requests_per_day)}`
          : ""}{" "}
        · {data.deep_reads.toLocaleString()} deep reads all time
      </p>
      {tight && (
        <p className="mt-1.5 text-[11px] text-amber-200">
          Close to today's cap — another tailored document may not fit.
        </p>
      )}
    </section>
  );
}
