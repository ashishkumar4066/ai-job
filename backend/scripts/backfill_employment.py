"""Backfill `employment_type` / `workplace_type` from stored `raw_json`.

No network. Every value comes from a payload already sitting in the DB, which
is why this is a one-off script rather than a re-ingest: the adapters were
already parsing these fields and dropping them, so the data has been there all
along.

Measured coverage on the live board (open rows):

    ashby        1019/1019  (100%)   employmentType, workplaceType
    himalayas    1124/1124  (100%)   employmentType  (board is remote-only)
    wellfound     722/ 722  (100%)   jobType, remoteConfig.kind
    lever         400/ 407  ( 98%)   categories.commitment, workplaceType
    remotive       18/  18  (100%)   job_type        (board is remote-only)
    greenhouse      0/2224  (  0%)   publishes neither

Greenhouse rows stay NULL, and NULL must PASS a preference rather than fail
it — otherwise a "full-time only" preference silently drops 2,224 rows for a
fact their board never published. Same doctrine as unstated pay and unstated
years in `eligibility.py`.

Run:  python -m scripts.backfill_employment [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.logging_config import configure_logging
from app.models import JobPosting
from app.normalize import employment_type, workplace_type

log = logging.getLogger("backfill_employment")

# Where each board hides the field. A tuple is a nested path.
_EMPLOYMENT_PATHS: dict[str, tuple[str, ...]] = {
    "ashby": ("employmentType",),
    "himalayas": ("employmentType",),
    "wellfound": ("jobType",),
    "lever": ("categories", "commitment"),
    "remotive": ("job_type",),
}

_WORKPLACE_PATHS: dict[str, tuple[str, ...]] = {
    "ashby": ("workplaceType",),
    "lever": ("workplaceType",),
}

# Boards that list remote roles exclusively, so the fact is about the feed
# rather than a field any individual posting carries.
_REMOTE_ONLY_BOARDS = {"himalayas", "remotive"}


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def _wellfound_workplace(payload: dict[str, Any]) -> str | None:
    """Wellfound states work mode in `remoteConfig.kind`, not a flat field."""
    kind = _dig(payload, ("remoteConfig", "kind"))
    if isinstance(kind, str):
        upper = kind.strip().upper()
        # ONSITE_OR_REMOTE is a choice the candidate gets, so it reads remote.
        if upper in {"REMOTE", "ONSITE_OR_REMOTE"}:
            return "remote"
        if upper == "ONSITE":
            return "onsite"
    if payload.get("remote") is True:
        return "remote"
    return None


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--limit", type=int, default=None, help="process at most N rows")
    args = parser.parse_args()

    configure_logging("INFO", False)

    filled_et: Counter[str] = Counter()
    filled_wp: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    written = 0

    async with session_scope() as session:
        stmt = select(JobPosting).where(JobPosting.raw_json.is_not(None))
        if args.limit:
            stmt = stmt.limit(args.limit)
        rows = list((await session.execute(stmt)).scalars().all())
        print(f"scanning {len(rows)} rows with a stored payload\n")

        for row in rows:
            payload = row.raw_json or {}
            ats = (row.ats or "").lower()

            et_raw = _dig(payload, _EMPLOYMENT_PATHS[ats]) if ats in _EMPLOYMENT_PATHS else None
            et = employment_type(et_raw if isinstance(et_raw, str) else None)

            if ats in _WORKPLACE_PATHS:
                wp_raw = _dig(payload, _WORKPLACE_PATHS[ats])
                wp = workplace_type(wp_raw if isinstance(wp_raw, str) else None)
            elif ats == "wellfound":
                wp = _wellfound_workplace(payload)
            elif ats in _REMOTE_ONLY_BOARDS:
                wp = "remote"
            else:
                wp = None

            if et:
                filled_et[f"{ats}:{et}"] += 1
            else:
                missing[f"{ats}:employment"] += 1
            if wp:
                filled_wp[f"{ats}:{wp}"] += 1

            if not args.dry_run and (row.employment_type != et or row.workplace_type != wp):
                row.employment_type = et
                row.workplace_type = wp
                written += 1

        if args.dry_run:
            print("DRY RUN — nothing written\n")
        else:
            await session.commit()

    print("employment_type filled:")
    for key, n in filled_et.most_common():
        print(f"   {key:34} {n}")
    print("\nworkplace_type filled:")
    for key, n in filled_wp.most_common():
        print(f"   {key:34} {n}")
    print("\nleft NULL (board publishes nothing — these PASS a preference):")
    for key, n in missing.most_common():
        print(f"   {key:34} {n}")
    print(f"\nrows updated: {written}")


if __name__ == "__main__":
    asyncio.run(main())
