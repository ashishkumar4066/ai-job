import { ArrowUpRight, BriefcaseBusiness, Ghost, Loader2, MessageSquare, Send, Trophy, X, XCircle, type LucideIcon } from "lucide-react";

import { APPLICATION_STATUSES, STATUS_LABELS, type ApplicationStatus } from "@/lib/types";
import { cx } from "./primitives";
import { SelectMenu } from "./SelectMenu";

/**
 * The stage control for one application, shared by every surface that shows an
 * applied job: the job table, the Matches list, the job drawer and the
 * Dashboard.
 *
 * Why a dropdown on the row and not a Dashboard-only edit
 * ------------------------------------------------------
 * Nothing moves an application past `applied` on its own, and nothing can: no
 * board tells us a recruiter replied. Every stage after the first is a human
 * assertion, so the pipeline is only ever as populated as this control is easy
 * to reach. Sending the user to a second screen to record "they replied" is how
 * a tracker ends up with everything sitting in Applied forever — the number it
 * is worst at is the one it exists to show.
 *
 * The colour comes from the stage, not from "applied at all", so a column of
 * these is scannable: sky = sent, violet = screening, amber = interviewing,
 * emerald = offer, rose = rejected, grey = ghosted.
 */
export const STATUS_META: Record<
  ApplicationStatus,
  { icon: LucideIcon; tone: string; ring: string }
> = {
  applied: { icon: Send, tone: "text-sky-200", ring: "border-sky-400/35 bg-sky-400/10" },
  screening: {
    icon: MessageSquare,
    tone: "text-violet-200",
    ring: "border-violet-400/35 bg-violet-400/10",
  },
  interviewing: {
    icon: BriefcaseBusiness,
    tone: "text-amber-200",
    ring: "border-amber-400/35 bg-amber-400/10",
  },
  offer: { icon: Trophy, tone: "text-emerald-200", ring: "border-emerald-400/35 bg-emerald-400/10" },
  rejected: { icon: XCircle, tone: "text-rose-200", ring: "border-rose-400/35 bg-rose-400/10" },
  ghosted: { icon: Ghost, tone: "text-subtle", ring: "border-edge bg-white/5" },
};

export function ApplicationControl({
  status,
  applyUrl,
  jobTitle,
  onStatus,
  onUnapply,
  statusPending = false,
  unapplying = false,
  compact = false,
  className,
}: {
  status: ApplicationStatus;
  /** The ATS form. Still reachable once applied — re-reading a question you
   *  already answered is the commonest reason to come back to a row. */
  applyUrl: string;
  /** Only for the labels screen readers announce; several of these are on screen. */
  jobTitle: string;
  onStatus: (status: ApplicationStatus) => void;
  /** Undo. A hard delete server-side, so a misclick leaves no trace in the counts. */
  onUnapply: () => void;
  statusPending?: boolean;
  unapplying?: boolean;
  /** Table density: smaller type and icons. */
  compact?: boolean;
  className?: string;
}) {
  const meta = STATUS_META[status];
  const Icon = meta.icon;
  const size = compact ? 12 : 14;
  return (
    <span
      // `stopPropagation` on the wrapper: every surface but the Dashboard puts
      // this inside a row that opens the drawer on click, and each of the three
      // controls would otherwise need its own copy.
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
      className={cx(
        "flex items-center gap-0.5 rounded-lg border py-0.5 pr-0.5 pl-1.5",
        compact ? "text-[11.5px]" : "text-[13px]",
        "font-semibold",
        meta.ring,
        meta.tone,
        className,
      )}
    >
      <Icon size={size} className="shrink-0" />

      {/* The label IS the trigger. A separate chevron button would be a second
          hit target for one action, and this column has ~90px to spend.

          Each stage carries its own icon in the list, because the colour that
          makes a column of these scannable is only on the closed control — the
          open menu is one surface, so the row needs another way to say which
          stage it is. */}
      <SelectMenu
        value={status}
        disabled={statusPending}
        onChange={onStatus}
        label={`Application stage for ${jobTitle}`}
        title="Where this application stands. Nothing moves it for you — set it when you hear back."
        className="py-1 pr-0.5 pl-1 font-semibold hover:opacity-90 focus-visible:underline"
        chevronSize={size - 1}
        trailing={
          statusPending ? (
            <Loader2 size={size - 1} className="shrink-0 animate-spin opacity-70" />
          ) : undefined
        }
        options={APPLICATION_STATUSES.map((value) => {
          const StageIcon = STATUS_META[value].icon;
          return {
            value,
            label: STATUS_LABELS[value],
            icon: <StageIcon size={13} className={STATUS_META[value].tone} />,
          };
        })}
      />

      <a
        href={applyUrl}
        target="_blank"
        rel="noopener noreferrer"
        title="Re-open the application form"
        aria-label={`Re-open the application form for ${jobTitle}`}
        className="shrink-0 rounded-md p-1 opacity-70 transition-colors hover:bg-white/10 hover:opacity-100"
      >
        <ArrowUpRight size={size} />
      </a>

      <button
        type="button"
        onClick={onUnapply}
        disabled={unapplying}
        title="I didn't actually apply — remove this from the Dashboard"
        aria-label={`Undo — remove the application for ${jobTitle}`}
        className="shrink-0 rounded-md p-1 opacity-70 transition-colors hover:bg-white/10 hover:text-danger hover:opacity-100"
      >
        {unapplying ? <Loader2 size={size} className="animate-spin" /> : <X size={size} />}
      </button>
    </span>
  );
}
