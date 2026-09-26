"""Export a static snapshot of the live board for the public demo build.

Why this exists
---------------
The Product Hunt demo is the React app with no backend behind it: a fetch shim
in `frontend/src/demo/` answers every API call from JSON files this script
writes into `frontend/public/demo/`. Hand-writing that JSON would mean
re-deriving ~34 response shapes by hand, and any drift between a hand-written
facet count and the row list makes the dashboard look broken. So this script
does not build responses — it **calls the real API in-process** and records what
it answered. The shapes cannot drift because they are the shapes.

Three safety properties, in order of how much they matter:

1. **It cannot write to the live database.** The DB is copied through SQLite's
   backup API (which picks up the WAL) into a scratch file, and `DATABASE_URL`
   is pointed at the copy *before* `app.config` is imported, so the lru_cached
   settings never see the real path.
2. **It runs no lifespan.** `httpx.ASGITransport` does not trigger startup, so
   `init_db()` never runs `create_all` and the APScheduler sweep never starts.
   A snapshot export must not contact a single job board.
3. **Only GET routes are called.** The write and spend routes (`/ingest/*`,
   `/matches/llm`, `/matches/score`) are synthesized by the shim instead.

Identity
--------
`profile.yaml` holds a real person's name, email and phone. Those must not ship
in a public JS bundle, so every captured payload — including the LaTeX of the
tailored documents — goes through `PERSONA` substitution, and the PDFs are
recompiled from the substituted source. The substitution runs over the whole
tree as a last step, so a field added to the profile later is covered without
this script knowing about it.

Usage
-----
    python -m scripts.export_demo               # full export
    python -m scripts.export_demo --no-pdf      # skip Tectonic (faster)
    python -m scripts.export_demo --jd-limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
OUT_DIR = REPO_ROOT / "frontend" / "public" / "demo"

# ---------------------------------------------------------------------------
# What gets redacted before the snapshot ships.
#
# The demo shows the REAL candidate: real name, real résumé, real employment
# history, real email. That is deliberate — it is the author's own portfolio,
# and a tailored résumé belonging to a fictional person demonstrates the
# feature while proving nothing about the person launching it.
#
# The phone number is the one exception, and the reasoning is asymmetry rather
# than secrecy: nobody browsing a launch page needs to call, so publishing it
# buys nothing, while a mobile number in ten downloadable PDFs is an OTP,
# WhatsApp-scam and SIM-swap target that is trivially harvested. A recruiter
# who wants to make contact has the email.
#
# Longest needle first, so "+91-8789777738" is replaced before the bare digits
# could match inside it and leave a mangled "+91-" prefix behind.
# ---------------------------------------------------------------------------
PHONE_PLACEHOLDER = "+91-XXXXX-XXXXX"

PERSONA_IDS: dict[str, str] = {
    "+91-8789777738": PHONE_PLACEHOLDER,
    # The bare digits, in case a field stores them without the country prefix.
    "8789777738": "XXXXXXXXXX",
}

# Nothing else is substituted. Kept as an explicit empty mapping rather than
# deleted, so the two-tier scrub below (identifiers everywhere, names in the
# profile trees only) still reads as a deliberate choice and is one edit away
# from being reinstated.
PERSONA_NAMES: dict[str, str] = {}

PERSONA: dict[str, str] = {**PERSONA_IDS, **PERSONA_NAMES}

# A backstop over every tree, independent of the substitution list above.
#
# It exists because the list can only replace spellings someone thought of. An
# earlier run leaked `linkedin.com/in/ashish-kumar` — a display string matching
# none of the URL rules — into a compiled PDF while the equality check reported
# clean. Only the phone is listed now, since it is the only redacted item, and
# ten digits cannot occur in a job posting by coincidence.
FORBIDDEN_STEMS: tuple[str, ...] = ("8789777738",)

# Sources whose terms forbid republishing their listings on a third-party site.
# Remotive's terms are explicit about it; the rest of the board is titles and
# company names, which every aggregator reproduces.
EXCLUDED_ATS: frozenset[str] = frozenset({"remotive"})

PAGE = 500  # the API's own `limit` ceiling

# The live `prefs.yaml` carries whatever scope was last sent from Jobs — on the
# day this was written, `posted_within_days: 3`, which bounds `/matches` to 137
# rows. That is the right answer for its owner and a thin demo for everyone
# else, so the export runs against a copy widened to the hard 30-day ceiling.
# It is a *widening*, not a different rule: the gate, the bands and the
# shortlist all still run exactly as configured.
DEMO_TRANSFER_DAYS = 30


def log(message: str) -> None:
    print(f"  {message}", flush=True)


# ---------------------------------------------------------------------------
# Step 1 — a throwaway copy of the database
# ---------------------------------------------------------------------------


def clone_database(source: Path, dest: Path) -> None:
    """Copy `source` to `dest` through the backup API.

    A plain file copy would miss anything still sitting in the WAL, which on a
    live board is the most recent sweep. `mode=ro` means this cannot acquire a
    write lock on the real file even briefly.
    """
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def widen_prefs(source: Path, dest: Path) -> None:
    """Copy `prefs.yaml`, widening the transferred scope to the 30-day ceiling.

    Nothing is written back to the real file — `PREFS_FILE` points the export at
    this copy, and the copy is what ships as the demo's stored preferences.
    """
    if not source.exists():
        return
    import yaml  # noqa: PLC0415 - only needed on this path

    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    transfer = data.get("transfer")
    if isinstance(transfer, dict) and isinstance(transfer.get("filters"), dict):
        transfer["filters"]["posted_within_days"] = DEMO_TRANSFER_DAYS
        transfer.pop("count_at_transfer", None)
    freshness = data.get("freshness")
    if isinstance(freshness, dict):
        freshness["max_age_days"] = DEMO_TRANSFER_DAYS
    dest.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 2 — capture
# ---------------------------------------------------------------------------


class Capture:
    """Records what the real API answers for a set of GET requests."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.client.get(path, params=params or {})
        if response.status_code != 200:
            raise RuntimeError(f"GET {path} {params or ''} -> {response.status_code} {response.text[:200]}")
        return response.json()

    async def maybe(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Same, but a non-200 yields None instead of raising.

        Used for routes that legitimately have nothing to answer on this
        machine — no résumé template vendored, no document generated yet.
        """
        try:
            response = await self.client.get(path, params=params or {})
        except Exception as exc:  # noqa: BLE001 - a missing optional is not fatal
            log(f"skip {path}: {exc}")
            return None
        if response.status_code != 200:
            log(f"skip {path}: HTTP {response.status_code}")
            return None
        return response.json()

    async def page(self, path: str, params: dict[str, Any], cap: int | None = None) -> list[dict]:
        """Walk `limit`/`offset` until the reported total is collected."""
        out: list[dict] = []
        offset = 0
        while True:
            body = await self.get(path, {**params, "limit": PAGE, "offset": offset})
            items = body.get("items", [])
            out.extend(items)
            total = body.get("total", len(out))
            offset += PAGE
            if not items or offset >= total or (cap and len(out) >= cap):
                break
        return out[:cap] if cap else out


async def capture_jobs(cap: Capture) -> list[dict]:
    """Every open posting, in the `/jobs` list shape.

    All of them, not a sample: the facet counts, the funnel steps and the
    header stats are all recomputed from these rows by the shim, so a sample
    would make every number on screen a lie about a board this size.
    """
    rows = await cap.page("/jobs", {"status": "open", "sort": "first_seen_at", "order": "desc"})
    kept = [r for r in rows if r.get("ats") not in EXCLUDED_ATS]
    log(f"jobs: {len(rows)} open, {len(kept)} kept ({len(rows) - len(kept)} dropped on source terms)")
    return kept


async def capture_details(cap: Capture, ids: list[int]) -> dict[str, dict]:
    """Full JD text for the rows a visitor can actually open.

    `raw_json` is dropped: it is the adapter's untouched upstream payload, the
    largest field on the row, and nothing in the UI reads it except the debug
    block.
    """
    out: dict[str, dict] = {}
    for index, job_id in enumerate(ids, 1):
        body = await cap.get(f"/jobs/{job_id}")
        out[str(job_id)] = {
            "description_html": body.get("description_html"),
            "description_text": body.get("description_text"),
            "llm_validity": body.get("llm_validity"),
            "raw_json": None,
        }
        if index % 250 == 0:
            log(f"  details {index}/{len(ids)}")
    return out


async def capture_meta(cap: Capture) -> dict[str, Any]:
    """The payloads the shim serves verbatim rather than recomputing."""
    meta: dict[str, Any] = {}

    # Facets are captured per gate state because the dashboard's default view
    # is gated on eligibility, and a facet list built without the gate offers
    # companies whose every posting is filtered out.
    meta["facets"] = {
        "eligible": await cap.get("/meta/facets", {"status": "open", "eligibility_pass": "true"}),
        "all": await cap.get("/meta/facets", {"status": "open"}),
    }

    meta["matches_funnel"] = await cap.maybe("/matches/funnel")
    meta["dashboard"] = await cap.maybe("/dashboard")
    meta["prefs"] = await cap.maybe("/prefs")
    meta["profile"] = await cap.maybe("/profile")
    meta["profile_document"] = await cap.maybe("/profile/document")
    meta["base_resume"] = await cap.maybe("/documents/base")
    meta["health"] = await cap.maybe("/health")
    meta["llm_status"] = await cap.maybe("/matches/llm/status")
    meta["pipeline_status"] = await cap.maybe("/matches/run/status")
    meta["ingest_status"] = await cap.maybe("/ingest/status")
    meta["companies"] = await cap.maybe("/companies")
    # Seeds the demo's application tracker, so the Dashboard panel opens with
    # something in it rather than an empty state a visitor has to populate.
    meta["applications"] = await cap.maybe("/applications")

    # The confirm dialog re-prices the deep read for each offered cap.
    meta["llm_estimate"] = {}
    for limit in (10, 15, 20, 50):
        priced = await cap.maybe("/matches/llm/estimate", {"limit": limit})
        if priced is not None:
            meta["llm_estimate"][str(limit)] = priced

    return meta


async def capture_matches(cap: Capture) -> list[dict]:
    """Every scored row, gate misses included.

    `match_prefs=false` is what makes the demo's preference panel meaningful:
    the shim re-applies the gate in JS, so toggling a preference visibly moves
    rows in and out the way it does against the real backend.
    """
    rows = await cap.page(
        "/matches",
        {"match_prefs": "false", "sort": "score", "order": "desc"},
    )
    log(f"matches: {len(rows)} scored rows")
    return rows


def document_targets(db: Path) -> list[tuple[int, str]]:
    """The (job_id, kind) pairs that actually have a stored document.

    Read from the table rather than probed through the API: asking
    `/matches/{id}/document` for all 11,775 rows meant 23,550 requests to find
    10 documents, and the 404s drowned the export's own log.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select distinct job_id, kind from generated_documents order by job_id, kind"
        ).fetchall()
    finally:
        conn.close()
    return [(int(job_id), str(kind)) for job_id, kind in rows]


async def capture_documents(cap: Capture, targets: list[tuple[int, str]]) -> dict[str, Any]:
    """Stored résumés and cover letters, with their diffs and chat threads.

    A 404 is still possible and still normal: `/matches/{id}/document` answers
    only for the *current* profile version, so a document generated against an
    older profile is correctly absent.
    """
    documents: dict[str, Any] = {}
    by_id: dict[str, Any] = {}
    diffs: dict[str, Any] = {}
    chats: dict[str, Any] = {}

    for job_id, kind in targets:
        body = await cap.maybe(f"/matches/{job_id}/document", {"kind": kind})
        if not body:
            continue
        documents[f"{job_id}:{kind}"] = body
        doc_id = body.get("id")
        if doc_id is None:
            continue
        by_id[str(doc_id)] = body
        diff = await cap.maybe(f"/documents/{doc_id}/diff")
        if diff is not None:
            diffs[str(doc_id)] = diff
        chat = await cap.maybe(f"/documents/{doc_id}/chat")
        if chat is not None:
            chats[str(doc_id)] = chat
        log(f"document {doc_id}: job {job_id} {kind}")

    return {"byKey": documents, "byId": by_id, "diffs": diffs, "chats": chats}


# ---------------------------------------------------------------------------
# Step 3 — scrub
# ---------------------------------------------------------------------------


def scrub(value: Any, rules: dict[str, str]) -> Any:
    """Replace the real identity with the demo persona.

    Applied to a whole subtree rather than to named fields, because the name also
    appears inside generated LaTeX, inside cover-letter prose, inside chat
    transcripts and inside `apply_url` query strings on some boards. A
    field-by-field scrub would miss at least one of those.

    Longest needle first, so "krashish1350@gmail.com" is replaced before a bare
    "krashish1350" could match inside it and leave a broken tail behind.
    """
    ordered = sorted(rules.items(), key=lambda kv: -len(kv[0]))

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            for needle, replacement in ordered:
                node = node.replace(needle, replacement)
            return node
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node

    return walk(value)


def assert_clean(tree: Any, rules: dict[str, str], label: str, *, stems: bool = False) -> None:
    """Fail the export if any real identifier survived the scrub.

    This is the check that matters most in the whole script: a public bundle
    carrying a real phone number cannot be taken back once it is deployed, so a
    leak has to stop the export rather than warn in a log nobody reads.

    `stems=True` adds the case-insensitive sweep described at `FORBIDDEN_STEMS`,
    and is passed for the trees built from `profile.yaml`.
    """
    blob = json.dumps(tree)
    leaked = [needle for needle in rules if needle in blob]
    if stems:
        lowered = blob.lower()
        leaked += [f"~{stem}" for stem in FORBIDDEN_STEMS if stem in lowered]
    if leaked:
        raise SystemExit(f"ABORT — real identity survived the scrub in {label}: {leaked}")


# ---------------------------------------------------------------------------
# Step 4 — PDFs
# ---------------------------------------------------------------------------


async def compile_pdfs(by_id: dict[str, Any], out_dir: Path) -> dict[str, str]:
    """Compile each scrubbed document to a PDF served as a static file.

    Tectonic cannot run on Vercel, so the compile happens here and a rewrite in
    `vercel.json` maps `/api/documents/:id/pdf` onto these files. The LaTeX is
    the scrubbed LaTeX, so the PDF shows the persona too — compiling before the
    scrub would have put the real name in an opaque binary where the
    `assert_clean` string check could never see it.
    """
    from app.latex import LatexUnavailable, compile_tex  # noqa: PLC0415 - heavy

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for doc_id, doc in by_id.items():
        tex = doc.get("tex")
        if not tex:
            continue
        try:
            result = await compile_tex(tex)
        except LatexUnavailable as exc:
            log(f"pdf: no Tectonic ({exc}) — skipping all PDFs")
            break
        if not result.ok or not result.pdf:
            log(f"pdf {doc_id}: failed — {'; '.join(result.errors[:2])}")
            continue
        (out_dir / f"{doc_id}.pdf").write_bytes(result.pdf)
        written[doc_id] = f"/demo/pdf/{doc_id}.pdf"
        log(f"pdf {doc_id}: {len(result.pdf) // 1024} KB in {result.duration_s:.1f}s")
    return written


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def rerank(force: bool) -> None:
    """Run the free pipeline against the widened preferences.

    Writes — but only to the throwaway copy, which is the point of cloning it.

    Two reasons this runs, and both are about the snapshot being internally
    consistent rather than about refreshing data:

    1. `widen_prefs` changes the preference set, so `prefs_version` on every
       stored verdict stops matching the preferences being shipped, and the
       Matches funnel correctly announces "preferences changed since the last
       run" on a demo where the visitor has changed nothing.

    2. The captured `/matches/run/status` is whatever the tracker last held. On
       a machine where no run happened today that is `stage: "idle"` with every
       field null — so the demo's Run button had nothing to replay and appeared
       to do nothing at all. Running the pipeline here leaves the tracker
       holding a genuinely completed run, with real validity counts, real
       ranking counts and a real deep-read estimate.

    It is the whole pipeline (validity, then ranking, then pricing the deep
    read) and it stops there, exactly as `POST /matches/run` does. Free and
    offline: no tokens, no network.
    """
    from app.db import session_scope  # noqa: PLC0415 - after env setup
    from app.pipeline import run_pipeline, tracker  # noqa: PLC0415

    async with session_scope() as session:
        # Into the module-level tracker, because that is the object
        # `GET /matches/run/status` reads and therefore what gets captured.
        progress = await run_pipeline(session, force=force, progress=tracker.progress)

    ranking = progress.ranking
    log(
        f"pipeline: stage={progress.stage} "
        f"scored={getattr(ranking, 'scored', 0)} "
        f"shortlisted={getattr(ranking, 'shortlisted', 0)}"
    )
    if progress.error:
        log(f"pipeline error (captured as-is): {progress.error}")


async def export(args: argparse.Namespace) -> None:
    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415 - after env setup

    from app.main import create_app  # noqa: PLC0415 - after env setup

    if not args.no_rerank:
        log("re-ranking against the widened preferences…")
        await rerank(force=args.force_rerank)

    app = create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://demo-export") as client:
        cap = Capture(client)

        log("capturing jobs…")
        jobs = await capture_jobs(cap)

        log("capturing matches…")
        matches = await capture_matches(cap)

        # Which descriptions to ship.
        #
        # Not simply "the newest N eligible rows": the jobs carrying a tailored
        # résumé are among the OLDEST in the board, so a newest-first cut left
        # every document's own posting showing "description not in the snapshot"
        # — in the drawer a visitor is most likely to open, because it is the one
        # with the flagship feature attached. Documented and scored rows are
        # therefore pinned ahead of the budget rather than competing for it.
        documented = {job_id for job_id, _ in document_targets(args.db_copy)}
        scored = {row["job"]["id"] for row in matches}
        eligible = [j["id"] for j in jobs if j.get("eligibility_pass")]

        pinned = [i for i in eligible if i in documented or i in scored]
        rest = [i for i in eligible if i not in documented and i not in scored]
        detail_ids = pinned + rest[: max(0, args.jd_limit - len(pinned))]

        missing = (documented | scored) - set(detail_ids)
        if missing:
            # A documented job outside the eligible set would otherwise be
            # skipped silently; fetch it anyway rather than ship a blank drawer.
            detail_ids += sorted(missing)

        log(
            f"capturing {len(detail_ids)} job descriptions "
            f"({len(pinned)} pinned: {len(documented)} documented, {len(scored)} scored)…"
        )
        details = await capture_details(cap, detail_ids)

        log("capturing meta payloads…")
        meta = await capture_meta(cap)

        log("capturing documents…")
        documents = await capture_documents(cap, document_targets(args.db_copy))

    log("scrubbing identity…")
    # Job data gets the unambiguous identifiers only; the profile and the
    # documents get those plus the bare names. See PERSONA_NAMES on why.
    jobs = scrub(jobs, PERSONA_IDS)
    details = scrub(details, PERSONA_IDS)
    matches = scrub(matches, PERSONA_IDS)
    meta = scrub(meta, PERSONA)
    documents = scrub(documents, PERSONA)

    # The stem sweep now runs over every tree, not just the profile ones. It
    # only looks for the phone digits, which — unlike a name — cannot appear in
    # an employer's job description by coincidence, so there is no false
    # positive to trade against the extra coverage.
    for tree, label in (
        (jobs, "jobs"),
        (details, "jd"),
        (matches, "matches"),
        (meta, "meta"),
        (documents, "documents"),
    ):
        assert_clean(tree, PERSONA, label, stems=True)

    pdfs: dict[str, str] = {}
    if not args.no_pdf and documents["byId"]:
        log("compiling PDFs…")
        pdfs = await compile_pdfs(documents["byId"], OUT_DIR / "pdf")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {
            "jobs": len(jobs),
            "eligible": sum(1 for j in jobs if j.get("eligibility_pass")),
            "descriptions": len(details),
            "matches": len(matches),
            "documents": len(documents["byId"]),
        },
        "excluded_ats": sorted(EXCLUDED_ATS),
        "pdfs": pdfs,
    }

    files = {
        "manifest.json": manifest,
        "jobs.json": jobs,
        "jd.json": details,
        "matches.json": matches,
        "meta.json": meta,
        "documents.json": documents,
    }
    for name, payload in files.items():
        path = OUT_DIR / name
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        log(f"wrote {name} ({path.stat().st_size / 1048576:.2f} MB)")

    print("\nSnapshot written to frontend/public/demo/")
    print(json.dumps(manifest["counts"], indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jd-limit", type=int, default=2000, help="how many JDs to include")
    parser.add_argument("--no-pdf", action="store_true", help="skip Tectonic compilation")
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="skip the re-ranking pass (leaves the Matches funnel reporting stale preferences)",
    )
    parser.add_argument(
        "--force-rerank", action="store_true", help="re-score every row, not just changed ones"
    )
    parser.add_argument("--db", type=Path, default=BACKEND_ROOT / "jobs.db")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"no database at {args.db}")

    scratch = Path(tempfile.mkdtemp(prefix="demo-export-"))
    copy = scratch / "snapshot.db"
    prefs_copy = scratch / "prefs.yaml"
    args.db_copy = copy
    try:
        log(f"cloning {args.db.name} -> {copy}")
        clone_database(args.db, copy)
        widen_prefs(BACKEND_ROOT / "prefs.yaml", prefs_copy)

        # Before any `app.*` import: `get_settings` is lru_cached, so a late
        # override would be ignored and the export would read the live files.
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{copy.as_posix()}"
        os.environ["RUN_SCHEDULER"] = "false"
        if prefs_copy.exists():
            os.environ["PREFS_FILE"] = str(prefs_copy)
        sys.path.insert(0, str(BACKEND_ROOT))

        asyncio.run(export(args))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
