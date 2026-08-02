/** Small display helpers — no date library needed. */

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600_000],
  ["month", 30 * 24 * 3600_000],
  ["week", 7 * 24 * 3600_000],
  ["day", 24 * 3600_000],
  ["hour", 3600_000],
  ["minute", 60_000],
];

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";

  const diff = then - Date.now();
  const magnitude = Math.abs(diff);
  if (magnitude < 60_000) return "just now";

  for (const [unit, ms] of UNITS) {
    if (magnitude >= ms) return RELATIVE.format(Math.round(diff / ms), unit);
  }
  return "just now";
}

export function absoluteDate(iso: string | null | undefined): string {
  if (!iso) return "Unknown";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function daysSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  return Math.floor((Date.now() - then) / 86_400_000);
}

/** ATS location strings vary wildly; keep the row readable. */
export function formatLocations(locations: string[], remote: boolean): string {
  if (locations.length === 0) return remote ? "Remote" : "Not specified";
  if (locations.length === 1) return locations[0]!;
  return `${locations[0]} +${locations.length - 1}`;
}

export function compactNumber(value: number): string {
  return new Intl.NumberFormat("en", { notation: "compact" }).format(value);
}

export function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/** Brand hue (see --brand-hue in index.css) and how far chips may stray from it. */
const BRAND_HUE = 264;
const HUE_SPREAD = 70;

/**
 * Deterministic chip colour per company, constrained to a band around the brand
 * hue so the list stays one palette instead of a rainbow.
 */
export function companyHue(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) % 997;
  return BRAND_HUE - HUE_SPREAD / 2 + (hash % HUE_SPREAD);
}

export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  // One letter for single-word names — "Notion" as "NO" reads like a word.
  if (words.length === 1) return words[0]![0]!.toUpperCase();
  return (words[0]![0]! + words[1]![0]!).toUpperCase();
}
