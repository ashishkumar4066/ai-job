import { BrainCircuit, ShieldCheck } from "lucide-react";
import { absoluteDate } from "@/lib/format";
import { validityBand } from "@/lib/types";
import type { MatchBand } from "@/lib/types";
import { cx } from "./primitives";

/** Fit bands, shared by the ranker's score and the LLM's verdict. */
export const FIT_BANDS: { id: MatchBand; label: string; tone: string; dot: string }[] = [
  { id: "excellent", label: "Excellent", tone: "text-success", dot: "bg-success" },
  { id: "strong", label: "Strong", tone: "text-accent-text", dot: "bg-accent" },
  { id: "moderate", label: "Moderate", tone: "text-highlight", dot: "bg-highlight" },
  { id: "weak", label: "Weak", tone: "text-muted", dot: "bg-muted" },
  { id: "poor", label: "Poor", tone: "text-subtle", dot: "bg-subtle" },
];

export function fitBand(id: string | undefined) {
  return FIT_BANDS.find((b) => b.id === id);
}

/**
 * The "Validator" and "LLM" tags.
 *
 * The rule is presence: a tag is shown ONLY when that check ran on this job,
 * and no tag means it did not run. There is deliberately no "not run"
 * placeholder and no bare number — a bare "95" beside a fit score of 42 read
 * as two contradictory scores. The validity number lives in the tooltip,
 * labelled as validity; a low result turns the tag amber or red.
 */

export function VerifierBadge({
  score,
  reasons,
  checkedAt,
}: {
  score: number | null;
  reasons?: string[];
  checkedAt?: string | null;
}) {
  if (score == null) return null;

  const band = validityBand(score);
  const penalties = (reasons ?? []).filter((r) => r.includes(":-")).map((r) => r.split(":-")[0]);
  const title = [
    `Validator ran${checkedAt ? ` on ${absoluteDate(checkedAt)}` : ""}.`,
    `Validity ${score}/100 — how real and current the posting looks (not how well it fits you).`,
    penalties.length ? `Penalties: ${penalties.join(", ")}.` : "No penalties.",
  ].join(" ");

  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10.5px] font-medium whitespace-nowrap",
        band === "suspect"
          ? "border-danger/30 bg-danger/10 text-danger"
          : band === "questionable"
            ? "border-highlight/30 bg-highlight/12 text-highlight"
            : "border-success/25 bg-success/10 text-success",
      )}
    >
      <ShieldCheck size={10} />
      Validator
    </span>
  );
}

export function LlmBadge({ read, band }: { read: boolean; band?: string }) {
  if (!read) return null;
  const label = fitBand(band)?.label;
  return (
    <span
      title={
        label
          ? `The LLM read this job's current description against your profile and rated the fit ${label}.`
          : "The LLM has read this job's current description"
      }
      className="inline-flex items-center gap-1 rounded-md border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10.5px] font-medium whitespace-nowrap text-accent-text"
    >
      <BrainCircuit size={10} />
      LLM
    </span>
  );
}
