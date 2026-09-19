import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CalendarClock,
  Check,
  Clock,
  IndianRupee,
  Loader2,
  RotateCcw,
  Save,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";

import { fetchPrefs, savePrefs } from "@/lib/api";
import type { Prefs, PrefsPatch } from "@/lib/types";
import { RunPanel } from "./RunPanel";
import { cx } from "./primitives";

/**
 * The preference panel — "what do I want out of the board right now?"
 *
 * This is the third of three config layers, and the panel only edits one of
 * them on purpose:
 *
 *   - `filters.yaml` decides ELIGIBILITY at ingest: can I hold this job at
 *     all (India-eligible, not junior, not leadership). NOT editable here.
 *     It gates Telegram alerts, so tuning it from a UI slider would silently
 *     rewrite which jobs wake your phone.
 *   - `profile.yaml` is WHO I AM and drives the fit score.
 *   - `prefs.yaml` — this — is WHAT I WANT, and gates only the Matches list.
 *
 * The Jobs tile ignores every control here. That separation is the whole
 * point: Jobs is the entire board, Matches is the board filtered to what you
 * asked for, and nothing in this panel can hide a row from Jobs.
 *
 * Saving is cheap and says so. Preferences select rows; they never change a
 * score, so a save costs a ~10s re-rank and zero LLM calls. The panel
 * therefore saves and prompts you to Run, rather than blocking on a
 * confirmation dialog about cost.
 */

const EMPLOYMENT_LABELS: Record<string, string> = {
  full_time: "Full-time",
  part_time: "Part-time",
  contract: "Contract",
  internship: "Internship",
  temporary: "Temporary",
  volunteer: "Volunteer",
};

const WORKPLACE_LABELS: Record<string, string> = {
  remote: "Remote",
  hybrid: "Hybrid",
  onsite: "On-site",
};

/** Capped at 30 server-side — older postings are no use. */
const POSTED_PRESETS = [
  { label: "24h", value: 1 },
  { label: "3 days", value: 3 },
  { label: "7 days", value: 7 },
  { label: "14 days", value: 14 },
  { label: "1 month", value: 30 },
];

/** INR presets. Written in lakh because that is how Indian pay is quoted. */
const PAY_PRESETS = [
  { label: "No floor", value: 0 },
  { label: "₹20L", value: 2_000_000 },
  { label: "₹30L", value: 3_000_000 },
  { label: "₹50L", value: 5_000_000 },
  { label: "₹75L", value: 7_500_000 },
  { label: "₹1Cr", value: 10_000_000 },
];

function formatInr(value: number): string {
  if (!value) return "no floor";
  if (value >= 10_000_000) return `₹${(value / 10_000_000).toFixed(2).replace(/\.00$/, "")}Cr`;
  return `₹${Math.round(value / 100_000)}L`;
}

function Chip({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-pressed={active}
      className={cx(
        "rounded-lg border px-2.5 py-1 text-[12px] font-medium transition-colors",
        active
          ? "border-accent bg-accent/15 text-accent-text"
          : "border-edge bg-panel text-muted hover:border-edge-strong hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

function Section({
  icon,
  title,
  hint,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="border-b border-edge px-4 py-3.5 last:border-b-0 md:px-5">
      <div className="mb-2 flex items-center gap-2">
        <span className="grid size-6 place-items-center rounded-md bg-accent/12 text-accent-text">
          {icon}
        </span>
        <div className="text-[12.5px] font-semibold text-ink">{title}</div>
      </div>
      {hint && <p className="mb-2.5 text-[11.5px] leading-relaxed text-subtle">{hint}</p>}
      {children}
    </div>
  );
}

export function PrefsPanel({
  open,
  onOpen,
  onClose,
  onRunningChange,
  onRunFinished,
  externalChangeAt = 0,
}: {
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  onRunningChange: (running: boolean) => void;
  onRunFinished: () => void;
  /** A scope change made outside this dialog ("Send to Matches"). */
  externalChangeAt?: number;
}) {
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: ["prefs"], queryFn: fetchPrefs, staleTime: 30_000 });

  // Local draft. The panel is an editor, not a live filter: applying each
  // keystroke would re-rank on every click and make the cost of a change
  // impossible to reason about.
  const [draft, setDraft] = useState<Prefs | null>(null);
  useEffect(() => {
    if (prefs.data) setDraft(prefs.data);
  }, [prefs.data]);

  // Bumped on a plain Save. RunPanel watches it to show "preferences changed
  // — run to apply them", because a saved preference does nothing to the
  // list until the (free) ranking pass re-gates the rows. A save made by
  // "Save & run" skips the bump: the run it precedes already applies it.
  const [savedAt, setSavedAt] = useState(0);

  const save = useMutation({
    mutationFn: ({ patch }: { patch: PrefsPatch; nudge: boolean }) => savePrefs(patch),
    onSuccess: (stored, { nudge }) => {
      qc.setQueryData(["prefs"], stored);
      setDraft(stored);
      if (nudge) setSavedAt(Date.now());
    },
  });

  const dirty = useMemo(() => {
    if (!draft || !prefs.data) return false;
    return JSON.stringify(stripMeta(draft)) !== JSON.stringify(stripMeta(prefs.data));
  }, [draft, prefs.data]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  const d = draft;

  // Always mounted, hidden when closed — the Run control inside keeps polling
  // a pass that outlives the dialog, and reopens it for the cost confirm.
  return (
    <Modal open={open} onClose={onClose} labelledBy="prefs-dialog-title">
      {/* ------------------------------------------------------------ header */}
      <div className="flex shrink-0 items-center gap-2 border-b border-edge px-4 py-3 md:px-5">
        <span className="grid size-7 place-items-center rounded-lg bg-accent/12 text-accent-text">
          <SlidersHorizontal size={15} />
        </span>
        <div className="leading-tight">
          <div id="prefs-dialog-title" className="text-[13.5px] font-semibold text-ink">
            Preferences
          </div>
          <div className="text-[11px] text-subtle">
            Filters the Matches list only — Jobs always shows the whole board
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {d && dirty && (
            <button
              type="button"
              onClick={() => prefs.data && setDraft(prefs.data)}
              className="flex items-center gap-1.5 rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-[12px] text-muted transition-colors hover:border-edge-strong hover:text-ink"
            >
              <RotateCcw size={13} />
              Revert
            </button>
          )}
          <button
            type="button"
            disabled={!d || !dirty || save.isPending}
            onClick={() => d && save.mutate({ patch: toPatch(d), nudge: true })}
            className={cx(
              "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] font-medium transition-colors",
              dirty && !save.isPending
                ? "bg-accent text-white hover:bg-accent/90"
                : "cursor-not-allowed border border-edge bg-panel text-subtle",
            )}
          >
            {save.isPending ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
            {save.isPending ? "Saving" : dirty ? "Save" : "Saved"}
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close preferences"
            className="rounded-lg border border-edge bg-panel px-2 py-1.5 text-muted transition-colors hover:border-edge-strong hover:text-ink"
          >
            <X size={14} />
          </button>
        </div>
      </div>

      {save.isError && (
        <div className="flex items-start gap-2 border-b border-edge bg-danger/10 px-4 py-2.5 text-[12px] text-danger md:px-5">
          <AlertTriangle size={14} className="mt-px shrink-0" />
          <span>{(save.error as Error).message}</span>
        </div>
      )}

      {/* ------------------------------------------------------------- body */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {!d ? (
          <div className="flex items-center gap-2 px-5 py-8 text-[12.5px] text-subtle">
            <Loader2 size={14} className="animate-spin" />
            Loading preferences…
          </div>
        ) : (
          <>
            <Section
              icon={<CalendarClock size={13} />}
              title="Posted within"
              hint="A hard limit: anything posted more than a month ago never reaches Matches, and this can only tighten it."
            >
              <div className="flex flex-wrap gap-1.5">
                {POSTED_PRESETS.map((preset) => (
                  <Chip
                    key={preset.value}
                    active={d.freshness.max_age_days === preset.value}
                    onClick={() => setDraft({ ...d, freshness: { max_age_days: preset.value } })}
                  >
                    {preset.label}
                  </Chip>
                ))}
              </div>
            </Section>

            <Section
              icon={<Sparkles size={13} />}
              title="Work mode"
              hint="How the work is done, from the board's own field — not a keyword guess. Select none to accept any."
            >
              <div className="flex flex-wrap gap-1.5">
                {d.workplace_options.map((opt) => (
                  <Chip
                    key={opt}
                    active={d.work.workplace_types.includes(opt)}
                    onClick={() =>
                      setDraft({
                        ...d,
                        work: {
                          ...d.work,
                          workplace_types: toggle(d.work.workplace_types, opt),
                        },
                      })
                    }
                  >
                    {WORKPLACE_LABELS[opt] ?? opt}
                  </Chip>
                ))}
              </div>
            </Section>

            <Section
              icon={<Check size={13} />}
              title="Commitment"
              hint="Permanent and Full-time are one option: Lever publishes them as alternatives for the same commitment, so splitting them would drop 92 good rows."
            >
              <div className="flex flex-wrap gap-1.5">
                {d.employment_options.map((opt) => (
                  <Chip
                    key={opt}
                    active={d.work.employment_types.includes(opt)}
                    onClick={() =>
                      setDraft({
                        ...d,
                        work: {
                          ...d.work,
                          employment_types: toggle(d.work.employment_types, opt),
                        },
                      })
                    }
                  >
                    {EMPLOYMENT_LABELS[opt] ?? opt}
                  </Chip>
                ))}
              </div>
            </Section>

            <Section
              icon={<Clock size={13} />}
              title="Experience the posting asks for"
              hint="Bounds on what the posting ASKS for, when it says. 5–8 keeps “5+ years” roles and drops 2–4 year ones. Where a JD names several figures the largest is used."
            >
              <div className="flex items-center gap-3">
                <NumberField
                  label="Min years"
                  value={d.experience.min_years}
                  max={d.experience.max_years}
                  onChange={(v) =>
                    setDraft({ ...d, experience: { ...d.experience, min_years: v } })
                  }
                />
                <NumberField
                  label="Max years"
                  value={d.experience.max_years}
                  min={d.experience.min_years}
                  onChange={(v) =>
                    setDraft({ ...d, experience: { ...d.experience, max_years: v } })
                  }
                />
              </div>
            </Section>

            <Section
              icon={<IndianRupee size={13} />}
              title="Pay floor"
              hint="Applied to a STATED salary, annualized from any currency. About 84% of postings state nothing at all."
            >
              <div className="flex flex-wrap gap-1.5">
                {PAY_PRESETS.map((preset) => (
                  <Chip
                    key={preset.value}
                    active={d.compensation.min_annual_inr === preset.value}
                    onClick={() =>
                      setDraft({
                        ...d,
                        compensation: { ...d.compensation, min_annual_inr: preset.value },
                      })
                    }
                  >
                    {preset.label}
                  </Chip>
                ))}
              </div>
              <label className="mt-2.5 flex cursor-pointer items-start gap-2 text-[12px] text-muted">
                <input
                  type="checkbox"
                  checked={d.compensation.require_stated}
                  onChange={(e) =>
                    setDraft({
                      ...d,
                      compensation: {
                        ...d.compensation,
                        require_stated: e.target.checked,
                      },
                    })
                  }
                  className="mt-0.5 size-3.5 accent-accent"
                />
                <span>
                  Only jobs that state pay
                  <span className="ml-1 text-subtle">
                    — excludes ~84% of the board, so expect a short list
                  </span>
                </span>
              </label>
            </Section>

            <Section
              icon={<AlertTriangle size={13} />}
              title="Minimum validity"
              hint="Discounts stale, evergreen and ghost postings. Rows the validity pass has not scored yet always pass, so this never empties a fresh board."
            >
              <div className="flex flex-wrap gap-1.5">
                {[0, 50, 70, 85].map((v) => (
                  <Chip
                    key={v}
                    active={d.validity.min_score === v}
                    onClick={() => setDraft({ ...d, validity: { min_score: v } })}
                  >
                    {v === 0 ? "Off" : `${v}+`}
                  </Chip>
                ))}
              </div>
            </Section>

            <Section
              icon={<Check size={13} />}
              title="Unstated facts"
              hint="Greenhouse publishes no commitment or work mode at all — 2,224 open rows. Requiring stated facts drops every one of them for something their board never wrote down."
            >
              <label className="flex cursor-pointer items-start gap-2 text-[12px] text-muted">
                <input
                  type="checkbox"
                  checked={d.include_unstated}
                  onChange={(e) => setDraft({ ...d, include_unstated: e.target.checked })}
                  className="mt-0.5 size-3.5 accent-accent"
                />
                <span>
                  Treat silence as a pass
                  <span className="ml-1 text-subtle">— recommended</span>
                </span>
              </label>
            </Section>
          </>
        )}
      </div>

      {/* ------------------------------------------------------------ footer */}
      {d && (
        <div className="shrink-0 border-t border-edge px-4 py-2.5 text-[11px] text-subtle md:px-5">
          <span className="font-mono">{d.version.slice(0, 8)}</span>
          {" · "}
          {d.work.workplace_types.map((w) => WORKPLACE_LABELS[w] ?? w).join(", ") || "any mode"}
          {" · "}
          {d.work.employment_types.map((e) => EMPLOYMENT_LABELS[e] ?? e).join(", ") ||
            "any commitment"}
          {" · "}
          ≤ {d.freshness.max_age_days}d
          {" · "}
          {d.experience.min_years}–{d.experience.max_years}y
          {" · "}
          {formatInr(d.compensation.min_annual_inr)}
          {dirty && <span className="ml-1 text-highlight">— unsaved</span>}
        </div>
      )}

      {/* --------------------------------------------------------------- run */}
      <div className="max-h-[45%] shrink-0 overflow-y-auto border-t border-edge bg-panel/40">
        <RunPanel
          prefsDirtySince={Math.max(savedAt, externalChangeAt)}
          unsaved={dirty}
          beforeRun={async () => {
            if (d && dirty) await save.mutateAsync({ patch: toPatch(d), nudge: false });
          }}
          onFinished={onRunFinished}
          onRunningChange={onRunningChange}
          onNeedsConfirm={onOpen}
          onDeepReadDone={onClose}
        />
      </div>
    </Modal>
  );
}

/** Backdrop + centred panel. Hidden rather than unmounted when closed, so
 *  state inside it (a live run's polling) survives closing the dialog. */
function Modal({
  open,
  onClose,
  labelledBy,
  children,
}: {
  open: boolean;
  onClose: () => void;
  labelledBy: string;
  children: React.ReactNode;
}) {
  return (
    <div hidden={!open}>
      <div
        onClick={onClose}
        className="animate-fade-in fixed inset-0 z-40 bg-black/55 backdrop-blur-[4px]"
        aria-hidden
      />
      <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby={labelledBy}
          className="glass-strong animate-fade-up pointer-events-auto flex max-h-[min(88vh,780px)] w-full max-w-[620px] flex-col overflow-hidden rounded-2xl border border-edge-strong"
        >
          {children}
        </div>
      </div>
    </div>
  );
}

function NumberField({
  label,
  value,
  min = 0,
  max = 40,
  onChange,
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-[12px] text-muted">
      <span>{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(e) => {
          const next = Number.parseInt(e.target.value, 10);
          if (Number.isFinite(next)) onChange(Math.min(max, Math.max(min, next)));
        }}
        className="w-16 rounded-lg border border-edge bg-panel px-2 py-1 text-[12px] text-ink outline-none focus:border-accent"
      />
    </label>
  );
}

function toggle(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

/** Drop the fields the server owns, so "dirty" tracks real edits only. */
function stripMeta(p: Prefs) {
  // `transfer` is owned by "Send to Matches", not edited in this dialog.
  const { version, updated_at, workplace_options, employment_options, transfer, ...rest } = p;
  void version;
  void updated_at;
  void workplace_options;
  void employment_options;
  void transfer;
  return rest;
}

function toPatch(p: Prefs): PrefsPatch {
  return {
    work: p.work,
    experience: p.experience,
    compensation: p.compensation,
    validity: p.validity,
    freshness: p.freshness,
    scope: p.scope,
    include_unstated: p.include_unstated,
  };
}
