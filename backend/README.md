# Phase 1 — Aggregator

Fetches live postings from every company in `companies.yaml`, normalizes them
into one schema, stores them in SQLite, detects genuinely-new jobs, and pushes
Telegram alerts.

## Supported platforms

| ATS | Endpoint | Verified live |
|---|---|---|
| **Greenhouse** | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | ✅ 5 boards |
| **Lever** | `GET https://api.lever.co/v0/postings/{slug}?mode=json` | ✅ 2 boards |
| **Ashby** | `GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | ✅ 4 boards |
| Playwright | — | stub only (fails loudly if enabled) |

All three are public, unauthenticated JSON APIs with no pagination.

## Quick start

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt

cp .env.example .env            # fill in Telegram creds (optional)
alembic upgrade head            # create the schema

python -m scripts.verify_endpoints   # confirm every board in companies.yaml is live
uvicorn app.main:app --reload        # http://localhost:8000/docs
```

Trigger a run and browse the results:

```bash
curl -X POST "http://localhost:8000/ingest/run?notify=false"
curl "http://localhost:8000/jobs?remote=true&q=engineer&limit=20"
```

With Docker (from the repo root): `docker compose up --build`.

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/jobs` | Filters: `company` (repeatable), `ats` (repeatable), `department` (repeatable), `remote`, `status`, `q`, `q_scope`, `posted_within_days`, `first_seen_after`, `limit`, `offset`, `sort`, `order` |
| `GET` | `/jobs/{id}` | Full description; `?include_raw=true` returns the original ATS payload |
| `GET` | `/meta/facets` | Filter options with counts + headline stats (backs the dashboard dropdowns) |
| `POST` | `/ingest/run` | Manual trigger; `?notify=false` to skip alerts. 409 if a run is in flight |
| `GET` | `/ingest/runs` | Run history with per-source counts |
| `GET` | `/companies` | Configured boards |
| `GET` | `/health` | Job counts + last ingest time |

Two notes on `/jobs`:

- **`q_scope`** — `all` (default) searches title + company + description;
  `title` searches titles only. Full-JD search is genuinely broad:
  `q=engineer` matches all 400 Anthropic jobs because their boilerplate says
  "researchers, engineers, policy experts".
- **Ordering** — after the requested `sort`, results tie-break on `posted_at`
  then `id`. A bulk first ingest stamps every row with an identical
  `first_seen_at`, so an id-only tie-break would render the default view as a
  single company's jobs.

## Adding a company

Append to `companies.yaml` — no code changes if the ATS is already supported:

```yaml
  - company: Acme
    ats: greenhouse           # greenhouse | lever | ashby
    token_or_slug: acme       # the slug from the board URL
    enabled: true
```

Then `python -m scripts.verify_endpoints --company Acme --sample` to confirm the
slug is right and inspect one normalized posting.

## How correctness is enforced

**Identity.** Every job is keyed on `source_key = "{ats}:{company}:{job_id}"`
using the ATS-native id, with a unique index. That single constraint is what
makes re-ingestion idempotent.

**Closure by disappearance.** A job missing from a source's latest *successful*
fetch flips to `closed` (with `closed_at`); it is never deleted. A failed fetch
is not evidence of closure, so an erroring source closes nothing. If the job is
listed again later it reopens in place — it does not re-alert.

**The empty-fetch guard.** Ashby answers a bad slug with **HTTP 200 and
`{"jobs": []}`**, not a 404. Without protection, one typo in `companies.yaml`
would silently close an entire company's postings. So when a board that
previously had open jobs returns zero, the closure sweep is skipped and a
warning is logged (`GUARD_EMPTY_FETCHES`). Lever 404s on a bad slug, so its
empty responses are trusted and *do* close jobs.

**Failure isolation.** Each source is fetched and persisted in its own
transaction. A network error, a malformed payload, or a DB failure on one board
cannot roll back or abort the others. A single unparseable posting is skipped
without losing the rest of the board.

**No wasted writes.** A `content_hash` over the meaningful fields means an
unchanged job only has `last_seen_at` refreshed. (This hash also seeds the
Phase 2 LLM validation cache.)

## Live-data quirks encoded in the adapters

Found by inspecting real responses before writing any field mapping:

- **Greenhouse** returns `content` **entity-escaped** (`&lt;p&gt;`), so it is
  unescaped before storage — detected rather than assumed, since not every
  board does it. `location.name` mixes single (`"San Francisco, CA"`) and
  multi-location (`"SF, NYC, SEA, CHI"`) strings. Field sets drift: `education`
  appeared on 6 of Stripe's 548 jobs, so the payload models are permissive and
  only `id` + `title` are load-bearing.
- **Lever** returns a bare array with no envelope. `createdAt` is epoch
  **milliseconds**, and there is **no update timestamp at all** (`updated_at`
  is always null for Lever jobs). The JD is split across `description`,
  `lists`, and `additional`, and is reassembled into one HTML document.
- **Ashby** `secondaryLocations` holds objects, not strings. `isRemote` and
  `workplaceType` genuinely disagree in live data — some jobs are
  `isRemote: true` **and** `workplaceType: "Hybrid"` — so `workplaceType` wins,
  because a hybrid role is not remote for someone filtering remote-only.

## Tests

```bash
python -m pytest            # 118 tests, no network access
```

Adapters are tested against **recorded JSON fixtures** in `tests/fixtures/`,
trimmed from real responses and chosen to cover the edge cases above
(escaped HTML, multi-location, each `workplaceType`, populated
`secondaryLocations`). CI never hits a live endpoint — only
`scripts/verify_endpoints.py` does, and it is never invoked by the suite.

Acceptance criteria are covered by name in `tests/test_ingest.py`:
`TestIdempotency`, `TestClosureByDisappearance`, `TestFailureIsolation`,
`TestNewJobNotification`.

## Verified end to end

Two consecutive live runs across all 11 configured boards:

```
RUN 1: fetched=3391 new=3391 updated=0 closed=0 errored=0
RUN 2: fetched=3391 new=0    updated=0 closed=0 errored=0
total rows: 3391   distinct source_keys: 3391
```

## Notes for Phase 2

- `GET /jobs?q=` is a substring search across title, company **and** full JD
  text, which is broad — `q=engineer` matches all 400 Anthropic jobs because
  their boilerplate mentions "engineers". A title-scoped option belongs in the
  dashboard's filter UI.
- Multi-location strings are stored verbatim rather than split, since
  `"San Francisco, CA"` and `"SF, NYC, SEA"` are not separable by a comma
  rule. Splitting is listed as Phase 2 stabilization work.
- `posted_at` is null-safe but Lever supplies no `updated_at`, so staleness
  checks must not assume that field exists.
- SQLite is in use per instruction; the models avoid dialect-specific types
  (`JSON` not `JSONB`, no `ARRAY`), so Postgres is a `DATABASE_URL` change plus
  adding `asyncpg`.
