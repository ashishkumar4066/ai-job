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
| `frontend/src/demo/state.ts`     | Applications, prefs, hand edits (localStorage)         |
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

## What the demo refuses, and why

Reads are answered from the snapshot. Writes split in two: mutations that are
genuinely local are applied, and anything that would cost an LLM call is
refused with a message saying so. A demo whose Apply button does nothing reads
as broken; a demo that pretends a deep read ran is claiming something false.

| Action                                  | Demo behaviour                                          |
| --------------------------------------- | ------------------------------------------------------- |
| Browse, filter, sort, funnels, facets   | Real, recomputed from the snapshot                      |
| Apply / change stage / delete           | Applied, in `localStorage`, idempotent                  |
| Edit preferences, Send to Matches       | Applied, in `localStorage`                              |
| Open a stored résumé or cover letter    | Real — 10 of them ship, with diffs and chat             |
| Revert a document                       | Real: replays the stored tailoring                      |
| Recompile an **unedited** document      | Succeeds — the exported PDF _is_ that compile           |
| Recompile a **hand-edited** document    | Refused: no Tectonic in a browser                       |
| Tailor / generate a new document        | Refused, points at the 10 that exist                    |
| Deep read, chat message, profile upload | Refused, names the cost                                 |
| Refresh (`r`)                           | Replays a fabricated sweep through the real sync screen |

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
