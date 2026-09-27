# The public demo

The dashboard, deployed with no backend behind it, for Product Hunt.

Everything the app asks the API for is answered from a static snapshot of the
real board. Your local setup is untouched: `npm run dev` still proxies to
`127.0.0.1:8000` exactly as before, and none of this loads unless `VITE_DEMO_JOB=1`.

---

## How it works

One interceptor, installed below the app rather than inside it.

```
main.tsx ──(VITE_DEMO_JOB=1 only)──> src/demo/install.ts ──> wraps window.fetch
                                                             │
   every component, hook and query is untouched              ▼
   and never learns the demo exists              src/demo/router.ts
                                                             │
                                          ┌──────────────────┴─────────────────┐
                                          ▼                                    ▼
                            public/demo/*.json                        src/demo/state.ts
                        (captured from the real API)          (what the visitor changes,
                                                                    in localStorage)
```

`window.fetch` is the interception point because `lib/api.ts` is the only place
the dashboard makes requests — so wrapping the layer _below_ it means no
component, type or query key is edited. An endpoint added to `api.ts` later
keeps working here for free.

| File                             | What it does                                           |
| -------------------------------- | ------------------------------------------------------ |
| `backend/scripts/export_demo.py` | Captures the snapshot from the real API                |
| `frontend/src/demo/snapshot.ts`  | Loads the JSON, lazily for the big parts               |
| `frontend/src/demo/query.ts`     | Port of `app/job_filters.py` — filters, sorts, funnels |
| `frontend/src/demo/router.ts`    | Answers all ~34 endpoints                              |
| `frontend/src/demo/fake/`        | Stands in for the four LLM surfaces (below)            |
| `frontend/src/demo/pdf.ts`       | Lays generated documents out as a PDF, by hand         |
| `frontend/public/demo-sw.js`     | Serves those PDFs to the preview iframe                |
| `frontend/src/demo/state.ts`     | Applications, prefs, hand edits, verdicts, documents   |
| `frontend/src/demo/overlay.tsx`  | Demo bar + waitlist, in its own React root             |
| `frontend/vercel.json`           | SPA rewrite + the PDF rewrite                          |

The only edit to pre-existing code is a guarded block in `src/main.tsx` and a
`--mode demo` plugin in `vite.config.ts`. Both are inert without the flag.

---

## Regenerating the snapshot

```bash
cd backend
.venv/Scripts/python -m scripts.export_demo          # full: ~3 min with PDFs
.venv/Scripts/python -m scripts.export_demo --no-pdf # faster, skips Tectonic
```

It clones `jobs.db` through SQLite's backup API and works on the copy, so it
**cannot write to your live database**. It runs no lifespan, so the scheduler
never starts and no job board is contacted.

What it writes into `frontend/public/demo/`:

| File             | Size    | Contents                                            |
| ---------------- | ------- | --------------------------------------------------- |
| `jobs.json`      | ~12 MB  | all 11,775 open postings, in the `/jobs` list shape |
| `jd.json`        | ~9.7 MB | 2,004 full descriptions (lazily loaded)             |
| `matches.json`   | ~1.9 MB | 970 scored rows with verdicts                       |
| `meta.json`      | ~250 KB | facets, dashboard, prefs, profile, funnels          |
| `documents.json` | ~230 KB | 10 tailored documents, their diffs and chats        |
| `pdf/*.pdf`      | ~280 KB | the compiled PDFs                                   |

Roughly **2.7 MB over the wire** once gzipped, and the two big files load
lazily.

> **Repo size:** each re-export rewrites ~24 MB of JSON. Git keeps every
> version, so re-exporting often will grow the repo fast. On the `demo` branch,
> prefer `git commit --amend` when refreshing a snapshot you have not pushed.

### Identity

The demo ships the **real candidate** — real name, real résumé, real employment
history, real email, real LinkedIn and GitHub. That is deliberate: it is your
own portfolio, and a tailored résumé belonging to a fictional person
demonstrates the feature while proving nothing about the person launching it.

**The phone number is the one redaction**, replaced with `+91-XXXXX-XXXXX`.
The reasoning is asymmetry rather than secrecy: nobody browsing a launch page
needs to call, so publishing it buys nothing, while a mobile number sitting in
ten downloadable PDFs is an OTP, WhatsApp-scam and SIM-swap target and PDFs are
the most harvestable format there is. Anyone who wants to make contact has the
email.

The substitution runs over every captured payload including the LaTeX, the
PDFs are compiled from the substituted source, and **the export aborts** if the
digits survive anywhere — in the JSON, in a document, or in a compiled PDF.
Verified on every run.

> Changing your mind is one edit: `PERSONA_IDS` in `scripts/export_demo.py`,
> then re-export. `PERSONA_NAMES` is an explicit empty mapping so the two-tier
> scrub (identifiers everywhere, bare names in the profile trees only) is one
> edit away from being reinstated if you ever want a fictional persona back.
>
> If you do reinstate names, note what the first version got wrong: running
> bare surnames over the job data renamed a "Kumar" inside two employers'
> descriptions. These are Indian job boards. Scrub names in the profile trees
> only.

Remotive's rows are dropped: its terms forbid reposting its listings to third
parties. Remote OK and Jobicy attribution stays visible in the UI.

---

## Running it locally

```bash
cd frontend
npm run dev:demo       # demo mode, hot reload
npm run build:demo     # production build into dist/
npm run preview:demo   # serve that build
```

`npm run dev` and `npm run build` are unchanged and never touch any of this.

---

## Deploying to Vercel (free)

1. Push the branch: `git push -u origin demo`
2. **vercel.com → Add New → Project → import the repo**
3. Settings:
   - **Root Directory:** `frontend`
   - **Framework Preset:** Vite
   - **Build Command:** `npm run build:demo` ← _not the default `npm run build`_
   - **Output Directory:** `dist`
   - **Production Branch:** `demo` (Settings → Git), so `main` never deploys
4. **Environment Variables:** `VITE_WAITLIST_URL` = your Apps Script URL (below)
5. Deploy.

The Hobby plan is free and covers this — a static SPA with no serverless
functions. Note its terms are for non-commercial use, which a launch demo is.

---

## The waitlist

Emails land as rows in a Google Sheet. No account beyond your Google one, no
serverless function, no submission cap.

1. Create a Sheet. First row: `timestamp | email | note | source | referrer`
2. **Extensions → Apps Script**, paste this, replacing the existing code:

```javascript
function doPost(e) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  let body = {};
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    body = {};
  }

  const email = String(body.email || '')
    .trim()
    .slice(0, 254);
  if (!email || email.indexOf('@') === -1) {
    return ContentService.createTextOutput('bad email');
  }

  sheet.appendRow([
    new Date(),
    email,
    String(body.note || '').slice(0, 2000),
    String(body.source || ''),
    String(body.referrer || ''),
  ]);
  return ContentService.createTextOutput('ok');
}
```

3. **Deploy → New deployment → Web app**
   - _Execute as:_ **Me**
   - _Who has access:_ **Anyone**
4. Copy the `/exec` URL into Vercel as `VITE_WAITLIST_URL`, and redeploy.

**Why the request looks the way it does:** Apps Script does not answer CORS
preflight, so the form posts `Content-Type: text/plain;charset=utf-8` — a
safelisted value that avoids a preflight. Apps Script still reads the JSON from
`e.postData.contents`. The consequence is that the response is not readable, so
success is inferred from the request not throwing: a _network_ failure is
reported to the visitor, a server-side one is not. With `VITE_WAITLIST_URL`
unset, the form says it is not wired up rather than silently dropping addresses.

Check it works by submitting once and looking at the Sheet.

---

## What the demo does instead of calling an LLM

Reads are answered from the snapshot. Writes split three ways: mutations that
are genuinely local are applied, the four surfaces that would cost an LLM call
are **stood in for** (`src/demo/fake/`), and what is left is refused with a
message saying what it would cost.

| Action                                | Demo behaviour                                           |
| ------------------------------------- | -------------------------------------------------------- |
| Browse, filter, sort, funnels, facets | Real, recomputed from the snapshot                       |
| Apply / change stage / delete         | Applied, in `localStorage`, idempotent                   |
| Edit preferences, Send to Matches     | Applied, in `localStorage`                               |
| Open a stored résumé or cover letter  | Real — 10 of them ship, with diffs and chat              |
| Revert a document                     | Real: replays the stored tailoring                       |
| Recompile an **unedited** document    | Succeeds — the exported PDF _is_ that compile            |
| **Deep read** (15/50 jobs, or one)    | Stand-in: paced pass, composed verdicts, `localStorage`  |
| **Tailor a résumé**                   | Stand-in: the base template re-ordered against the JD    |
| **Cover letter**                      | Stand-in: assembled from profile-true sentences          |
| **Document chat**                     | Stand-in: re-order / drop proposals, fact check intact   |
| Recompile a **hand-edited** document  | Laid out in the browser — not LaTeX, so it looks plainer |
| Profile upload                        | Refused, names the cost                                  |
| Refresh (`r`)                         | Replays a fabricated sweep through the real sync screen  |

### What "stand-in" means here

Each one composes an answer out of data the real pipeline already produced,
rather than writing new claims. None of them calls a model, and none reports
that one was called — a composed verdict carries `model: "demo-stand-in"`, and a
generated document is stored with `llm_used: false`.

- **The deep read** (`fake/screen.ts`) takes the rows the shortlist really
  selected, best score first, and builds each verdict from that row's own
  `matched_skills`, `missing_stacks`, `years_required` and blockers, with the
  strengths phrased from `profile.yaml`'s `evidence:` lines. The band starts at
  the deterministic band and steps down when the posting names stacks the
  profile cannot show, which is the disagreement the real model produces most
  often. It commits a row at a time, so closing the panel mid-pass keeps the
  reads that had already happened, and the Dashboard's token band moves with it.
- **The résumé** (`fake/documents.ts`) is the base template with its bullets
  re-ordered against the posting's keywords and the least relevant one dropped,
  plus the skills rows re-sorted. **Nothing is rewritten** — CLAUDE.md's rule
  that the generator may only select, re-order and re-word is easy to keep when
  the stand-in cannot write a sentence at all. The diff against base is computed
  from that same re-ordering, so `moved` / `dropped` are exact.
- **The cover letter** picks from a fixed bank of sentences the profile already
  states, gated on what the posting names; the header, date, addressee and
  sign-off are rendered from `profile.yaml` exactly as `app/cover_letter.py`
  does.
- **Chat** proposes re-orders, drops and sentence trims — never new prose — and
  **keeps the fact check**, which is the point of the panel: ask it for AWS or
  Kubernetes and it refuses and names the `gaps:` entry, because the denylist it
  reads is the real one out of the snapshot's profile.

### The preview, and why there is a service worker

A generated document has no exported PDF, and nothing in a browser runs LaTeX.
`src/demo/pdf.ts` is a ~400-line PDF writer (base-14 Helvetica, WinAnsi, no
compression, no dependency) that lays the document out from its own structure.
It is not a LaTeX renderer and does not try to look like Tectonic's output.

Getting those bytes into the preview needs one more piece: the preview is an
`<iframe src="/api/documents/3/pdf">`, and an iframe is a navigation, so the
fetch shim never sees it. `public/demo-sw.js` is a service worker that answers
exactly that path from a Cache Storage entry the page fills, and falls through
to the network for everything else — so the ten exported PDFs keep coming from
`vercel.json`'s rewrite untouched. Where a service worker cannot run (a private
window, blocked site data), a generated document's compile answers `ok: false`
and says so rather than showing an empty pane.

---

## Verified

Driven in a real browser (Playwright) against the production build:

- all four views render, no uncaught errors anywhere
- the funnel's last step equals the table total (1,803 = 1,803)
- keyword filtering narrows 1,803 → 735
- applying records, stays idempotent, survives a reload
- the Tailor modal loads 7,086 chars of stored LaTeX, its PDF serves 29,711
  real bytes through the rewrite, and the Diff and Chat tabs render
- the phone number appears in no JSON, no LaTeX and none of the 10 PDFs, while
  the real name, email and links are intact

Re-run with `scratchpad/verify_all.py` against `npm run preview:demo`.

The stand-ins were driven through `router.handle` directly, in Node with
`localStorage`, `caches` and `navigator.serviceWorker` stubbed — the wiring is
what breaks, not the composition:

- a 5-job deep read walks `running → done`, commits a verdict per tick, and the
  rows come back `llm_read` with a band; the estimate re-prices from 23 cached
  to 28 and the Dashboard's `tokens_today` moves by the same 9,815
- `POST /matches/{id}/document` generates a 7,039-char résumé (from a
  7,252-char template), its diff reports 9 moved / 1 dropped / 3 unchanged, and
  compiling puts 14,463 real PDF bytes in the cache the worker reads
- the cover letter addresses the right company, and `% --- BODY` survives every
  chat edit
- chat proposes a re-order, applying it marks the document hand-edited, Revert
  restores it byte-for-byte, and "Add Kubernetes and AWS" comes back blocked
  naming both `gaps:` entries
- a stored document still compiles to its exported PDF and caches nothing

---

## Known limits

- **Full-JD keyword search** only sees the 2,004 descriptions the snapshot
  carries. `query.ts` knows this in one place, so the list and the funnel agree
  with each other either way.
- **Saving the profile** is accepted but does not bump `profile_version` — a
  bump would correctly mark every stored score and document stale and empty the
  Matches list.
- **`?job=` on a posting outside the loaded page** cannot open the Tailor modal
  from the drawer. That is the app's own behaviour, not the demo's: the drawer
  looks the match up in the currently loaded page.
- **Headless Chromium renders no PDF**, so the preview pane looks blank under
  Playwright. The bytes are served correctly; check it in a real browser.
- **A generated document's PDF is laid out, not compiled.** It is a real PDF
  and it carries the document's real content, but base-14 Helvetica next to
  Tectonic's Computer Modern is a visible difference if you open a generated
  résumé straight after one of the ten exported ones. The same applies the
  first time you hand-edit a *stored* document and recompile: the preview
  switches from the exported file to the browser's rendering, and Revert
  switches it back.
- **The stand-ins do not write prose.** They select, re-order and drop. So chat
  will not reword a bullet for you, and a second deep read of the same job
  returns the same verdict — both are honest about what a browser can do, and
  both are less than the real feature.
- **Generated documents are capped at 12** per browser (`MAX_GENERATED` in
  `state.ts`), oldest evicted, so a long session cannot fill the
  `localStorage` quota with LaTeX.
