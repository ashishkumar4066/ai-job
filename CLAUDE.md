# CLAUDE.md — Personal Job Aggregator & Auto-Apply

## How to use this file

This is the project spec **and** the build plan. It is loaded as persistent project context, so everything above the "Phase 1" heading applies to every session.

Build in **phases, in order**. When I say `Implement Phase N`, treat that phase's section as the task. **Do not start a phase until the previous phase's Acceptance Criteria pass** — each layer must be solid before the next builds on it.

### Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Aggregator (adapters, ingest, alerts, API) | ✅ Done |
| 2A | Dashboard (browse / filter / inspect) | ✅ Done |
| 2B | Validation layer (deterministic + LLM) | ⬜ Not started |
| 3 | Autofill (review-before-submit) | ⬜ Not started |

## Overview

A single-user, self-hosted tool that:

1. **Aggregates** live, valid job postings from a configurable set of companies across multiple ATS platforms.
2. **Presents** them in a filterable dashboard with an automated validity check.
3. **Assists applying** via review-before-submit autofill.

Optimize for **correctness and freshness over scale**. This runs for one person against ~50 companies, not at web scale.

## Tech Stack (defaults — keep unless I say otherwise)

- **Backend:** Python 3.12, FastAPI, async (`httpx`, `asyncio`)
- **DB:** **SQLite** (`sqlite+aiosqlite`) via SQLAlchemy 2.0 + Alembic migrations.
  Chosen deliberately over Postgres for single-user scale. Models stay
  dialect-neutral (`JSON` not `JSONB`, no `ARRAY`, a `UtcDateTime` decorator),
  so moving to Postgres is a `DATABASE_URL` change plus `asyncpg`.
- **Data modeling / validation:** Pydantic v2
- **JS-site fallback + autofill:** Playwright (Python)
- **LLM tasks (validation, drafting):** Anthropic API
- **Frontend:** React + Vite + TypeScript, Tailwind, TanStack Query
- **Scheduling:** APScheduler (local) → Cloud Scheduler + Cloud Run (prod)
- **Alerts:** Telegram bot
- **Packaging:** Docker + docker-compose (app + postgres); deploy target GCP Cloud Run
- **Config:** `companies.yaml` for the company list; `.env` for secrets (never commit)

## Architecture principles (apply to every phase)

1. **ATS-first.** Use official public JSON board APIs. HTML scraping / Playwright is a _fallback only_ for sites with no API.
2. **Adapter pattern.** One adapter per ATS behind a shared interface. Adding a company whose ATS already has an adapter = a config entry, not new code.
3. **One normalized schema** for all sources.
4. **Identity = ATS-native stable job id.** Dedupe and track on `source_key = "{ats}:{company}:{job_id}"`.
5. **Closure by disappearance.** If a previously-seen job is absent from a source's latest _successful_ fetch, mark it `closed`. Never hard-delete.
6. **Idempotent ingestion.** Re-running a fetch must produce zero duplicates and zero false "new job" events.
7. **Isolate failures.** One adapter/company erroring must not abort the run — log and continue.
8. **Verify before hardcoding.** For each ATS, hit the live endpoint once and inspect the real JSON before writing field mappings — these APIs are unversioned and can drift.

## Conventions

- Full type hints; Pydantic models for all external/API data.
- Every adapter ships with a **fixture-based test** (recorded JSON response). CI must never hit live endpoints.
- Structured logging with per-source counts: `fetched / new / closed / errored`.
- No secrets in code or git.

---

## PHASE 1 — Aggregator

**Goal:** a backend that fetches current postings from all configured sources, normalizes and stores them, **filters them for my eligibility (India-based, USD pay)**, detects new eligible jobs, and pushes Telegram alerts. This is the foundation — get it correct before any UI.

**Build:**

- `adapters/base.py` — `BaseAdapter` (abstract). One interface, two adapter families behind it:
  ```python
  class BaseAdapter(ABC):
      source: str  # "greenhouse" | "lever" | "ashby" | "himalayas" | "remotive"
      async def fetch(self, cfg: SourceConfig) -> list[RawJob]: ...
      def normalize(self, raw: RawJob, cfg: SourceConfig) -> JobPosting: ...
  ```
  `SourceConfig` generalizes the old `CompanyConfig`: a curated ATS entry carries `{ company, ats, token_or_slug }`; an aggregator entry carries the board slug + its query params. Ingest, dedupe, closure and alerting must not branch on which family a job came from.

  `source_key` stays `"{source}:{company}:{job_id}"` — for aggregator boards, `source` is the board slug (`himalayas`, `remotive`) and `company` is the employer name from the feed.

### A. Aggregator-board adapters (primary — broad discovery, eligibility-tagged)

- `HimalayasAdapter` → `GET https://himalayas.app/jobs/api/search` (free, no key).
  Params: `q`, `country`, `worldwide`, `seniority`, `employment_type`, `company`, `timezone`, `sort`, `page`.
  **Primary source**, because each job carries candidate-location eligibility and currency directly: country restrictions, timezone restrictions, a worldwide flag, and structured salary (`min`, `max`, `currency`).
  Rate-limited → **exponential backoff on HTTP 429**.
  (Browse feed `GET https://himalayas.app/jobs/api` returns max 20/page; page it with `offset`.)
- `RemotiveAdapter` → `GET https://remotive.com/api/remote-jobs?category=software-dev` (free, no key). Secondary feed.
  Constraints, non-negotiable: listings are **delayed 24h**; we **must persist and display Remotive's own job URL** (it becomes `apply_url` for Remotive-sourced rows) and **attribute Remotive as the source** in the UI; **do not repost Remotive jobs to third parties**.
- `WellfoundAdapter` → Wellfound (ex-AngelList) via **Firecrawl** (`FIRECRAWL_API_KEY`). The one source with **no public API** — `api.angel.co` is gone, and plain HTTP gets a bot challenge — so this is the principle-1 fallback. Driven by `queries` that build robots-allowed URL paths (`/role/r/{role}`, `/role/l/{role}/{loc}`, `/location/{loc}`); `/search` is disallowed and never used.
  **Two stages, and the second is not optional.** Stage 1 reads the `__NEXT_DATA__` Apollo cache off a listing page (~37 jobs, 1 credit). Stage 2 reads schema.org JSON-LD off a detail page for candidates only, because Wellfound renders an *unstated* candidate location as "Everywhere": verified job 4627451 claimed Everywhere while its own text said "fully remotely within the United States". Trusting stage 1 alone pushes US-only roles through the India filter.
  Compound location names (`"Mumbai, Maharashtra"`) must go through `split_location_text` before `resolve_eligibility`, or India eligibility is silently dropped.

### B. Curated ATS adapters (high-signal supplement — pre-vetted employers)

- Keep `GreenhouseAdapter`, `LeverAdapter`, `AshbyAdapter`, now driven by a curated `companies.yaml` of **India-friendly, USD-paying, remote-first** employers. Pre-vetting is the point: these companies are known-good, so eligibility is unambiguous rather than inferred.
  - `GreenhouseAdapter` → `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
  - `LeverAdapter` → `GET https://api.lever.co/v0/postings/{company}?mode=json`
  - `AshbyAdapter` → Ashby public job-board posting API (**verify exact endpoint/shape live**)
  - Seed list: `deel`, `gitlab`, `zapier`, `automattic`, `turing`.
- `PlaywrightAdapter` — interface + stub only for now (custom sites, later).

**Verify each live endpoint's real JSON shape before writing field mappings** (principle 8) — including the Himalayas param names above, which are unversioned like everything else here.

### Schema

- `JobPosting` model + table:
  `id`, `source_key`, `ats`, `company`, `title`, `locations[]`, `remote`, `department`, `apply_url` (canonical ATS URL, or Remotive's job URL for Remotive rows), `description_html`, `description_text`, `posted_at`, `updated_at`, `first_seen_at`, `last_seen_at`, `status` (`open|closed`), `raw_json`.
- **New fields:** `location_eligibility[]` (ISO country codes and/or `"worldwide"`), `timezone_restrictions`, `salary_min`, `salary_max`, `salary_currency`, `is_us_employer` (derived), `eligibility_pass` (bool), `eligibility_reasons[]`.
  - `is_us_employer` is derived, not fetched: curated `companies.yaml` entries may set it explicitly; aggregator rows derive it from the feed's company-location/HQ field when present, else leave it unknown (`None` — distinct from `false`, because the pay rule below keys off "unknown").
  - Alembic migration for the added columns.

### Ingestion

- `ingest.py` — for each entry in `companies.yaml` / the aggregator source config: run its adapter → **eligibility filter (below)** → upsert by `source_key` (refresh `last_seen_at` + changed fields), insert new rows with `first_seen_at=now`, then mark any previously-open job for that source **not seen this run** as `closed`.
- **New ingestion stage — eligibility filter.** Runs **after `normalize`, before a job is marked active.** Retain a job as **eligible** only if ALL FOUR hold:
  1. **Location:** `location_eligibility` includes `IN` **OR** `worldwide` (treat `"global"` / `"anywhere"` as worldwide). This is who the employer will *hire*, not where its office is — a London or Singapore company that takes India-based remote candidates passes.
  2. **Remote:** off by default (`require_remote: false`) — rule 1 already implies remote-from-India, and `detect_remote` under-reports badly (GitLab, an all-remote employer, returns `remote=false` on 42 rows).
  3. **Pay:** an *unstated* salary **passes** and is surfaced in the UI as "not stated" (~84% of postings state nothing). A *stated* salary must annualize to at least `min_annual_salary_inr` (INR 20L). Any currency is acceptable above the floor; `fx_to_inr` is a static table, and `salary_period_bands` infers hourly/monthly/annual from magnitude in the **native** currency.
  4. **Role + seniority** (`app/roles.py`): the title must name a wanted engineering family, must not be junior or leadership or non-engineering, and any *stated* years-of-experience requirement must fall in `role.min_years .. role.max_years` (2-8). An *unstated* requirement passes, exactly as an unstated salary does.

  Jobs that fail are **still stored**, flagged `eligibility_pass = false` with populated `eligibility_reasons[]`. **Nothing is silently dropped** — the rules stay auditable and tunable against real data.
- Filter config in `config/filters.yaml` — see the file for the full annotated block.

### Rule 4 — role and seniority (`app/roles.py`)

Rules 1-3 read structured fields a board hands us. Rule 4 has only the title and
the JD prose, so it is the only rule that reads English and the only one whose
mistakes are worth auditing. Two decisions in it are non-obvious:

- **`years_required` takes the MAXIMUM stated figure, not the minimum.** A JD's
  several year counts are a headline requirement plus narrower sub-clauses, not
  alternatives. Palantir's "Senior Software Engineer - Observability" states
  `5+ years professional software development`, then `2+ years ... system
  design`, then `1+ years ... as a mentor`. Reading the minimum files a genuine
  senior role as junior — the first cut of this module did exactly that.
- **Every non-engineering pattern anchors on a role HEAD**, never a lone
  qualifier. Matching a bare `product` rejects "Software Engineer, AI Product";
  a bare `partner` rejects "Senior Staff Software Engineer - App and Partner
  Ecosystem". A qualifier names the team a role serves, not the role.
- A wanted title does **not** rescue an out-of-range bar: Databricks asks 12-15
  years for Staff and Stripe asks 10, so those are dropped even though `staff`
  is a wanted family (116 rows). Staff roles stating ≤8 years, or stating
  nothing, still pass.
- `associate` is deliberately **not** a junior marker — "Associate Staff
  Engineer" is Nagarro's real mid-level IC title (21 live rows).
- **The API-level half lives in `companies.yaml`.** Himalayas is swept with
  `seniority=Mid-level,Senior`, so entry-level rows are never fetched. Verified
  live: the param takes a comma-separated list and its vocabulary is exactly
  `Entry-level | Mid-level | Senior | Manager | Director | Executive` —
  anything else 400s (`Lead` does).
- **New-job detection:** rows whose `first_seen_at == this run`.
- `notify/telegram.py` — send each new job (title, company, location, apply_url). **Only `eligibility_pass = true` jobs trigger alerts.**
- `scheduler.py` — APScheduler job running ingest every N minutes (configurable).
- FastAPI: `GET /jobs` (filters: company, ats, remote, q, status), `GET /jobs/{id}`, `POST /ingest/run` (manual trigger).
- `companies.yaml` — curated ATS list of `{ company, ats, token_or_slug }`.
- Docker + compose (app + postgres). Alembic migration for the schema.

**Acceptance criteria:**

- Run `POST /ingest/run` twice back-to-back → the second run emits **zero** new-job events and creates **zero** duplicate rows.
- Delete a job from a fixture and re-run → that job flips to `closed`, not deleted.
- Force one adapter to throw → the other adapters still complete and persist.
- A genuinely new fixture job appears in `GET /jobs` and triggers **exactly one** Telegram message.
- All live source endpoints verified: a short script (`python -m scripts.verify_endpoints`) prints fetched count per source and it matches that source's public listing page.
- Himalayas and Remotive adapters each return normalized jobs from a **recorded fixture**, and the shared interface handles both with **no downstream special-casing**.
- A worldwide-eligible USD job **passes** the filter; a US-candidates-only remote job is flagged `eligibility_pass = false` with a **location** reason.
- **Only** `eligibility_pass = true` jobs generate alerts.
- Remotive jobs retain and display Remotive's canonical URL and attribution.

---

## PHASE 2A — Dashboard ✅

**Goal:** a React dashboard to browse, filter and inspect the aggregated jobs. No validation logic — that is 2B.

**Built:**

- React 19 + Vite + TS + Tailwind v4 + TanStack Query against the Phase 1 API. Lives in `frontend/`.
- **Virtualized** table via `@tanstack/react-virtual` — ~18 rows in the DOM regardless of result-set size. Columns: role/company, location + remote, department, posted, apply. Infinite paging (100/page) as you scroll.
- **Filters**, composable and fully round-tripped through the URL: keyword (+ title-only scope), company, ATS, department, remote, status, posted-within, "new since last visit". Only non-defaults are serialized, so links stay short.
- Sort: recently-found (default), recently-posted, title, company. Ties on `first_seen_at` fall back to `posted_at` — a bulk first ingest gives every row the same discovery stamp, which otherwise collapses the default view to one company.
- Job detail drawer: full JD, metadata grid, apply on the canonical ATS URL, `j`/`k` to move between jobs. Drawer state is in the URL (`?job=`), so back closes it.
- "New since last visit" via a `localStorage` timestamp, frozen for the session so rows don't stop being new while you read them.
- Glassmorphism design system, dark/light themes (no FOUC), keyboard shortcuts (`/` `j` `k` `r` `t` `esc`), skeletons, empty states, responsive to 430px.
- **Fetch-on-load, once a day.** Opening the dashboard sweeps every board before
  anything renders: `/jobs` and `/meta/facets` are `enabled`-gated on the sweep,
  so a stale snapshot is never requested, let alone shown. A sync screen lists
  each board as it goes (queued → fetching → done/failed/skipped) — a bare
  spinner for 90s reads as broken, a board list does not.
  - The window is a **rolling 24h** (`REFRESH_WINDOW_HOURS`), not a calendar
    day. A calendar boundary called a 23:00 sweep stale at 00:05; making it the
    browser's local midnight fixed the timezone half but not that. Rolling also
    means the browser contributes nothing to the decision — no clock, no
    timezone, no `fresh_since` on the wire.
  - Freshness is decided server-side from `max(ingest_runs.finished_at)`, so it
    survives a cleared browser and counts scheduler runs too. A run killed
    mid-sweep leaves `finished_at` NULL and correctly does **not** count.
    Second visit inside the window: no board is contacted, dashboard renders in
    ~0.3s. Only the refresh button / `r` (`force=true`) sweeps early.
  - **The scheduler shares the same gate.** Its interval is a *check* cadence,
    not a sweep cadence — it wakes hourly, and runs only if the window has
    lapsed. Before this it swept every 60 minutes outright, so the boards were
    hit all day regardless of the dashboard's rule, and a reload landing during
    a tick was held behind the whole sweep.
  - A failed first sweep keeps the gate shut and offers *retry* or *show stored
    jobs* — the user chooses, rather than getting an empty table under a banner.

**API added for the dashboard** (`backend/app/api.py`):

- `GET /meta/facets` — filter options with counts + headline stats, so dropdowns only ever offer values that have jobs. Takes `eligibility_pass` for the same reason `/jobs` does: the dashboard hides ineligible rows by default, and a facet list built without the gate offers companies whose every posting is filtered out, so selecting one yields an empty table. `totals.eligible` / `totals.ineligible` are deliberately computed *without* the gate — inside it they would report `ineligible = 0` and describe the query rather than the board.
- **The dashboard's default view is gated on `eligibility_pass = true`** (`matchesPrefs` in the filter state, the "My roles / All roles" toggle). This is the one filter that hides rows by default. Ingest still stores every posting and flags the misses, so "All roles" is the way back to the full board and the drawer shows `eligibility_reasons` for any row. Serialized as `prefs=0` only when switched OFF, so default links stay short.
- `q_scope=all|title` on `/jobs` — full-JD search is inherently noisy (`q=engineer` matched all 400 Anthropic jobs via boilerplate); title scope is the precise option.
- `first_seen_after` on `/jobs` — backs the "new since last visit" filter.
- `department` is now an exact, repeatable filter (was a single substring match).
- `POST /ingest/refresh` — the page-load entry point. Skips the sweep when the
  last finished run is inside the window (`fresh_since` overrides the default
  cutoff); otherwise starts the run **in the background** and returns at once.
  Holding an HTTP request open for a real sweep (Wellfound alone can take a
  minute) yields a proxy timeout, not an answer. Freshness is checked **before**
  the in-progress check, so a load that lands during a scheduler tick paints
  from storage instead of waiting; only a caller with no fresh data attaches to
  the running sweep, and never queues a second one. `force=true` bypasses the
  window (the manual refresh button and `r`).
- `GET /ingest/status` — poll target with live per-source progress. Backed by
  `app/ingest_state.py`, which also owns the lock the scheduler, the manual
  trigger and the background refresh all share.

**Acceptance criteria — all met:**

- ✅ Filters compose correctly and round-trip through the URL.
- ✅ Dashboard renders 3,391 live jobs without visible lag (virtualized, verified in-browser).

---

## PHASE 2B — Validation

**Goal:** a validity layer that flags stale/ghost/duplicate/low-signal postings, surfaced in the Phase 2A dashboard. Also stabilize Phase 1 against real-world data.

**Build — Validation:**

- **Deterministic checks** (run on every job, cheap): still present in latest fetch? age of `posted_at`/`updated_at`? cross-company duplicate (hash of normalized title + location + JD)? missing critical fields?
- **LLM validator** (only for ambiguous jobs; **cached by content hash** so unchanged jobs never re-bill): given JD + metadata via the Anthropic API, classify active vs likely-evergreen/ghost, extract structured fields (seniority, tech stack, comp if stated, visa/sponsorship mention), and flag internal inconsistencies.
  - `job_postings.content_hash` already exists from Phase 1 and is the intended cache key.
- Persist `validity_score`, `validity_reasons[]`, and extracted structured fields on the job; expose via API.

**Build — Dashboard additions (extends 2A):**

- Validity badge column in the table + validity section in the detail drawer.
- `min validity` filter, wired into the existing URL filter state (`useFilters.ts`) and `_apply_filters` in `api.py`, plus a facet entry so counts stay consistent.
- Sanitize `description_html` before rendering — 2A renders ATS markup directly via `dangerouslySetInnerHTML`, which is acceptable for a personal same-origin tool but should be cleaned once this layer lands.

**Stabilization:**

- Fix ingestion edge cases surfaced by real data: multi-location roles (Greenhouse packs several into one `location.name` string, e.g. `"SF, NYC, SEA, CHI"` — not comma-splittable because `"San Francisco, CA"` is one place), missing/renamed fields, pagination if an ATS starts paging, timezone handling on dates.
- Note: Lever exposes **no** update timestamp, so staleness checks must not assume `updated_at` exists.

**Acceptance criteria:**

- A fixture job that is old **and** closed-elsewhere is flagged stale/ghost.
- Validation is idempotent and cached by content hash (no re-billing on unchanged jobs).
- The validity badge and `min validity` filter compose with the existing 2A filters and round-trip through the URL.

---

## PHASE 3 — Autofill (review-before-submit)

**Goal:** from a selected job + my stored profile/resume, open the application page, auto-fill standard fields, draft answers to custom questions, and present everything for my review. **The system never submits autonomously — a human confirm is always required.**

**Build:**

- **Profile store:** `profile.yaml` (name, contact, links, work authorization, standard reusable answers) + resume file(s). Pydantic model.
- **Per-ATS fillers** via Playwright — `GreenhouseFiller`, `LeverFiller`, `AshbyFiller`: map profile fields → each ATS's known application-form selectors (resume upload, name, email, phone, LinkedIn, etc.). Keep this isolated per ATS, same pattern as the Phase 1 adapters.
- **Custom-question handler:** detect non-standard/free-text questions on the form; draft answers with the Anthropic API using profile + JD; label them `DRAFT — review`.
- **Review step:** headed Playwright session (or a review page) showing the filled form + drafted answers. I edit, then explicitly click submit. **No auto-submit path exists in the code.**
- **Anti-bot:** on CAPTCHA / Cloudflare / an unmapped required field, **pause and hand control to me** rather than trying to bypass.
- **Audit log:** per application, record what was filled and the final answers.

**Acceptance criteria:**

- Standard fields fill correctly on one Greenhouse and one Lever test application.
- Custom questions are detected and drafted, **never** auto-answered-and-submitted.
- On CAPTCHA or an unmapped required field, the flow pauses and surfaces to me.
- No submission occurs without an explicit human confirmation action.

**Guardrails:**

- Personal-scale only. Many career sites' ToS restrict automated submission; human-in-the-loop by design keeps this a copilot, not a bot farm.
