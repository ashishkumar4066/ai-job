/**
 * A stand-in for the deep read.
 *
 * The real pass sends each shortlisted JD and the profile brief to the provider
 * and stores what comes back. A static demo has no provider, and a demo whose
 * headline feature answers "not available" is not showing the feature — so this
 * composes a verdict instead.
 *
 * It is a composition, not an invention: every strength comes from the skills
 * the deterministic matcher really did find in that JD, every gap from the
 * stacks it really did miss, and the band from the score it really did compute.
 * The wording is templated. That is the honest limit of what a browser can do
 * here, and `model` says `demo` so nothing downstream mistakes one of these for
 * a paid read.
 */

import type { LlmFitVerdict, MatchBand } from "@/lib/types";
import type { MatchRow } from "../snapshot";

/** Marks a verdict this module composed. Real ones name the provider's model. */
export const DEMO_MODEL = "demo-stand-in";

const BANDS: MatchBand[] = ["poor", "weak", "moderate", "strong", "excellent"];

/** A stable small integer per job, so a verdict never changes between reloads. */
function seed(jobId: number): number {
  let hash = 0x811c9dc5 ^ jobId;
  for (let i = 0; i < 4; i += 1) hash = Math.imul(hash ^ (hash >>> 13), 0x01000193) >>> 0;
  return hash >>> 0;
}

/**
 * What the profile can actually show for a skill the JD asked for.
 *
 * Lifted from `profile.yaml`'s `evidence:` block — the same lines the real fit
 * prompt sends — so a strength names the project it came from rather than
 * echoing the JD back.
 */
const EVIDENCE: [RegExp, string][] = [
  [/\bagentic|langgraph|langchain|agent\b/i, "Agentic workflow in production (LangChain/LangGraph, human-in-the-loop)"],
  [/\brag|retrieval|vector|embedding|qdrant\b/i, "Schema-grounded RAG shipped to production, plus a hybrid-search side project"],
  [/\bllm|gpt|gemini|mistral|openai|anthropic|claude\b/i, "LLM APIs in production across Gemini, Mistral and Groq"],
  [/\bobservability|tracing|monitoring|metering\b/i, "Built LLM observability and token metering from scratch; prompt caching cut cost ~90%"],
  [/\bpython|fastapi\b/i, "Python and FastAPI in production services"],
  [/\btypescript|javascript|node\b/i, "Node.js and TypeScript across a multi-tenant SaaS platform"],
  [/\breact|frontend|front-end|next\b/i, "React UIs shipped to 1,000+ users, including a reusable editor used by 7 products"],
  [/\bpostgres|sql|mongo|mysql|database\b/i, "PostgreSQL, MongoDB and a Text-to-SQL engine at 75% accuracy over 200+ schemas"],
  [/\bgcp|cloud run|docker|ci\/cd|deploy\b/i, "GCP, Cloud Run, Docker and CI/CD ownership end to end"],
  [/\bmulti-tenant|saas|tenant|oauth|sso|jwt|auth\b/i, "Multi-tenant isolation with SSO/OAuth for 10 enterprise tenants"],
  [/\bwebsocket|real-?time|event|kafka|cdc|stream\b/i, "Event-driven CDC pipeline delivering real-time alerts within 20s"],
  [/\bvision|yolo|pytorch|computer vision|ml\b/i, "Trained and deployed a YOLOv11 layout model at 80% mAP@0.5"],
  [/\blead|mentor|architec|design\b/i, "Lead engineer for the team's AI systems; mentors 5 engineers and drives RFCs"],
];

function strengthFor(skill: string): string {
  for (const [pattern, line] of EVIDENCE) {
    if (pattern.test(skill)) return line;
  }
  const label = skill.charAt(0).toUpperCase() + skill.slice(1);
  return `${label} — named in the profile's skills, used in shipped work`;
}

function titleCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * The band the read lands on.
 *
 * Starts from the deterministic band and moves one step down when the JD names
 * several stacks the profile cannot show, which is the disagreement the real
 * model produces most often: the keyword overlap looks good and the must-haves
 * are the reason it does not.
 */
function bandFor(row: MatchRow): MatchBand {
  const index = Math.max(0, BANDS.indexOf(row.band));
  const missing = row.missing_stacks.length;
  const drop = missing >= 4 ? 2 : missing >= 2 ? 1 : 0;
  const nudge = !row.confident && index > 0 ? 1 : 0;
  return BANDS[Math.max(0, index - drop - nudge)] ?? "moderate";
}

/** One composed verdict for a row, in the shape a real read stores. */
export function synthesizeVerdict(row: MatchRow): LlmFitVerdict {
  const band = bandFor(row);
  const title = row.job.title;
  const company = row.job.company;

  const strengths: string[] = [];
  for (const skill of row.matched_skills) {
    const line = strengthFor(skill);
    if (!strengths.includes(line)) strengths.push(line);
    if (strengths.length === 6) break;
  }
  if (strengths.length === 0) {
    strengths.push("6 years in production software, the last 2+ on AI/LLM systems");
  }
  if (row.years_required !== null && row.years_required <= 6 && strengths.length < 6) {
    strengths.push(`${row.years_required}+ years asked, 6 years shipped`);
  }

  // The real prompt splits gaps by whether the posting called them required.
  // Nothing in the snapshot records that split, so the first two missing stacks
  // are treated as must-haves and the rest as preferences — which is how the
  // matcher orders them anyway (most-named first).
  const mustHave = row.missing_stacks.slice(0, 3).map(titleCase);
  const niceToHave = row.missing_stacks.slice(3, 7).map(titleCase);

  const reasons: string[] = [];
  if (row.matched_skills.length > 0) {
    reasons.push(
      `The posting's core stack overlaps the profile on ${row.matched_skills
        .slice(0, 4)
        .join(", ")} — all of it production work rather than side projects.`,
    );
  }
  if (mustHave.length > 0) {
    reasons.push(
      `${mustHave.join(", ")} ${mustHave.length === 1 ? "is" : "are"} named in the requirements and the profile does not evidence ${
        mustHave.length === 1 ? "it" : "them"
      }.`,
    );
  } else {
    reasons.push("Nothing in the requirements is missing from the profile outright.");
  }
  if (row.years_required !== null) {
    reasons.push(
      row.years_required <= 6
        ? `Asks for ${row.years_required}+ years; the profile shows 6, so the bar is met.`
        : `Asks for ${row.years_required}+ years against 6 shipped — above the band, but adjacent.`,
    );
  }
  reasons.push(
    `Read as a ${band} fit for ${title} at ${company} on the evidence above.`,
  );

  return {
    fit_band: band,
    fit_reasons: reasons,
    strengths,
    must_have_gaps: mustHave,
    nice_to_have_gaps: niceToHave,
    blocked: row.blockers.length > 0,
    model: DEMO_MODEL,
  };
}

/**
 * The rows a pass would read: shortlisted, not already read, best score first.
 *
 * The same ordering the real runner uses, so the count the confirm dialog
 * quoted is the count that moves.
 */
export function deepReadTargets(rows: MatchRow[], limit: number, read: Set<number>): MatchRow[] {
  return rows
    .filter((row) => row.shortlisted && !row.llm_read && !read.has(row.job.id))
    .sort((a, b) => b.score - a.score)
    .slice(0, Math.max(0, limit));
}

/** Tokens a row "cost", near enough the ~1.8k a real qwen screen books. */
export function verdictTokens(jobId: number): number {
  return 1500 + (seed(jobId) % 900);
}
