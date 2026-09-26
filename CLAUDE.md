# CLAUDE.md — Personal Job Aggregator & Auto-Apply

## How to use this file

This is the project spec **and** the build plan. It is loaded as persistent project context, so everything above the "Phase 1" heading applies to every session.

Build in **phases, in order**. When I say `Implement Phase N`, treat that phase's section as the task. **Do not start a phase until the previous phase's Acceptance Criteria pass** — each layer must be solid before the next builds on it.

### Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Aggregator (adapters, ingest, alerts, API) | ✅ Done |
| 2A | Dashboard (browse / filter / inspect) | ✅ Done |
| 2B | Validation layer (deterministic + LLM) | ✅ Done (deep read has covered 483 rows so far; it is resumable by design, so "all rows" is a budget question, not a build one) |
| 2C | Matches (profile fit + tailored résumé/cover letter) | ✅ Done — Stages 1-3 complete: profile upload/edit from the dashboard, fit scoring, and tailored résumé + cover letter with chat and a diff against base |
| 3 | Autofill (review-before-submit) | 🟡 Started ahead of its gate — `app/autofill/lever.py` + `scripts/autofill.py` work against a live Lever form; no Greenhouse/Ashby fillers, no API route, no tests |

### Deviation from the original 2B/2C split

2C Stage 1 (`profile.yaml`) and the deterministic half of Stage 2 landed **inside
2B**, ahead of the LLM work, because measuring the board changed the plan:

- **The LLM provider is Groq, not Anthropic** (`GROQ_API_KEY`). Verified live:
  the chat models are `openai/gpt-oss-120b`, `gpt-oss-20b`,
  `qwen/qwen3.8-27b` and `qwen/qwen3.6-27b` — there is no Llama 3.3 70B on
  Groq any more. Structured output needs **a `description` on every field plus
  rubric anchors**: without them `gpt-oss-120b` answered a 0-100 scale on 0-10
  and returned `fit_score: 6` for a good match, stably, at temperature 0.
  Repeat runs move the number ±7.5 while the extracted evidence
  (`gaps: ["AWS experience"]`) was identical 5/5 — so the UI shows **bands**,
  and treats the reasons as the trustworthy output.
- **The provider is switchable (2026-09-13):** `LLM_PROVIDER=groq|mistral|cerebras`,
  each with its own key, model, base URL and rate-limit block in `.env`
  (resolved in `Settings.llm`). Cerebras: `qwen-3.8-27b` by default, limited
  six ways (requests and uncached tokens, each per minute/hour/day). Defaults:
  300 RPM / 150K TPM (Developer tier on its model page), 27K RPH / 648K RPD /
  9M TPH / 216M TPD (read live off the key). Its undocumented `x-ratelimit-*-{minute,hour,day}`
  headers were verified live and are read on every call. Its strict mode rejects `maxItems`, so
  that keyword is stripped per provider. It also defaults qwen to `reasoning_effort=high`,
  so `low` is sent explicitly, along with `max_completion_tokens`, which it
  books against its limits up front. The measurements below were all taken on Groq.
- **Correction (2026-09-08): strict `json_schema` does NOT work on all three.**
  Measured while building the pass. `gpt-oss-20b` rejects the screen schema
  outright (HTTP 400). `gpt-oss-120b` *intermittently* generates JSON that
  violates the schema it was given — 3 failures in 4 probe rows, then the same
  row succeeded on retry — so it cannot be retried away cheaply. Only
  `qwen/qwen3.8-27b` held the schema on every attempt, and it is also the
  cheapest and the better reader: `gpt-oss-120b` scored "Software Engineer II,
  Enterprise AI Enablement" as an *excellent* match with no gaps, where qwen
  read it as mid-level and named the missing stacks. **`qwen/qwen3.8-27b` is
  the default** (`GROQ_MODEL`).
- **Correction (2026-09-13): the binding constraint is 200,000 tokens/DAY,
  not 8,000 tokens/minute.** Groq's free plan limits each model four ways
  (https://console.groq.com/docs/rate-limits): 30 RPM, 1K RPD, 8K TPM, 200K
  TPD. At ~1,800 tokens a row TPD ends a pass at ~110 rows — a 424-row pass
  is a multi-day job, not "~2 hours". The first live pass ran into it at
  ~207k tokens and failed every row after that, because the 429 said "try
  again in 7m32s" and the parser only read "Ns". Headers report only TPM
  (`*-tokens`) and RPD (`*-requests`); RPM and TPD are invisible, so
  `app/ratelimit.py` counts all four and seeds TPD from the persisted
  `llm_usage` ledger (migration `0006`). A daily wait stops the pass cleanly
  (resumable); minute windows are slept through; 429s without a named wait,
  5xx and network errors use exponential backoff with jitter. The per-job
  cost still depends on the model — on a reasoning model most of the
  completion is reasoning: `gpt-oss-120b` costs ~3,100 tokens at
  `reasoning_effort=low` and ~3,950 at `medium` **for an identical verdict**,
  qwen ~1,800. One LLM call per job is not affordable across the whole board,
  which is what forced the deterministic pass to come first.
- **Validation and matching share one call**, with **two cache keys** —
  `content_hash` for the validity half, `content_hash + profile_version` for
  the fit half. One key would make a résumé edit re-bill validation for jobs
  whose description never changed.

**A note on the duplicate check 2B asks for:** measured against the live board
it finds almost nothing. Exact `content_hash` collisions across open rows: 0.
On the 917 eligible rows a (company, title, JD-hash) key collapses **2**. Same
JD across different companies: **3**. Databricks' "Solutions Architect" ×23
looks like duplication but carries 23 distinct JD hashes. It is cheap, so it
ships — but it is a footnote, not a feature.

**And `updated_at` is unusable for staleness.** The spec says Lever has no
update timestamp; in fact **5 of 6 sources** have none (ashby, himalayas,
lever, remotive, wellfound — 3,266 of 5,491 open rows). Only Greenhouse
populates it. Staleness keys off `posted_at` (100% coverage) plus our own
`first_seen_at` / `last_seen_at`.

## Overview

A single-user, self-hosted tool that:

1. **Aggregates** live, valid job postings from a configurable set of companies across multiple ATS platforms.
2. **Presents** them in a filterable dashboard with an automated validity check.
3. **Ranks** them against my stored profile and drafts tailored application documents.
4. **Assists applying** via review-before-submit autofill.

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
  **An unrecognized role slug fails OPEN, not closed** — verify every new one before adding it. `/role/r/founding-engineer` answers 200 and then redirects to `/remote`, the entire unfiltered board (11,695 jobs, 205 pages), so a typo'd or non-existent slug does not return nothing, it silently spends a Firecrawl credit per page scraping the whole site. The valid slugs are enumerated in the footer of any listing page.
  **Two stages, and the second is not optional.** Stage 1 reads the `__NEXT_DATA__` Apollo cache off a listing page (~37 jobs, 1 credit). Stage 2 reads schema.org JSON-LD off a detail page for candidates only, because Wellfound renders an *unstated* candidate location as "Everywhere": verified job 4627451 claimed Everywhere while its own text said "fully remotely within the United States". Trusting stage 1 alone pushes US-only roles through the India filter.
  Compound location names (`"Mumbai, Maharashtra"`) must go through `split_location_text` before `resolve_eligibility`, or India eligibility is silently dropped.
- `JobicyAdapter` → `GET https://jobicy.com/api/v2/remote-jobs?count=200&industry=engineering&geo=apac` (free, no key; added 2026-09-17). Terms: credit Jobicy, keep its job URL as `apply_url`, poll **at most once an hour** (`min_fetch_interval_minutes: 60`, one query). `geo=apac` is a superset of `geo=anywhere`; there is no `india` geo, and a bad slug answers 200 with an `error`. "Anywhere" stays `worldwide` **without** the Wellfound prose check: its one hit on 47 rows was a pay sentence ("compensation for US based candidates"), so JD restrictions are left to `blockers.py`. Live: 72 fetched, 19 eligible.
- `TheMuseAdapter` → `GET https://www.themuse.com/api/public/jobs` (free; `THEMUSE_API_KEY` required by the terms; 500 calls/h unkeyed, 12,000 keyed as measured, though the docs say 3,600; added 2026-09-17). Three silent traps: **`page` is capped at 99** (2,000 rows; the query totals ~3,370), results are **not in date order**, and an **unknown `location` fails open** to the remote set (`Bengaluru, India` does; `Bangalore, India` is the valid spelling). A capped fetch sets `BaseAdapter.fetch_truncated` and ingest **skips the closure sweep**. "Flexible / Remote" is **not** worldwide (sampled remote-only rows were Unum, Visa, GoodRx and "GitLab … EMEA"); eligibility comes from concrete city locations only, and no remote marker means `workplace_type = onsite`. Live: 2,000 fetched, 371 eligible, but only ~127 posted ≤30d and ~10 of those remote. It is mostly large-employer India-office roles, and takes ~2 min a sweep.
- `YcAdapter` → `GET https://www.ycombinator.com/jobs/role/{role}/{location}` (public pages; Work at a Startup itself answers 406 without a login; added 2026-09-19). No API: the page embeds its view model as HTML-escaped JSON in `data-page`, so plain HTTP suffices. **An unknown role or location slug fails open** (`backend-engineer` serves the default Software Engineer list with a 200), so the adapter checks the slugs the page echoes and fails the source on a mismatch. **No pagination**: `?page=` is ignored, and `remote` (~29) and `india` (~27) are whole sets. **Locations are ISO codes**: `"CA / Remote (CA)"` is Canada, so YC parses its own `" / "` segments rather than using the state-first shared resolver. A **bare `"Remote"` is not worldwide** (sampled rows were US-overlap or EU-remote). The detail page (read only for India-eligible rows) adds the JD, the exact `datePosted` and the HQ country. Its JSON-LD is wrong about currency and location (it gave USD/US for a CAD/Canada role), so those come from the listing. `visa` ("US citizen/visa only" on 58 of 83 rows) stays out of the JD text, or `blockers.py` would flag India-remote roles. Live: 56 fetched, 1 eligible. The India page is mostly hardware, leadership and growth roles. **`{mode: companies}` (added 2026-09-19) gets past the caps**: YC's company directory is an Algolia index with `isHiring` and `regions` facets. Its secured key is **read off `/companies` (`window.AlgoliaOpts`) on every run, never stored**. Each hiring company's `/companies/{slug}/jobs` page then gives every role, uncapped, in the same row shape, and names the HQ country (`is_us_employer`) without a detail read. 251 companies matched India/Fully Remote. `YC_COMPANY_BUDGET` (80) caps pages per run, so YC now polls at most every 12h. Live with 10 companies: 232 fetched, 3 eligible. Most are US-remote roles.
- `ArcAdapter` → `GET https://arc.dev/remote-jobs[/{category}]?countries=IN&…` (public Next.js pages, `__NEXT_DATA__`; added 2026-09-19). Each page carries **two lists that read an empty `requiredCountries` differently**: `arcJobs` (Arc's confidential clients, stored as `Arc.dev client`), where empty means worldwide (detail pages say `requiredLocations: ["worldwide"]`), and `externalJobs` (LinkedIn/Indeed re-posts), where it means unknown (CrowdStrike's "(Remote, DEU)" had it empty). **No pagination** (`?page=` is ignored, and the client-side bundle is not served to plain clients), so coverage comes from seven filter URLs, each giving its first 30+30. Every sweep is therefore truncated and closure never runs. Polled at most every 6h, with 3s between requests (robots.txt gives named crawlers 10s). Live: 164 fetched, 15 eligible, ~2 min.
- `CutshortAdapter` → `GET https://cutshort.io/backend-api/webpage/jobs/remote-jobs?page=N` (added 2026-09-19). This is the JSON the site's own listing pages render from. It is not a documented API: Cutshort's "public API" is employer-side only. **Use the JSON, not the HTML page**, whose `?page=` barely pages (pages 3-200 were identical). The JSON is newest first, 50 per page, and past the end it answers 200 with `success: false, errorCode: jobs_not_found`, the normal end marker. A sweep stops at `CUTSHORT_MAX_AGE_DAYS` (30d is ~4 of ~112 pages), so it is always truncated. **USD rows store the INR conversion in `salaryRange.min/max`** ("$2K - $3.5K" carries 95,602/334,608). The displayed native figures are `userMinVanity/userMaxVanity`, and `hideSalary` rows are unstated. Remote rows name no place, so an India board's placeless remote row is read as `IN`. Only `expRange.min` is given to the role rule. Live: 191 fetched, 19 eligible, 11s.
- `HiristAdapter` → `GET https://gladiator.hirist.tech/job/category/?categoryId=N&page=P&size=100` plus `/job/detail?jobcode=` (added 2026-09-19; found in the site bundle, no auth, needs header `version: 2`). **Paced at 10s per request**, following the site's `Crawl-delay: 10`, so a full sweep takes minutes and is polled at most every 12h. **Two stages:** every row is in India, so stage-2 candidates are chosen by the **title** (`workFromHome == 1` or a "Remote" location, *and* `roles.classify(title)` passes), not by location. **Pay is in lakhs** (`30`-`35` = ₹30L-35L) and ~93% hidden. `wfh=1` is ignored by the API. Category order is only roughly newest first, so stale rows are dropped individually and a query stops once a whole page is out of the window. 190/200 titles lead with "Employer - ", which is stripped. Live (1 page per category): 394 fetched, 151 eligible. **Known limit:** `roles.py` matches no family for "Software Development Engineer", "Golang Developer" or "Rust Developer", the commonest Indian titles, so they fail rule 4 and never become stage-2 candidates.
- `BuiltInAdapter` → `GET https://builtin.com/jobs/remote/dev-engineering?country=IND&page=N` + detail JSON-LD (added 2026-09-19). The only HTML-parsed listing: cards are read by Built In's own hooks (`id="job-card-N"`, `data-id=…`, the icon beside each attribute), and an empty first page fails the source. robots.txt disallows `?search=`, so the adapter refuses a `search` query. **Deep pages never run dry**: it stops at the first page adding no new id, and every sweep is truncated. **Countries are alpha-3** (`IND`, `"Pune, Maharashtra, IND"`) and a bare `IN` is ambiguous with Indiana, so the adapter maps alpha-3 itself. **Card pay has no currency** ("4M-4M Annually" on an India role), so it stays in `raw_json` as unstated. **The JSON-LD tag is escaped** (`application/ld&#x2B;json`) inside `@graph`, which the shared extractor misses. Details only for titles passing the role rule. Live: 162 fetched, 71 eligible, ~110s.
- `RemoteOkAdapter` → `GET https://remoteok.com/api?tag={dev,engineer,backend}` (free, no key; added 2026-09-19). Terms (the response's own `legal` element): **link back to the Remote OK URL, followed, and name Remote OK**, or access is suspended. It is `apply_url`, and the dashboard's links carry no `nofollow`. Element 0 is that notice, not a job. **No pagination** (~100 newest per tag), so every sweep is truncated. An unknown tag returns nothing. **Text is mojibake at the source** (`BogotÃ¡`), repaired by a Latin-1 round trip. **A blank `location` is not worldwide** (DomainTools' blank row says "US-based"), and the job page's JSON-LD says "Anywhere" even for US-only rows. A blank location is therefore a worldwide *claim* narrowed by the JD prose. **145 of 237 rows name a bare city the resolver cannot place** ("Dehradun, ", "Posts, "); they fail location, stored flagged. A 10K-750K pay range is a placeholder and reads as unstated. Live: 237 fetched, 10 eligible, 4s.
- `WeWorkRemotelyAdapter` → `GET https://weworkremotely.com/categories/{category}.rss` (public RSS; the JSON API needs a token; added 2026-09-19). **`region` is "Anywhere in the World" on 97% of rows**, Reddit's US-only roles included, so it is trusted only when it narrows. **`country` (a flag-prefixed list) is the real restriction** when present (17 of 69 rows). Otherwise the claim is narrowed by a `Headquarters:` line that says remote, then by prose. Of 52 "Anywhere" rows, 23 were narrowed by HQ, 8 by prose, and 21 stay worldwide (unverified). The all-jobs feed caps each category at 10, but category feeds are uncapped, so **closure runs**. An unknown slug answers 301 with an empty body, which fails the source. Title is `"Company: Role"`. No pay field. Live: 69 fetched, 10 eligible, 5s.
- `RemoteYeahAdapter` → `GET https://remoteyeah.com/remote-{role}-jobs-in-india[/page/N]` + detail JSON-LD (HTML; added 2026-09-19). **An unknown slug fails open** (redirects to the all-roles India page with a 200), so the final URL is checked on every listing request. **`?page=` is ignored**; paging is `/page/N`, and past the end redirects back to the last page. Cards are newest first with an exact `<time datetime>` (Featured cards have none), and a sweep stops at `REMOTEYEAH_MAX_AGE_DAYS`, so it is always truncated. **Location tags are taken as stated**, because the description is RemoteYeah's own bullet summary, not the JD, so prose cannot second-guess them. Details (candidates only, `REMOTEYEAH_DETAIL_BUDGET` 120) give `applicantLocationRequirements`, `baseSalary` with a real currency, and `monthsOfExperience`. Card pay has no reliable currency and stays in `raw_json`. Live: 203 fetched, 132 eligible, ~2.5 min (with a 60 budget, which ran out).
- **Shared: `geo.restriction_from_prose`** (moved out of Wellfound) narrows a worldwide claim from JD text. With `skip_pay_sentences=True` (Remote OK and WWR) it ignores pay sentences, because "salary ranges for all US-based postings" restricts nobody. Ingest now **appends the adapter's own notes after the filter's verdict** (`eligibility_reasons`). It used to overwrite them, so "treated as worldwide (unverified)" never reached the drawer, for Wellfound either.

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
- **`founding_engineer` matches the BARE title only** (`founding engineer`),
  never `founding <anything> engineer`. It exists because "Founding Engineer"
  names no stack and so matched no other family; qualified variants already
  have homes ("Founding Backend Engineer" is `backend`). The wildcard form was
  tried and it turned `founding` into a lone qualifier — the exact failure the
  head-anchoring rule above exists to prevent. It admitted "Founding Flutter
  Engineer" and "Founding Data Pipeline Engineer", reopening the `mobile` and
  `data` families that `filters.yaml` switches off on purpose, plus "Founding
  Customer Success Engineer", which is not engineering at all. 16 live rows
  pass on the bare form.
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
- **Left nav shell** (`components/SideNav.tsx`): the app is a rail plus a
  content column, with one tile per top-level surface — **Jobs** (everything
  above) and **Matches** (Phase 4, shell only). The active tile lives in the URL
  as `?view=`, parsed by `useFilters` alongside the filters, so a view is
  linkable and back moves between views. Default (`jobs`) is left out of the
  URL, so existing links keep meaning the job list. Switching tiles keeps the
  filters and drops the open job. The rail collapses to icons on desktop
  (persisted) and becomes an off-canvas drawer under `md`, where TopBar's menu
  button opens it — TopBar gave up the brand block to the rail in exchange.
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

**Built (2026-09-08) — the deep read (`app/llm.py`, `app/llm_runner.py`):**

- One Groq call per routed row carries **both halves**, as planned: the
  validity fields (`posting_status` active/evergreen/ghost, `status_reason`,
  `seniority`, `tech_stack`, `compensation_text`, `sponsorship_required`,
  `inconsistencies`) and the fit fields (`fit_band`, `fit_reasons`,
  `strengths`, `gaps`). The JD has to be in the prompt either way, so two
  calls would double the only cost that matters.
- Lands in the `job_matches.llm_used / llm_content_hash / llm_verdict` columns
  that were already designed for it. **No migration.** The separate
  `validity_score` / `validity_reasons` columns on `job_postings` above, and
  the dashboard badge and `min validity` filter, are still unbuilt.
- ~~Routing is `MatchVerdict.needs_llm` (score ≥ threshold **or** low
  confidence)~~ — **superseded 2026-09-13 by the LLM shortlist**, see
  "Transparency and the LLM budget" below. The low-confidence clause spent
  most of 1.26M tokens on jobs the LLM then called weak or poor.
- **Two hard blockers only prose can show**, and they are separate fields on
  purpose. `sponsorship_required` catches "must be authorized to work in the
  US without sponsorship", which eligibility rule 1 cannot see because it
  reads the board's *structured* location fields. `work_mode = onsite` is a
  blocker in its own right because `profile.work_authorization` is remote-only.
  The first live run folded location and work-mode together and reported Steps
  AI's "On-Site, Hyderabad, **India**" role as "India cannot hold this" — the
  right answer for the wrong reason, and the wrong reason is what the
  dashboard would have shown.
- **A field's NAME outweighs its description.** While the pay field was called
  `comp_stated`, the model answered the literal string `"No"` on postings that
  state no pay, ignoring a description that spelled out the empty-string rule
  twice. Renaming it `compensation_text` fixed it with no other change.
  `location_policy` is likewise phrased about the posting, not the candidate.
  **Known residual:** it still reads an India-located *on-site* role as
  `excludes_india`. `blocked` is unaffected (`work_mode` catches those), but
  the field itself is not trustworthy for India-located on-site rows.
- **Resumable and cache-correct.** Committed every 10 rows, failures recorded
  with an `error` in the verdict rather than left null, so "failed" and "not
  reached yet" stay distinguishable across a 2-hour pass. `match_runner` now
  **clears `llm_verdict` when it re-scores a row whose JD moved** — it stamps
  `llm_content_hash` itself, so without that a changed JD would re-stamp the
  hash and leave a stale verdict looking current.
- Run it with `python -m scripts.run_llm_screen` (`--dry-run` prices it,
  `--limit N` bounds it, `--force` re-reads cached rows). `POST /matches/llm`
  runs the same function as a background task for the dashboard, with
  `GET /matches/llm/status` to poll — a ~2-hour pass cannot be a synchronous
  request.

**Built (2026-09-13) — closing 2B (migrations `0004`, `0005`):**

- **Deterministic validity** — `app/validation.py` + `validation_runner.py`,
  config in the `validity:` block of `filters.yaml`. Whole board, free, ~12s.
  Persists `validity_score` (NULL = not yet checked, never 0) and
  `validity_reasons[]`, each penalty naming its amount (`stale:67d:-20`).
  Checks: missed last sweep, age off `posted_at`, evergreen/contentless prose,
  missing fields, duplicates. `POST /validity/run`.
- **The ghost check compares against a run's `started_at`, not
  `finished_at`.** Rows are stamped as their board is swept and a full sweep
  takes ~6.5 min, so the first cut flagged 2,123 live rows as ghosts.
- **Duplicates: 2 on the eligible slice, 223 on the whole board.** Both are
  right. The key is (company, title, JD) and deliberately *not* location —
  Databricks posts one role across nine cities as nine rows.
- **The LLM cache split is now real.** Before this, both halves of the screen
  lived in `job_matches.llm_verdict` under one key, so a profile edit stranded
  validity answers that had already been paid for. Validity fields now go to
  `job_postings.llm_validity` keyed by `content_hash` alone
  (`JobScreen.VALIDITY_FIELDS` / `FIT_FIELDS`). Verified live: a profile
  version change invalidated every fit verdict and kept all 10 validity ones.
- **`employment_type` / `workplace_type`** — the adapters were already parsing
  these and throwing them away. Backfilled from `raw_json` with no network
  (`scripts/backfill_employment.py`) for 3,992 rows. Greenhouse publishes
  neither, so its rows stay NULL — and NULL passes every preference. These
  are deliberately **not** in `content_hash`, which would have invalidated
  every cached verdict.
- **Dashboard** — validity column in the Jobs table (no badge at all when
  unscored), validity + deep-read section in the drawer, `min validity` filter
  (`minval=` in the URL, facet bands). `description_html` now goes through
  `lib/sanitize.ts`, an allowlist sanitizer. A leak was found in its first
  version: the parser rewrites NUL to U+FFFD, which slipped past a "no scheme,
  so relative" fallback. `safeUrl` now rejects anything that looks like a
  scheme and isn't on the allowlist. **Correction (2026-09-26): the jsdom test
  this used to claim is not in the repo** — `frontend/` has no test runner and
  no test files at all. The sanitizer ships; its regression test does not.
  Worth adding, since it is the one piece of frontend code where a silent
  failure is a security bug.
- **Stabilization** — `"SF, NYC, SEA, CHI"` resolved to ASEAN through a bare
  `sea` alias; `SEA` and the other US metro codes now map to US.
  `looks_like_timezone` matched "East" under IGNORECASE, so "South East Asia"
  was stored as a timezone; the abbreviation branch is case-sensitive now and
  regions resolve before timezones. A curated board that fetches 0 rows and has
  never stored any sets `never_produced_rows` — this is how **`ashby:deel`**
  failed silently. Its slug still needs checking by hand.

**Preferences — the Matches gate (`prefs.yaml`, `app/prefs.py`, `prefs_gate.py`):**

A third config layer. `filters.yaml` decides eligibility at ingest time and
also drives Telegram alerts, so it is not editable from the UI. `profile.yaml`
is who I am. `prefs.yaml` is what I want right now: work mode, commitment,
years band, pay floor, min validity and exclusions. It gates the **Matches
list only**, and the Jobs tile ignores it.

- The verdict is an overlay on `job_matches` (`prefs_pass`, `prefs_reasons`,
  `prefs_version`) and is **not part of the unique key**, so editing a
  preference never creates duplicate rows and costs zero LLM calls.
- `prefs_version` hashes canonicalized values. Hashing raw floats gave
  `3000000` and `3000000.0` different versions, so every save looked like a
  change.
- **Run** (`POST /matches/run`, polled via `/matches/run/status`) does
  validity, then ranking with the gate, then an estimate of the deep read's
  cost — and **stops there**. The UI shows jobs, minutes and share of the
  daily budget, and the deep read only starts after an explicit confirm
  (`POST /matches/llm`). Preferences cut the routed set from 417 to 314.
- Default preferences (remote, full-time, 2-8y, ₹30L) admit 719 of the 929
  eligible jobs. *(Now 5-8y and posted ≤ 30d: 401 of 910 on 2026-09-13.)*

**Built (2026-09-13) — transparency and the LLM budget (migration `0007`):**

The dashboard showed 5,481 / 740 remote / "430 of 910 eligible" / a deep read
quoted at 198 that read 427, with nothing connecting them. And one confirm had
spent 1.26M tokens.

- **One filter builder** — `app/job_filters.py` (`JobFilterSet`,
  `apply_job_filters`). `/jobs`, facets, the funnel and the Matches scope all
  use it. **Remote means one thing**: `IS_REMOTE` = the board's
  `workplace_type` when stated, else the keyword flag.
- **Funnels** — `GET /meta/funnel` (same params as `/jobs`, last step equals
  its total) and `GET /matches/funnel` (from stored verdicts; `stale` when
  preferences changed since the last Run). `app/funnel.py`. Both render as a
  strip above their list, each step showing what it removed and why.
- **Posted ≤ 30 days is a hard limit.** Jobs defaults to 30d (`posted=any` to
  widen); `prefs.freshness.max_age_days` is 1-30 and cannot exceed 30. The
  SQL and Python sides cut at the same instant — rounding the age to whole
  days put 619 rows in Matches against 612 in Jobs.
- **Send to Matches** — `PUT /matches/transfer` stores the Jobs *filters* (not
  ids) in `prefs.yaml`; each Run re-applies them, so later sweeps flow in. Rows
  outside the scope fail the gate with `not_transferred`. Matches no longer
  reads the live URL filters. `DELETE` returns to all eligible jobs.
  Eligibility is still a hard gate in Matches even if sent with "All roles".
- **Experience floor 5** (was 6). At 6 the band dropped 372 jobs and every one
  asked for *fewer* years (162 asked 5+); none asked more than 8.
- **The LLM shortlist** (`app/shortlist.py`) replaces `needs_llm` routing. A
  job is read only if it passed preferences, scored ≥ 65, was scored
  confidently, was verified ≥ 70, and has no JD blocker. Best score first,
  **15 per pass by default, never more than 50** (`clamp_reads`; the old
  `LLM_MAX_REQUESTS_PER_RUN=500` is clamped rather than rejected so an old
  `.env` still boots). The confirm offers 10/15/20/50, re-priced by
  `GET /matches/llm/estimate`. Low-confidence jobs are read one at a time
  from the drawer (`POST /matches/{job_id}/llm`). The estimate and the pass
  select through the same function, so they cannot disagree again.
- **Free JD blockers** — `app/blockers.py`, stored as `job_matches.blockers` /
  `blocked`. US work authorization, no sponsorship, US persons, clearance,
  region-only remote. Region rules are vetoed by an India/APAC/worldwide
  mention in the same sentence. Tuned against the JD sentences, **not** the
  LLM's labels, which were noisy: it marked "Applied AI Engineer - India" as
  `excludes_india` and claimed sponsorship rules for GitLab and Pinterest JDs
  that state none. 34 eligible jobs carry one.
- **Every row states its checks**: Verifier ran (score) or not, LLM read or
  not (`JobOut.llm_read`, `validity_checked_at`; `MatchOut.llm_read`,
  `shortlisted`, `shortlist_reasons`, `blockers`).
- Fixed on the way: `POST /validity/run` called an unimported `run_validation`.

**Built (2026-09-19) — the fit prompt reads evidence, not keywords:**

- The LLM used to see only the weighted skill list, so it could not tell
  "used an LLM API in a side project" from "built LLM infrastructure in
  production". `profile.yaml` now carries `evidence:` (one line per
  capability, `depth: production|project`, with the résumé's numbers),
  `unproven:` (capabilities the résumé does not show) and `domains:`.
  `profile_brief()` sends evidence first as `[P]`/`[S]` lines and drops skills
  the evidence already names.
- `gaps` is split into `must_have_gaps` (with `" (partial)"` for adjacent or
  side-project evidence) and `nice_to_have_gaps`. `fit_band` is generated
  **last**, after the lists it summarizes. The drawer reads the old `gaps` as
  must-have gaps.
- `FIT_PROMPT_VERSION` in `profile.py` is hashed into `profile_version`. Bump
  it whenever the fit prompt changes meaning, or old verdicts stay "current".
- Measured on Cerebras qwen against the old prompt, same two JDs: +3% and
  +13% tokens (~5.5-6.3k per long JD). The old prompt rated "Agentic AI
  Engineer" *strong* despite four must-have gaps; the new one says
  *moderate*. Cerebras strips `maxItems`, so the list limits are also written
  into the descriptions ("up to 6"). Without that, strengths ran to 13 items,
  and one row hit `max_completion_tokens`.

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

## PHASE 2C — Matches (profile fit + tailored documents)

**Goal:** of everything the aggregator has collected, surface the jobs that fit
**my** profile, ranked with reasons I can argue with, and for the ones worth
applying to, draft a tailored résumé and cover letter. The `Matches` nav tile
and its placeholder surface (`SideNav.tsx`, `MatchesView.tsx`) already exist —
this fills them in.

Runs **after 2B**, so matching never ranks a ghost posting highly, and
**before Phase 3**, because autofill needs the profile and the documents this
phase produces.

### Stage 1 — Your profile

- `profile.yaml` + résumé file(s), Pydantic-modelled: contact and links, work
  authorization, skills, seniority, comp floor, location rules, and the
  reusable free-text answers every application asks for.
- **This is the profile store Phase 3 used to define — it lands here instead,
  and Phase 3 consumes it.** One profile, not two.
- Editable from the dashboard, but `profile.yaml` on disk stays the source of
  truth: it is the one file worth backing up, and hand-editing it must not be a
  second-class path.
- **Every write bumps a `profile_version`** (hash of the normalized content).
  Scores and documents are keyed to it. Without that, editing my résumé leaves
  yesterday's scores on screen looking current.

### Stage 2 — Fit score

- **Scored surface is `status = open` AND `eligibility_pass = true` — 695 rows
  today, not the 5,251 open ones.** Eligibility is Phase 1's hard binary gate;
  fit is a *ranking on top of it*. Scoring rows already rejected on location or
  pay spends tokens on jobs I cannot take. Unscored browsing stays in the Jobs
  tile.
- **Deterministic first** (`app/matching.py`, built like `eligibility.py`):
  tech-stack overlap with the profile, title/seniority distance from the wanted
  families in `roles.py`, comp against the floor, location and timezone fit.
  Cheap, free, runs on every scored row, and emits reasons on its own.
- **LLM second, and only on the ambiguous middle** — the band the deterministic
  pass cannot separate. Anthropic API, structured output.
- **Cached by `content_hash` + `profile_version`.** Both halves of that key
  matter: unchanged JD *and* unchanged profile → zero API calls. `content_hash`
  is already maintained by ingest and is the same key 2B's validator uses.
- **Scoring is a separate pass, never inline in ingest.** A sweep already takes
  ~90s with Wellfound in it; an LLM call per job on top would make 2A's
  fetch-on-load unusable. Trigger it from the Matches view, or as a background
  task after a run completes.
- Persist the score and `match_reasons[]` **per (job, profile_version)**, in
  their own table rather than as columns on `job_postings`. A score is a fact
  about a *pairing*, not about the posting.

### Stage 3 — Tailored documents

- Generated **per job, on request** — never in bulk across the match list. This
  is the expensive stage, and most matches never get applied to.
- A résumé re-ordered and re-worded to lead with what that JD asks for, and a
  cover letter drafted from profile + JD.
- **Hard constraint: the generator may only select, re-order and re-word facts
  stated in the profile.** It must never invent an employer, a date, a title or
  a metric. A résumé that hallucinates experience is worse than no résumé — it
  is a lie with my name on it, and if it reads well I will not catch it at
  review time. This needs a fixture test, not just a prompt instruction.
- Stored as **drafts**, labelled as such, versioned by (job, profile_version).
  They feed Phase 3's autofill; they do not replace its review step.

**Built (2026-09-20) — Stage 3, the résumé (migration `0008`):**

A "Tailor" button on every Matches row and in the job drawer opens a modal with
the LaTeX on the left and the compiled PDF on the right, Overleaf-style. What
is downloaded is byte-for-byte what is previewed.

- **The résumé is LaTeX, and `data/Ashish_AI_FullStack_v2.tex` is the
  template.** `app/resume_tex.py` parses it into 16 addressable regions
  (`summary`, `exp.asint.0-7`, `exp.incture-technologies.0`, `proj.0`, five
  `skills.*` rows) and splices edits back in from the end of the file
  backwards. **Rendering with no edits is byte-identical to the template** —
  that is the safety property, and a section the model said nothing about
  cannot change.
- **The model never sees or writes LaTeX.** It gets plain text with `**bold**`
  markers and answers with decisions keyed by region id. `to_latex` escapes in
  a single pass, so `\input{/etc/passwd}` becomes literal characters. It cannot
  add a bullet: there is no slot to add one to.
- **A dropped bullet takes its `\item` with it.** The first cut made the region
  start *after* the token, which rendered an empty bullet in the PDF. Skills
  rows are re-worded, never removed — dropping one leaves a dangling `\\` that
  breaks the tabular.
- **`app/factcheck.py` is the fixture test's subject.** Numbers are
  **blocking**: a rewrite carrying a metric absent from the profile is
  discarded and the original bullet kept, because "80% mAP" becoming "92%"
  reads exactly as well as the truth and survives any proofreading. Unknown
  terms only **warn** — mirroring the JD's vocabulary is the feature.
- **`gaps:` is a denylist, not part of the allowlist.** The first version built
  one corpus from the whole profile, so "Shipped it on Kubernetes" passed:
  `kubernetes` is a key in `gaps:`, and the evidence line "No AWS, Azure or
  Kubernetes in production" put it there twice over. Claiming a `gaps:` stack
  is now blocking. `unproven:` is deliberately NOT treated this way — it is
  prose ("leads and mentors, no direct reports stated") and harvesting its
  words would forbid "leads" and "mentors", which the résumé states truthfully.
- **The corpus indexes every word; the claim side only capitalised ones.**
  Indexing only proper nouns made the two sides asymmetric, and any word the
  résumé writes lower-case that a rewrite happened to start a sentence with was
  reported as invented — "Schema-grounded" was, on the first run.

**Compilation — Tectonic, vendored (`app/latex.py`):**

- There is no TeX distribution on this machine and installing one is ~500MB
  that then repeats in the Docker image. Tectonic is one binary that fetches
  only what a document needs. `python -m scripts.install_tectonic` puts it in
  `backend/.tools/` (gitignored, ~50MB). **Cold: ~5 min once, ever. Warm:
  ~1-3.5s.** That is why Recompile is a button, not a keystroke.
- Run with `--untrusted`: the .tex is hand-editable in the browser, so it is
  input, not code we wrote.
- **`data/resume.cls` is reconstructed, not the Overleaf original**, which was
  never in the repo — `data/` is gitignored as PII. It is Trey Hunner's
  upstream class plus two changes, each found by diffing a Tectonic render
  against the real `Ashish_AI_FullStack_v2.pdf` until they matched: `hyperref`
  (the résumé uses `\href` and the class never loads it, so nothing compiled at
  all), and rSection's `\leftmargin` 1.5em → 0em. Verified: one page, same line
  breaks, same blue links. **If the Overleaf original turns up, drop it in.**

**Cost, measured live (Cerebras qwen):**

- **~6.3-8.8k tokens per document**, against ~1,800 for a deep-read screen: the
  JD, the profile brief and all 16 regions go up, and up to 16 rewritten lines
  come back. On Groq's free 200K/day that is ~22 documents a day.
- It needs a **bigger completion ceiling** than the screen — at 4,096 the
  answer truncated mid-JSON. `llm_tailor_completion_tokens` (10,000) is its own
  knob because Cerebras books the figure against its limits up front.
- Nothing generates one automatically. The modal opens on an empty state with a
  button; `POST /matches/{job_id}/document` returns the stored document and
  spends nothing when neither the JD nor the profile has moved.

**Storage and the API:**

- `generated_documents` stores the **LaTeX**, not the PDF: the PDF is a pure
  function of it and recompiles in seconds, and the compiled bytes live in a
  small in-process cache keyed by (document id, hash of the .tex).
- `tailoring` (what the model said, verbatim) is kept beside `edits` (what was
  applied), so "why is this bullet unchanged?" stays answerable after the fact
  check discards a rewrite. It also makes **Revert free** — hand edits are
  undone by replaying the stored tailoring, with no second LLM call.
- `GET|POST /matches/{job_id}/document`, `PUT /documents/{id}`,
  `POST /documents/{id}/{compile,revert}`, `GET /documents/{id}/{pdf,tex}`,
  `GET /documents/base`. A failed compile answers **200 with `ok: false`** —
  the editor is expected to be mid-edit and broken half the time, so a broken
  document is a state to render, not an HTTP error to handle.
- Fixed on the way: `api.py` already had a route handler named `get_job`, which
  silently shadowed the imported helper of the same name and called it with
  swapped arguments (an instant 500 on the cache-hit path). Imported as
  `require_job` now.

**Built (2026-09-21) — refining the résumé by chat (migration `0009`):**

The Tailor modal's left pane has a **LaTeX | Chat** tab pair. Chat sits there
and not over the PDF, because the preview is what each suggestion gets
checked against. `app/resume_chat.py`, `components/ResumeChat.tsx`.

- **A reply is a proposal, never an edit.** Each change shows before/after and
  is applied (all, or ticked ones) or dismissed. Applying is free and marks the
  document `hand_edited`, so Revert undoes chat edits like any other.
- **Same guards as tailoring:** region ids in, plain text out, and `factcheck`
  against the profile **plus the base résumé**, never the current document.
  Otherwise a hand edit would launder an invented metric into "already stated".
  Blocked changes stay visible with their reason. Verified live: "add Next.js"
  was refused in the reply, and only the reorder was proposed.
- **Stale-safe:** a proposal stores the hash of the .tex it was written
  against. If the résumé has moved since, it still applies when every region
  it touches still reads as it did, and is refused otherwise.
- Stored as `generated_documents.chat` (JSON) and reset on regenerate. The last
  8 messages go back to the model. Starter prompts come free from the stored
  `jd_keywords` and the deep read's `must_have_gaps`.
- **Cost:** ~4k tokens a message measured (Cerebras qwen), and up to the
  tailoring's ~9k on a long JD.
- `GET|POST|DELETE /documents/{id}/chat`,
  `POST /documents/{id}/chat/{message_id}/{apply,dismiss}`.

**Built (2026-09-22) — the cover letter (`app/cover_letter.py`, no migration):**

A **Cover letter** button beside Tailor (row and drawer) opens the same modal,
with a Résumé | Cover letter switch in its header. Stored in
`generated_documents` as `kind = "cover_letter"`; the routes take `?kind=`.

- **The model writes only the body paragraphs.** Header, date, addressee,
  greeting and sign-off are rendered from `profile.yaml` and the posting, and
  the contact links reuse the résumé's own `\href` labels.
- **No original to fall back to, so a blocked claim removes its SENTENCE.**
  The corpus is profile + base résumé, plus the JD's *words* (naming what the
  employer builds must not warn) but **not the JD's numbers**, or "5+ years"
  in a requirement would vouch for a claim. `gaps:` stays blocking.
- The body sits between `% --- BODY` markers so a hand-edited letter is still
  fact-checked without reading the header (whose date is never in the profile).
- `edits` stores the rendered paragraphs + company/title/date, so Revert is
  free. Chat is résumé-only (409 on a letter): its proposals are keyed by
  résumé regions.
- **Cost: 7,499 tokens** measured (Cerebras qwen, Pulsora "AI Engineer -
  India"), close to a tailoring, because the input dominates.
  `llm_cover_completion_tokens` (6,000) is its own ceiling.

**Built (2026-09-26) — closing 2C: profile intake/editor, the diff, letter chat
(no migration):**

Three things were outstanding. None needed a schema change.

- **Stage 1's editor exists** (`app/profile_intake.py`, `components/ProfilePanel.tsx`).
  `GET /profile` now answers **200 with `configured: false`** instead of 500
  when there is no profile — a first run legitimately has none, and the
  dashboard has to tell "not set up" from "the server is broken" to open the
  setup dialog rather than an error screen. Added `GET /profile/document`
  (the raw mapping), `PUT /profile` and `POST /profile/upload`.
- **The editor round-trips the raw YAML mapping, not the `Profile` model.**
  `Profile` drops keys it does not declare, so saving the model would silently
  delete a hand-added key — and CLAUDE.md says hand-editing must not be
  second-class. A save is atomic (tmp + replace) and clears `get_profile`'s
  `lru_cache`, mirroring `save_prefs`.
- **Upload is two files and two steps.** The **.tex** becomes the tailoring
  template; the **PDF** is what Phase 3 attaches. `resume_tex.resolve_template`
  now looks at `data/resume.tex` first, then the legacy hardcoded name, then a
  lone `.tex` under any name — `data/` is gitignored as PII, so a checkout has
  whatever was dropped in. The LLM drafts a profile from the .tex and **nothing
  is written until the human confirms**, because `gaps:` is the fact-checker's
  denylist, `evidence.depth` decides whether a capability reads as production,
  and `unproven:` is about what the résumé omits. A gap the model misses is a
  protection that silently stops applying. One call, ~5-7k tokens, setup only.
- **Known limit on the upload:** `resume_tex` parses the rSection/itemize
  structure of *this* class, and `data/resume.cls` is the only class file
  vendored beside it. A .tex on a different template stores and de-TeXes fine
  (intake strips markup rather than locating structure), so the profile still
  fills in — but `/documents/base` will report no editable regions and
  tailoring stays unavailable until the template matches. The upload warns when
  a file has no `\documentclass`, not when it has an unfamiliar one.
- **The intake schema uses arrays where the file uses maps.** `skills:` is
  `{category: {skill: weight}}` and strict `json_schema` cannot describe an
  open-ended map (every object needs `additionalProperties: false` and a fixed
  `required`). The model answers with `{category, name, weight}` rows and
  `draft_to_profile_data` folds them back. It also **drops a drafted gap that
  is also a skill** — a term in both would block a claim the résumé supports.
- **`tex_to_text` is a de-TeXer, deliberately not `ResumeDocument`**, which
  parses the 16 regions of one known template. Intake must cope with any
  reasonable file, so it strips markup instead of locating structure. Three
  bugs it was measured into fixing: `$\sim$90\%` degraded to `$$90%` (math
  shorthands now map, and `\$` rides a sentinel past the `$` strip); `\itemsep
  -3pt {}` has no braces, so stripping the macro left "-3pt" mid-résumé; and a
  `tabular` column spec is three braces deep, so a non-greedy match spilled
  ">p1.5in @" into the text (`_skip_args` is brace-aware now).
- **A CRLF .tex was being corrupted — found only by live testing.**
  `write_text` applies the platform newline translation, so an uploaded
  Windows file's existing `\r\n` was rewritten to `\r\r\n`: a 7,404-byte
  template stored as 7,557. `newline=""` fixes it, and the test suite now
  asserts byte-for-byte storage for both line endings. The inline-LaTeX
  fixtures are all LF, which is why only the real file exposed it.
- **The diff against base** — `GET /documents/{id}/diff`,
  `documents.diff_against_base`, `components/DiffPane.tsx`. Region-level, not
  line-level: the tailored .tex is the template with spans spliced in, so a
  line diff is brace noise around every touched bullet.
- **Bullets are matched by CONTENT, not by region id**, which is the whole
  difficulty. A bullet's id is its *position* (`exp.asint.3`), so dropping one
  renumbers every bullet after it and reordering a pair swaps their ids. The
  first cut matched on id and reported a real tailoring as "the last bullet was
  deleted plus five rewrites". Within each group, identical texts pair first
  (`unchanged` / `moved`), leftovers pair by token overlap ≥ 0.25
  (`reworded`), and the remainder is `dropped` / `added`. Verified live on
  document 3: 5 reworded, 5 moved, 1 dropped — the moved rows are exactly what
  id-matching mislabelled. `moved` is a separate verdict because a reordered
  bullet's text is identical, so a text comparison calls it unchanged. `added`
  can only come from a structural hand edit and is surfaced, not hidden.
- **Cover-letter chat** (`app/cover_chat.py`) — the 409 is gone. Its regions
  are the body paragraphs (`para.0…N`, read back by
  `cover_letter.body_paragraphs`), and it exposes the same five functions in
  the same proposal shape as `resume_chat`, so the routes pick a module by
  `kind` and one panel renders both.
- **Three differences from the résumé's chat, each forced by the letter.** It
  **can add a paragraph** (`para.N` one past the end) because prose has no
  fixed slots — the fact check is the only constraint, not the absence of a
  slot. A paragraph with one blocked sentence is offered **blocked whole**
  rather than silently shortened: generation trims because it has no fallback,
  a chat has one, and quietly returning less than was asked for hides the
  refusal. And it writes through **`cover_letter.replace_body`**, which splices
  between the `% --- BODY` markers rather than re-rendering — re-rendering
  rebuilds the header from the current profile and today's date, so accepting a
  wording change would restamp the letter's date.
- **A partial `order` is discarded**, since applying it literally would delete
  the paragraphs it forgot to mention. Paragraphs are keyed by id through the
  rewrite, so a drop and a reorder in one proposal still move the right text.
- Verified live against the stored Pulsora letter: "I have run Kubernetes
  clusters at scale on AWS" came back blocked naming both gap stacks *and* the
  offending sentence; an honest rewrite applied with the header, footer and
  date byte-identical.
- **Tests:** `test_profile_intake.py` (44), `test_api_profile.py` (19),
  `test_cover_chat.py` (27), `test_document_diff.py` (11) — 101 new, 903
  collected, all green. `python-multipart` added for the upload.

**Still not built:** a regression test for `lib/sanitize.ts` (see 2B's
correction above — `frontend/` has no test runner at all).

### Schema

- `job_matches` — `job_id` FK, `profile_version`, `score`, deterministic
  subscores, `match_reasons[]`, `llm_used`, `scored_at`. Unique on
  (`job_id`, `profile_version`).
- `generated_documents` — `job_id` FK, `profile_version`, `kind`
  (`resume | cover_letter`), content, `is_draft`, `created_at`.
- Alembic migration for both, dialect-neutral per the stack rules.

### API

- `GET /profile`, `PUT /profile` — read/write the profile; the write returns the
  new `profile_version`.
- `POST /matches/score` — run the scoring pass, cache-aware (`force=true` to
  bypass). Reports live progress the way `/ingest/status` does, and shares that
  module's lock discipline.
- `GET /matches` — scored jobs by score, with reasons; composes with the
  existing `/jobs` filters.
- `POST /matches/{job_id}/documents` — generate for one job; `GET` reads back.

### Dashboard (replaces the `MatchesView` placeholder)

- Match list with score and reasons, reusing 2A's job drawer.
- Profile editor.
- Per-job "generate documents", with a **diff against the base résumé** so I can
  see exactly what was changed, plus download.
- `view=matches` already round-trips through the URL (`useFilters.ts`) — keep it.

**Acceptance criteria:**

- Scoring is idempotent and cached: a re-run with no profile change and no JD
  change issues **zero** LLM calls.
- Editing the profile bumps `profile_version` and re-scores; a stale score is
  never displayed as current.
- Only open, `eligibility_pass = true` jobs are scored.
- Every listed match shows the reasons behind its score — the low ones included.
- A generated résumé contains **no** claim absent from `profile.yaml` (fixture
  test).
- Generated documents are always drafts for review; **no send or submit path
  exists in this phase**.

---

## PHASE 3 — Autofill (review-before-submit)

**Goal:** from a selected job + my stored profile/resume, open the application page, auto-fill standard fields, draft answers to custom questions, and present everything for my review. **The system never submits autonomously — a human confirm is always required.**

**Build:**

- **Profile store:** reuse **Phase 2C's** `profile.yaml` + résumé files — the same profile, not a second one. Phase 3 adds only what autofill needs on top: per-ATS field aliases, and answers a form asks for that matching never needed.
- **Per-ATS fillers** via Playwright — `GreenhouseFiller`, `LeverFiller`, `AshbyFiller`: map profile fields → each ATS's known application-form selectors (resume upload, name, email, phone, LinkedIn, etc.). Keep this isolated per ATS, same pattern as the Phase 1 adapters.
- **Custom-question handler:** detect non-standard/free-text questions on the form; draft answers with the Anthropic API using profile + JD; label them `DRAFT — review`.
- **Résumé upload** attaches 2C's tailored draft for that job when one exists, else the base résumé from the profile store.
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
