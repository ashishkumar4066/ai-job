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

**Goal:** a backend that fetches current postings from all configured companies, normalizes and stores them, detects new jobs, and pushes Telegram alerts. This is the foundation — get it correct before any UI.

**Build:**

- `adapters/base.py` — `BaseAdapter` (abstract). Interface:
  ```python
  class BaseAdapter(ABC):
      ats: str
      async def fetch(self, company: CompanyConfig) -> list[RawJob]: ...
      def normalize(self, raw: RawJob, company: CompanyConfig) -> JobPosting: ...
  ```
- Concrete adapters (start with these three — most API-friendly):
  - `GreenhouseAdapter` → `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
  - `LeverAdapter` → `GET https://api.lever.co/v0/postings/{company}?mode=json`
  - `AshbyAdapter` → Ashby public job-board posting API (**verify exact endpoint/shape live**)
  - `PlaywrightAdapter` — interface + stub only for now (custom sites, later)
- `JobPosting` model + table:
  `id`, `source_key`, `ats`, `company`, `title`, `locations[]`, `remote`, `department`, `apply_url` (canonical ATS URL), `description_html`, `description_text`, `posted_at`, `updated_at`, `first_seen_at`, `last_seen_at`, `status` (`open|closed`), `raw_json`.
- `ingest.py` — for each company in `companies.yaml`: run its adapter → upsert by `source_key` (refresh `last_seen_at` + changed fields), insert new rows with `first_seen_at=now`, then mark any previously-open job for that source **not seen this run** as `closed`.
- **New-job detection:** rows whose `first_seen_at == this run`.
- `notify/telegram.py` — send each new job (title, company, location, apply_url).
- `scheduler.py` — APScheduler job running ingest every N minutes (configurable).
- FastAPI: `GET /jobs` (filters: company, ats, remote, q, status), `GET /jobs/{id}`, `POST /ingest/run` (manual trigger).
- `companies.yaml` — list of `{ company, ats, token_or_slug }`.
- Docker + compose (app + postgres). Alembic migration for the schema.

**Acceptance criteria:**

- Run `POST /ingest/run` twice back-to-back → the second run emits **zero** new-job events and creates **zero** duplicate rows.
- Delete a job from a fixture and re-run → that job flips to `closed`, not deleted.
- Force one adapter to throw → the other adapters still complete and persist.
- A genuinely new fixture job appears in `GET /jobs` and triggers **exactly one** Telegram message.
- All three live ATS endpoints verified: a short script prints fetched count per company and it matches that company's public career page.

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

**API added for the dashboard** (`backend/app/api.py`):

- `GET /meta/facets` — filter options with counts + headline stats, so dropdowns only ever offer values that have jobs.
- `q_scope=all|title` on `/jobs` — full-JD search is inherently noisy (`q=engineer` matched all 400 Anthropic jobs via boilerplate); title scope is the precise option.
- `first_seen_after` on `/jobs` — backs the "new since last visit" filter.
- `department` is now an exact, repeatable filter (was a single substring match).

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
