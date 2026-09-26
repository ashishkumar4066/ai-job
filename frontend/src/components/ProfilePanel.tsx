import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  FileText,
  Info,
  Loader2,
  Save,
  ShieldAlert,
  Sparkles,
  Upload,
  UserRound,
  X,
} from "lucide-react";

import { ApiError, fetchProfileDocument, saveProfile, uploadResume } from "@/lib/api";
import type { ProfileData, ProfileForm } from "@/lib/types";
import { cx } from "./primitives";

/**
 * The profile editor — Phase 2C Stage 1's "editable from the dashboard".
 *
 * `profile.yaml` on disk stays the source of truth, so this panel round-trips
 * the **raw mapping** rather than a typed view of it: a key the backend's
 * `Profile` model does not declare rides through a save untouched. An editor
 * that silently deleted what it did not understand would make hand-editing a
 * second-class path, which is exactly what CLAUDE.md says it must not be.
 *
 * Two entry points, one component
 * -------------------------------
 * With no profile on disk it opens on the **upload step** and cannot be
 * dismissed: scoring, Matches and every generated document need a profile, so
 * there is nothing useful behind it. With a profile, it opens on the **review
 * step** as an ordinary editor.
 *
 * Why the upload is two steps and not one
 * ---------------------------------------
 * The LLM reads the résumé and proposes a profile, but three sections are not
 * descriptions of the résumé — they are constraints on what may be written
 * about it:
 *
 *   - `gaps:` is the fact-checker's DENYLIST. A stack listed here is blocked
 *     from every generated résumé and cover letter. One the model failed to
 *     notice is a protection that silently stops applying.
 *   - `evidence[].depth` decides whether a capability reads as production
 *     experience or a side project, which the fit prompt leans on.
 *   - `unproven:` is about what the résumé OMITS, which cannot be read off
 *     what it contains.
 *
 * So the draft is shown for review and nothing is written until Save. The LLM
 * removes the typing, not the judgement.
 */

/* ------------------------------------------------------------ form <-> data */

type SkillMap = Record<string, Record<string, number>>;

/** `ai: llm:3, rag:3` — one line per category.
 *
 *  A line-based text field rather than a tag picker, deliberately: this is 80
 *  weighted terms across 6 categories, it mirrors the YAML's own nesting, and
 *  it stays fast to edit for someone who already knows what they want to say.
 */
function serializeSkills(skills: SkillMap): string {
  return Object.entries(skills)
    .map(([category, entries]) => {
      const terms = Object.entries(entries)
        .map(([name, weight]) => `${name}:${weight}`)
        .join(", ");
      return `${category}: ${terms}`;
    })
    .join("\n");
}

function parseSkills(text: string, maxWeight: number): SkillMap {
  const out: SkillMap = {};
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const at = line.indexOf(":");
    if (at < 0) continue;
    const category = line.slice(0, at).trim().toLowerCase();
    if (!category) continue;
    const bucket = (out[category] ??= {});
    for (const term of line.slice(at + 1).split(",")) {
      const parts = term.split(":");
      const name = parts[0]?.trim().toLowerCase();
      if (!name) continue;
      const weight = Number.parseInt(parts[1] ?? "", 10);
      bucket[name] = Math.min(maxWeight, Math.max(1, Number.isFinite(weight) ? weight : 2));
    }
  }
  return out;
}

function serializeGaps(gaps: Record<string, number>): string {
  return Object.entries(gaps)
    .map(([name, weight]) => `${name}:${weight}`)
    .join(", ");
}

function parseGaps(text: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const term of text.split(/[,\n]/)) {
    const parts = term.split(":");
    const name = parts[0]?.trim().toLowerCase();
    if (!name) continue;
    const weight = Number.parseInt(parts[1] ?? "", 10);
    out[name] = Math.min(2, Math.max(1, Number.isFinite(weight) ? weight : 2));
  }
  return out;
}

function lines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function commas(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

type Section = Record<string, unknown>;

function section(data: ProfileData, key: string): Section {
  const value = data[key];
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Section) : {};
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function formFromData(data: ProfileData): ProfileForm {
  const identity = section(data, "identity");
  const seniority = section(data, "seniority");
  const compensation = section(data, "compensation");
  const auth = section(data, "work_authorization");
  const evidence = Array.isArray(data.evidence) ? data.evidence : [];
  return {
    full_name: str(identity.full_name),
    email: str(identity.email),
    phone: str(identity.phone),
    location: str(identity.location),
    country: str(identity.country, "IN"),
    summary: str(data.summary),
    total_years: num(seniority.total_years),
    ai_years: num(seniority.ai_years),
    current_title: str(seniority.current_title),
    target_titles: Array.isArray(seniority.target_titles)
      ? seniority.target_titles.map((t) => String(t))
      : [],
    min_annual_inr: num(compensation.min_annual_inr),
    needs_sponsorship: bool(auth.needs_sponsorship, true),
    remote_only: bool(auth.remote_only, true),
    skills: (data.skills as SkillMap) ?? {},
    gaps: (data.gaps as Record<string, number>) ?? {},
    evidence: evidence.map((entry) => {
      const row = (entry ?? {}) as Section;
      return {
        area: str(row.area),
        depth: str(row.depth) === "production" ? "production" : "project",
        proof: str(row.proof),
      };
    }),
    unproven: Array.isArray(data.unproven) ? data.unproven.map((u) => String(u)) : [],
    domains: Array.isArray(data.domains) ? data.domains.map((d) => String(d)) : [],
  };
}

/** Merge the form back over the raw mapping, preserving everything else. */
function dataFromForm(raw: ProfileData, form: ProfileForm): ProfileData {
  return {
    ...raw,
    identity: {
      ...section(raw, "identity"),
      full_name: form.full_name.trim(),
      email: form.email.trim(),
      phone: form.phone.trim(),
      location: form.location.trim(),
      country: form.country.trim().toUpperCase() || "IN",
    },
    summary: form.summary.trim(),
    seniority: {
      ...section(raw, "seniority"),
      total_years: form.total_years,
      ai_years: Math.min(form.total_years, form.ai_years),
      current_title: form.current_title.trim(),
      target_titles: form.target_titles,
    },
    compensation: {
      ...section(raw, "compensation"),
      min_annual_inr: form.min_annual_inr,
    },
    work_authorization: {
      ...section(raw, "work_authorization"),
      needs_sponsorship: form.needs_sponsorship,
      remote_only: form.remote_only,
    },
    skills: form.skills,
    gaps: form.gaps,
    evidence: form.evidence.filter((e) => e.area.trim() && e.proof.trim()),
    unproven: form.unproven,
    domains: form.domains,
  };
}

function countSkills(skills: SkillMap): number {
  return Object.values(skills).reduce((sum, entries) => sum + Object.keys(entries).length, 0);
}

/* ---------------------------------------------------------------- the panel */

export function ProfilePanel({
  open,
  onClose,
  blocking = false,
  startAt,
}: {
  open: boolean;
  onClose: () => void;
  /** No profile on disk: the dialog cannot be dismissed. */
  blocking?: boolean;
  /**
   * Which step to open on. Only "upload" is honoured, and only when a profile
   * already exists — an unconfigured profile has nothing to review anyway.
   */
  startAt?: "upload";
}) {
  const queryClient = useQueryClient();
  const document_ = useQuery({
    queryKey: ["profile-document"],
    queryFn: fetchProfileDocument,
    enabled: open,
    staleTime: 30_000,
  });

  const [step, setStep] = useState<"upload" | "review">("upload");
  const [raw, setRaw] = useState<ProfileData | null>(null);
  const [form, setForm] = useState<ProfileForm | null>(null);
  const [skillsText, setSkillsText] = useState("");
  const [gapsText, setGapsText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string[]>([]);
  const [resumeText, setResumeText] = useState("");
  const [saved, setSaved] = useState(false);

  const configured = document_.data?.configured ?? false;

  const load = useCallback((data: ProfileData) => {
    const next = formFromData(data);
    setRaw(data);
    setForm(next);
    setSkillsText(serializeSkills(next.skills));
    setGapsText(serializeGaps(next.gaps));
  }, []);

  // Open on the editor when there is something to edit, on the uploader when
  // there is not. Re-runs when the dialog opens, not on every render.
  useEffect(() => {
    if (!open || !document_.data) return;
    setError(null);
    setSaved(false);
    if (document_.data.configured) {
      // Load either way: the uploader's "keep what I have" path steps straight
      // into the editor, which needs the form already filled.
      load(document_.data.data);
      setStep(startAt ?? "review");
    } else {
      setStep("upload");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, startAt, document_.data?.configured, document_.dataUpdatedAt]);

  const patch = useCallback((changes: Partial<ProfileForm>) => {
    setForm((current) => (current ? { ...current, ...changes } : current));
    setSaved(false);
  }, []);

  const upload = useMutation({
    mutationFn: ({ tex, pdf, parse }: { tex: File; pdf: File | null; parse: boolean }) =>
      uploadResume(tex, pdf, { parse }),
    onSuccess: (draft) => {
      load(draft.data);
      setResumeText(draft.resume_text);
      setNotice(draft.warnings);
      setError(null);
      setStep("review");
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  const save = useMutation({
    mutationFn: () => saveProfile(dataFromForm(raw ?? {}, form!)),
    onSuccess: (profile) => {
      setError(null);
      setSaved(true);
      queryClient.setQueryData(["profile"], profile);
      // A new profile_version means every stored score and document is keyed
      // to the old one. Re-reading is what makes a stale score render as stale
      // rather than sitting on screen looking current.
      void queryClient.invalidateQueries({ queryKey: ["profile-document"] });
      void queryClient.invalidateQueries({ queryKey: ["matches"] });
      void queryClient.invalidateQueries({ queryKey: ["matches-funnel"] });
      void queryClient.invalidateQueries({ queryKey: ["base-resume"] });
      if (blocking) onClose();
    },
    onError: (e: unknown) =>
      setError(
        e instanceof ApiError && e.status === 422
          ? `${e.message} Nothing was written — fix it and save again.`
          : e instanceof Error
            ? e.message
            : String(e),
      ),
  });

  // Commit the two text areas into the form on blur rather than per keystroke:
  // re-parsing mid-word reorders and re-cases what is being typed.
  const commitSkills = useCallback(
    () => patch({ skills: parseSkills(skillsText, 3) }),
    [patch, skillsText],
  );
  const commitGaps = useCallback(() => patch({ gaps: parseGaps(gapsText) }), [patch, gapsText]);

  const pending = save.isPending || upload.isPending;
  const onSave = useCallback(() => {
    // Blur may not have fired if Save was reached by keyboard.
    const next = {
      skills: parseSkills(skillsText, 3),
      gaps: parseGaps(gapsText),
    };
    setForm((current) => (current ? { ...current, ...next } : current));
    setTimeout(() => save.mutate(), 0);
  }, [save, skillsText, gapsText]);

  useEffect(() => {
    if (!open || blocking) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, blocking, onClose]);

  if (!open) return null;

  return (
    <div>
      <div
        onClick={blocking ? undefined : onClose}
        className="animate-fade-in fixed inset-0 z-40 bg-black/60 backdrop-blur-[4px]"
        aria-hidden
      />
      <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center p-3 md:p-5">
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="profile-title"
          className="glass-strong animate-fade-up pointer-events-auto flex max-h-[min(92vh,900px)] w-full max-w-[860px] flex-col overflow-hidden rounded-2xl border border-edge-strong"
        >
          <header className="flex shrink-0 items-center gap-3 border-b border-edge px-5 py-3.5">
            <span className="grid size-8 place-items-center rounded-xl bg-accent/15 text-accent">
              <UserRound size={16} />
            </span>
            <div className="min-w-0 flex-1">
              <h2 id="profile-title" className="truncate text-[14px] font-semibold text-ink">
                {!configured
                  ? "Set up your profile"
                  : step === "upload"
                    ? "Your résumé"
                    : "Your profile"}
              </h2>
              <p className="truncate text-[11.5px] text-subtle">
                {step !== "upload"
                  ? document_.data?.path || "profile.yaml"
                  : configured
                    ? "Replace your résumé files"
                    : "Upload your résumé to get started"}
              </p>
            </div>
            {document_.data?.version && (
              <span className="hidden rounded-lg border border-edge px-2 py-1 font-mono text-[10.5px] text-subtle sm:block">
                {document_.data.version}
              </span>
            )}
            {!blocking && (
              <button
                type="button"
                onClick={onClose}
                aria-label="Close"
                className="rounded-lg p-1.5 text-subtle transition-colors hover:bg-panel-hover hover:text-ink"
              >
                <X size={16} />
              </button>
            )}
          </header>

          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            {document_.isLoading ? (
              <div className="flex items-center gap-2 py-10 text-[13px] text-subtle">
                <Loader2 size={15} className="animate-spin" /> Reading profile.yaml…
              </div>
            ) : step === "upload" ? (
              <UploadStep
                pending={upload.isPending}
                onUpload={(tex, pdf, parse) => upload.mutate({ tex, pdf, parse })}
                onSkip={configured ? () => setStep("review") : undefined}
              />
            ) : form ? (
              <ReviewStep
                form={form}
                patch={patch}
                skillsText={skillsText}
                gapsText={gapsText}
                onSkillsText={setSkillsText}
                onGapsText={setGapsText}
                onCommitSkills={commitSkills}
                onCommitGaps={commitGaps}
                resumeText={resumeText}
                hasTemplate={document_.data?.has_template ?? false}
                onReupload={() => setStep("upload")}
              />
            ) : null}
          </div>

          <footer className="flex shrink-0 flex-wrap items-center gap-2 border-t border-edge px-5 py-3">
            {error && (
              <span className="flex min-w-0 flex-1 items-start gap-1.5 text-[11.5px] text-danger">
                <AlertTriangle size={13} className="mt-px shrink-0" />
                <span className="min-w-0">{error}</span>
              </span>
            )}
            {!error && notice.length > 0 && (
              <span className="flex min-w-0 flex-1 items-start gap-1.5 text-[11.5px] text-warn">
                <Info size={13} className="mt-px shrink-0" />
                <span className="min-w-0">{notice.join(" ")}</span>
              </span>
            )}
            {!error && notice.length === 0 && (
              <span className="min-w-0 flex-1 text-[11.5px] text-subtle">
                {step === "review" && form
                  ? `${countSkills(form.skills)} skills · ${Object.keys(form.gaps).length} gaps · ${form.evidence.length} evidence lines`
                  : "Nothing is saved until you review it."}
              </span>
            )}
            {saved && (
              <span className="flex items-center gap-1 text-[11.5px] font-medium text-ok">
                <Check size={13} /> Saved
              </span>
            )}
            {step === "review" && (
              <button
                type="button"
                onClick={onSave}
                disabled={pending || !form}
                className="btn-primary flex items-center gap-1.5 rounded-xl px-4 py-2 text-[12.5px] font-semibold disabled:cursor-not-allowed disabled:opacity-50"
              >
                {save.isPending ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <Save size={14} />
                )}
                Save profile
              </button>
            )}
          </footer>
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- the steps */

function UploadStep({
  pending,
  onUpload,
  onSkip,
}: {
  pending: boolean;
  onUpload: (tex: File, pdf: File | null, parse: boolean) => void;
  onSkip?: () => void;
}) {
  const [tex, setTex] = useState<File | null>(null);
  const [pdf, setPdf] = useState<File | null>(null);

  return (
    <div className="space-y-4">
      <p className="text-[12.5px] leading-relaxed text-muted">
        Two files, two jobs. The <strong className="text-ink">.tex</strong> becomes the template
        every tailored résumé is built from — tailoring only ever re-words regions of your real
        document, so without it there is nothing to tailor. The{" "}
        <strong className="text-ink">PDF</strong> is what gets attached to application forms.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <FilePick
          label="Résumé LaTeX source"
          hint="Required · .tex"
          accept=".tex,text/x-tex,text/plain"
          file={tex}
          onPick={setTex}
          icon={<FileText size={15} />}
        />
        <FilePick
          label="Compiled résumé"
          hint="Optional · .pdf"
          accept=".pdf,application/pdf,.doc,.docx"
          file={pdf}
          onPick={setPdf}
          icon={<Upload size={15} />}
        />
      </div>

      <div className="rounded-xl border border-edge bg-panel/60 p-3.5">
        <p className="flex items-start gap-2 text-[12px] leading-relaxed text-muted">
          <Sparkles size={14} className="mt-px shrink-0 text-accent" />
          <span>
            <strong className="text-ink">Read my résumé</strong> sends the .tex to the model once
            and fills the form in for you — roughly 5–7k tokens, one time. You review everything
            before it is written, and the sections that matter most (gaps, evidence depth) are the
            ones worth your eye.
          </span>
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={!tex || pending}
          onClick={() => tex && onUpload(tex, pdf, true)}
          className="btn-primary flex items-center gap-1.5 rounded-xl px-4 py-2 text-[12.5px] font-semibold disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pending ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
          Read my résumé
        </button>
        <button
          type="button"
          disabled={!tex || pending}
          onClick={() => tex && onUpload(tex, pdf, false)}
          className="rounded-xl border border-edge px-3.5 py-2 text-[12.5px] font-medium text-muted transition-colors hover:bg-panel-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
        >
          Store the files only
        </button>
        {onSkip && (
          <button
            type="button"
            onClick={onSkip}
            className="ml-auto text-[12px] text-subtle underline-offset-2 hover:text-ink hover:underline"
          >
            Back to the editor
          </button>
        )}
      </div>
    </div>
  );
}

function FilePick({
  label,
  hint,
  accept,
  file,
  onPick,
  icon,
}: {
  label: string;
  hint: string;
  accept: string;
  file: File | null;
  onPick: (file: File | null) => void;
  icon: React.ReactNode;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <button
      type="button"
      onClick={() => input.current?.click()}
      className={cx(
        "flex items-center gap-3 rounded-xl border px-3.5 py-3 text-left transition-colors",
        file ? "border-accent/50 bg-accent/5" : "border-edge hover:bg-panel-hover",
      )}
    >
      <span
        className={cx(
          "grid size-8 shrink-0 place-items-center rounded-lg",
          file ? "bg-accent/15 text-accent" : "bg-panel text-subtle",
        )}
      >
        {file ? <Check size={15} /> : icon}
      </span>
      <span className="min-w-0">
        <span className="block truncate text-[12.5px] font-medium text-ink">
          {file ? file.name : label}
        </span>
        <span className="block text-[11px] text-subtle">
          {file ? `${(file.size / 1024).toFixed(0)} KB — click to change` : hint}
        </span>
      </span>
      <input
        ref={input}
        type="file"
        accept={accept}
        hidden
        onChange={(e) => onPick(e.target.files?.[0] ?? null)}
      />
    </button>
  );
}

function ReviewStep({
  form,
  patch,
  skillsText,
  gapsText,
  onSkillsText,
  onGapsText,
  onCommitSkills,
  onCommitGaps,
  resumeText,
  hasTemplate,
  onReupload,
}: {
  form: ProfileForm;
  patch: (changes: Partial<ProfileForm>) => void;
  skillsText: string;
  gapsText: string;
  onSkillsText: (value: string) => void;
  onGapsText: (value: string) => void;
  onCommitSkills: () => void;
  onCommitGaps: () => void;
  resumeText: string;
  hasTemplate: boolean;
  onReupload: () => void;
}) {
  const [showRead, setShowRead] = useState(false);

  return (
    <div className="space-y-5">
      {!hasTemplate && (
        <Callout tone="warn" icon={<AlertTriangle size={14} />}>
          No résumé template on disk, so tailoring is unavailable.{" "}
          <button
            type="button"
            onClick={onReupload}
            className="font-medium text-ink underline underline-offset-2"
          >
            Upload your .tex
          </button>
          .
        </Callout>
      )}

      {resumeText && (
        <div>
          <button
            type="button"
            onClick={() => setShowRead((v) => !v)}
            className="text-[11.5px] text-subtle underline-offset-2 hover:text-ink hover:underline"
          >
            {showRead ? "Hide" : "Show"} what was read from your résumé
          </button>
          {showRead && (
            <pre className="mt-2 max-h-52 overflow-auto rounded-xl border border-edge bg-panel/60 p-3 text-[11px] leading-relaxed whitespace-pre-wrap text-muted">
              {resumeText}
            </pre>
          )}
        </div>
      )}

      <Group title="Identity" note="Generated documents are signed with this.">
        <div className="grid gap-3 sm:grid-cols-2">
          <Text
            label="Full name"
            value={form.full_name}
            onChange={(v) => patch({ full_name: v })}
          />
          <Text label="Location" value={form.location} onChange={(v) => patch({ location: v })} />
          <Text label="Email" value={form.email} onChange={(v) => patch({ email: v })} />
          <Text label="Phone" value={form.phone} onChange={(v) => patch({ phone: v })} />
        </div>
      </Group>

      <Group
        title="Seniority"
        note="Drives the years band in Matches and the seniority read on every title."
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Text
            label="Current title"
            value={form.current_title}
            onChange={(v) => patch({ current_title: v })}
          />
          <Text
            label="Target titles"
            hint="Comma separated"
            value={form.target_titles.join(", ")}
            onChange={(v) => patch({ target_titles: commas(v) })}
          />
          <NumberField
            label="Total years"
            value={form.total_years}
            max={50}
            onChange={(v) => patch({ total_years: v })}
          />
          <NumberField
            label="AI/ML years"
            value={form.ai_years}
            max={50}
            onChange={(v) => patch({ ai_years: v })}
          />
        </div>
      </Group>

      <Group title="Summary" note="Used when drafting documents. Never invented from.">
        <Area value={form.summary} rows={3} onChange={(v) => patch({ summary: v })} />
      </Group>

      <Group
        title="Skills"
        note="One category per line: ai: llm:3, rag:3 — weight 3 means production, repeatedly."
      >
        <Area
          value={skillsText}
          rows={7}
          mono
          onChange={onSkillsText}
          onBlur={onCommitSkills}
          placeholder={"ai: llm:3, rag:3\nbackend: python:3, fastapi:3"}
        />
      </Group>

      <Group
        title="Gaps"
        tone="danger"
        icon={<ShieldAlert size={13} />}
        note="The fact-checker's denylist — the section most worth checking by hand."
      >
        <Callout tone="danger" icon={<ShieldAlert size={14} />}>
          Anything listed here is <strong className="text-ink">blocked</strong> from every generated
          résumé and cover letter, even if you ask for it in chat. That is what stops a document
          claiming a stack you cannot defend in an interview. Removing an entry removes that
          protection.
        </Callout>
        <Area
          value={gapsText}
          rows={3}
          mono
          onChange={onGapsText}
          onBlur={onCommitGaps}
          placeholder="kubernetes:2, aws:2, terraform:2"
        />
      </Group>

      <Group
        title="Evidence"
        note="What the résumé proves, and how deeply. The fit prompt reads this before the skills list."
      >
        <EvidenceRows rows={form.evidence} onChange={(evidence) => patch({ evidence })} />
      </Group>

      <Group title="Unproven" note="What the résumé does NOT show. One per line, in plain prose.">
        <Area
          value={form.unproven.join("\n")}
          rows={4}
          onChange={(v) => patch({ unproven: lines(v) })}
          placeholder={
            "no people management (mentors, no direct reports stated)\nno high-traffic consumer scale"
          }
        />
      </Group>

      <Group title="Domains" note="Industries you have shipped in. Comma separated.">
        <Text
          label=""
          value={form.domains.join(", ")}
          onChange={(v) => patch({ domains: commas(v) })}
        />
      </Group>

      <Group title="Preferences" note="The pay floor and work rules the Matches gate uses.">
        <div className="grid gap-3 sm:grid-cols-2">
          <NumberField
            label="Minimum annual (INR)"
            value={form.min_annual_inr}
            max={100_000_000}
            step={100_000}
            onChange={(v) => patch({ min_annual_inr: v })}
          />
          <div className="flex flex-col justify-center gap-2">
            <Toggle
              label="Needs visa sponsorship"
              checked={form.needs_sponsorship}
              onChange={(v) => patch({ needs_sponsorship: v })}
            />
            <Toggle
              label="Remote only"
              checked={form.remote_only}
              onChange={(v) => patch({ remote_only: v })}
            />
          </div>
        </div>
      </Group>
    </div>
  );
}

function EvidenceRows({
  rows,
  onChange,
}: {
  rows: ProfileForm["evidence"];
  onChange: (rows: ProfileForm["evidence"]) => void;
}) {
  const update = (index: number, changes: Partial<ProfileForm["evidence"][number]>) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, ...changes } : row)));

  return (
    <div className="space-y-2">
      {rows.map((row, index) => (
        <div key={index} className="rounded-xl border border-edge bg-panel/50 p-2.5">
          <div className="flex items-center gap-2">
            <input
              value={row.area}
              onChange={(e) => update(index, { area: e.target.value })}
              placeholder="Capability"
              className="min-w-0 flex-1 rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-[12px] text-ink outline-none focus:border-accent"
            />
            <select
              value={row.depth}
              onChange={(e) =>
                update(index, {
                  depth: e.target.value as "production" | "project",
                })
              }
              className="rounded-lg border border-edge bg-panel px-2 py-1.5 text-[11.5px] text-ink outline-none focus:border-accent"
            >
              <option value="production">production</option>
              <option value="project">project</option>
            </select>
            <button
              type="button"
              onClick={() => onChange(rows.filter((_, i) => i !== index))}
              aria-label="Remove"
              className="rounded-lg p-1.5 text-subtle transition-colors hover:bg-panel-hover hover:text-danger"
            >
              <X size={14} />
            </button>
          </div>
          <textarea
            value={row.proof}
            onChange={(e) => update(index, { proof: e.target.value })}
            rows={2}
            placeholder="The achievement that proves it, with the résumé's own numbers."
            className="mt-2 w-full resize-y rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-[12px] leading-relaxed text-ink outline-none focus:border-accent"
          />
        </div>
      ))}
      <button
        type="button"
        onClick={() => onChange([...rows, { area: "", depth: "project", proof: "" }])}
        className="rounded-lg border border-edge px-2.5 py-1.5 text-[11.5px] font-medium text-muted transition-colors hover:bg-panel-hover hover:text-ink"
      >
        + Add evidence
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ atoms */

function Group({
  title,
  note,
  tone,
  icon,
  children,
}: {
  title: string;
  note?: string;
  tone?: "danger";
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <div>
        <h3
          className={cx(
            "flex items-center gap-1.5 text-[12.5px] font-semibold",
            tone === "danger" ? "text-danger" : "text-ink",
          )}
        >
          {icon}
          {title}
        </h3>
        {note && <p className="mt-0.5 text-[11px] leading-relaxed text-subtle">{note}</p>}
      </div>
      {children}
    </section>
  );
}

function Callout({
  tone,
  icon,
  children,
}: {
  tone: "warn" | "danger";
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <p
      className={cx(
        "flex items-start gap-2 rounded-xl border px-3 py-2.5 text-[11.5px] leading-relaxed",
        tone === "danger"
          ? "border-danger/35 bg-danger/8 text-danger"
          : "border-warn/35 bg-warn/8 text-warn",
      )}
    >
      <span className="mt-px shrink-0">{icon}</span>
      <span className="min-w-0">{children}</span>
    </p>
  );
}

function Text({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      {label && (
        <span className="mb-1 block text-[11px] text-subtle">
          {label}
          {hint && <span className="text-subtle/70"> · {hint}</span>}
        </span>
      )}
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-[12.5px] text-ink outline-none focus:border-accent"
      />
    </label>
  );
}

function NumberField({
  label,
  value,
  max,
  step = 1,
  onChange,
}: {
  label: string;
  value: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] text-subtle">{label}</span>
      <input
        type="number"
        min={0}
        max={max}
        step={step}
        value={value}
        onChange={(e) => {
          const next = Number(e.target.value);
          if (Number.isFinite(next)) onChange(Math.min(max, Math.max(0, next)));
        }}
        className="w-full rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-[12.5px] text-ink outline-none focus:border-accent"
      />
    </label>
  );
}

function Area({
  value,
  rows,
  mono,
  placeholder,
  onChange,
  onBlur,
}: {
  value: string;
  rows: number;
  mono?: boolean;
  placeholder?: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
}) {
  return (
    <textarea
      value={value}
      rows={rows}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      onBlur={onBlur}
      spellCheck={!mono}
      className={cx(
        "w-full resize-y rounded-lg border border-edge bg-panel px-2.5 py-2 text-ink outline-none focus:border-accent",
        mono ? "font-mono text-[11.5px] leading-relaxed" : "text-[12.5px] leading-relaxed",
      )}
    />
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-[12px] text-muted">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="size-3.5 accent-accent"
      />
      {label}
    </label>
  );
}
