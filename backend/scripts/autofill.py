"""Fill a job application in a visible browser, then hand it to you.

    python -m scripts.autofill applications/jeeves-senior-ai-engineer.yaml

Opens the form, uploads the base résumé, fills standard fields from
`profile.yaml` and custom questions from the answers file, outlines what needs
you (red) and what was drafted (amber), writes an audit log, and waits.

It NEVER submits. You review, edit, solve the captcha if one appears, and
click Submit yourself. Closing the browser window ends the script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from app.autofill.lever import LeverFiller  # noqa: E402
from app.profile import load_profile  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
FILLERS = {"jobs.lever.co": LeverFiller}


async def main(spec_path: Path) -> int:
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    url = spec["url"]
    host = urlparse(url).hostname or ""
    filler_cls = FILLERS.get(host)
    if filler_cls is None:
        print(f"No filler for {host}. Supported: {', '.join(FILLERS)}")
        return 2

    profile = load_profile(BACKEND / "profile.yaml")
    resume_rel = profile.resume_files.get("base")
    resume = (BACKEND / resume_rel) if resume_rel else None
    if resume and not resume.exists():
        print(f"warning: résumé not found at {resume}")
        resume = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.goto(url, wait_until="domcontentloaded")

        filler = filler_cls(page, profile, resume)
        report = await filler.fill(spec.get("answers") or {}, set(spec.get("drafts") or []))

        log_dir = BACKEND / "applications" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = log_dir / f"{stamp}-{spec_path.stem}.json"
        log_path.write_text(
            json.dumps(
                {
                    "job": spec.get("job"),
                    "url": url,
                    "ats": filler.ats,
                    "profile_version": profile.version,
                    "filled_at": stamp,
                    "submitted": "by hand, not recorded",
                    **report.__dict__,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print(f"\nFilled {len(report.filled)} fields. Audit log: {log_path.relative_to(BACKEND)}")
        for title, items in (
            ("NEEDS YOU (red outline)", report.needs_you),
            ("OPTION NOT FOUND (red outline) — fix the answers file", report.unmatched),
            ("DRAFTED — read before submitting (amber outline)", report.drafts),
        ):
            if items:
                print(f"\n{title}:")
                for item in items:
                    print(f"  - {item}")
        print("\nNothing has been submitted. Review the form, then click Submit yourself.")
        print("Close the browser window when you're done.")

        await page.wait_for_event("close", timeout=0)
        await browser.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", type=Path, help="answers file, e.g. applications/<job>.yaml")
    sys.exit(asyncio.run(main(parser.parse_args().spec)))
