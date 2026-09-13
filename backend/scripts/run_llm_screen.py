"""Run the LLM deep-read pass from the terminal.

    python -m scripts.run_llm_screen --dry-run       # what would be read, and what it costs
    python -m scripts.run_llm_screen --limit 10      # a bounded first pass
    python -m scripts.run_llm_screen                 # everything routed and uncached

The terminal is the right home for the full pass. It is paced by a 8,000
token/minute ceiling, so ~400 rows takes roughly 90 minutes — far past any HTTP
timeout. `POST /matches/llm` runs the same function as a background task for
the dashboard; this is the one you can watch, interrupt and resume.

Resumable by construction: every screened row is cached on
`(job_id, profile_version, llm_content_hash)` and committed every 10 rows, so
re-running after a Ctrl-C picks up where it stopped and re-reads nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.llm import build_messages, estimate_tokens, profile_brief  # noqa: E402
from app.llm_runner import (  # noqa: E402
    LLMProgress,
    _routed_rows,
    measured_completion_tokens,
    recent_usage,
    run_llm_screen,
)
from app.ratelimit import WINDOWS  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.matching import get_weights  # noqa: E402
from app.models import JobMatch  # noqa: E402
from app.profile import get_profile  # noqa: E402


async def dry_run() -> int:
    """Price the pass without spending anything."""
    settings = get_settings()
    provider = settings.llm
    profile = get_profile()
    weights = get_weights()
    brief = profile_brief(profile)

    async with session_scope() as session:
        rows = await _routed_rows(session, profile.version, weights)
        pending = [
            (m, j)
            for m, j in rows
            if m.llm_verdict is None or not m.llm_used or m.llm_content_hash != j.content_hash
        ]
        completion = await measured_completion_tokens(
            session, provider.model, default=settings.llm_completion_reserve
        )
        total_tokens = 0
        for _match, job in pending:
            messages = build_messages(
                title=job.title,
                company=job.company,
                description_text=job.description_text,
                profile_text=brief,
                max_jd_chars=settings.llm_max_jd_chars,
            )
            total_tokens += estimate_tokens(messages, completion)

        done = (
            await session.scalar(
                select(JobMatch)
                .where(JobMatch.profile_version == profile.version, JobMatch.llm_used.is_(True))
                .with_only_columns(JobMatch.id)
                .limit(1)
            )
        ) is not None
        used = {
            name: sum(
                t
                for _at, t in await recent_usage(
                    session, provider.model, window=timedelta(seconds=WINDOWS[name][0])
                )
            )
            for name in provider.limits
            if name in ("tokens_per_day", "tokens_per_month")
        }

    headroom = settings.llm_budget_headroom
    tpm = provider.limits.get("tokens_per_minute")
    budget = int(tpm * headroom) if tpm else 0
    # Bounded by whichever short limit binds first — on Mistral that is
    # usually 1 request/second, not tokens. Excludes response latency.
    per_minute = {
        "tokens_per_minute": (total_tokens, 1),
        "requests_per_minute": (len(pending), 1),
        "requests_per_second": (len(pending), 60),
    }
    minutes = max(
        (
            amount / max(1.0, provider.limits[name] * headroom * scale)
            for name, (amount, scale) in per_minute.items()
            if name in provider.limits
        ),
        default=0.0,
    )

    print(f"provider         : {provider.name}")
    print(f"model            : {provider.model}")
    print(f"profile_version  : {profile.version}")
    print(f"routed to LLM    : {len(rows)}")
    print(f"already cached   : {len(rows) - len(pending)}")
    print(f"to read          : {len(pending)}")
    print(f"est. tokens      : {total_tokens:,}")
    print(f"limits           : {provider.limits}")
    print(f"budget           : {budget:,} tokens/min (x {settings.llm_budget_headroom} headroom)")
    print(f"est. wall clock  : {minutes:.0f} min")
    for name, spent in used.items():
        print(f"{name:<17}: {spent:,} used, this pass needs {total_tokens:,} of "
              f"{provider.limits[name]:,} — the pass stops cleanly at the cap")
    print(f"prior verdicts   : {'yes' if done else 'none yet'}")
    return 0


async def live(limit: int | None, force: bool) -> int:
    progress = LLMProgress()

    async def report() -> None:
        last = -1
        while progress.finished_at is None:
            await asyncio.sleep(15)
            if progress.done != last and progress.total:
                pct = 100 * progress.done / progress.total
                print(
                    f"  {progress.done}/{progress.total} ({pct:.0f}%)  "
                    f"tokens={progress.tokens:,}  failed={progress.failed}  "
                    f"| {progress.current or ''}"[:150]
                )
                last = progress.done

    async with session_scope() as session:
        watcher = asyncio.create_task(report())
        try:
            result = await run_llm_screen(session, force=force, limit=limit, progress=progress)
        finally:
            watcher.cancel()

    if result.error:
        print(f"ERROR: {result.error}")
        return 1

    print()
    print(f"routed         : {result.routed}")
    print(f"cached (free)  : {result.cached}")
    print(f"screened       : {result.screened}")
    print(f"failed         : {result.failed}")
    if result.skipped_budget:
        print(f"left for later : {result.skipped_budget} (request budget)")
    print(f"requests       : {result.requests}")
    print(f"tokens         : {result.tokens:,}")
    print(f"duration       : {result.duration_ms / 60000:.1f} min")
    print(f"fit bands      : {dict(sorted(result.bands.items()))}")
    print(f"posting status : {dict(sorted(result.statuses.items()))}")
    print(f"hard blockers  : {result.blocked} (sponsorship or region excludes India)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="cost the pass, call nothing")
    parser.add_argument("--limit", type=int, default=None, help="read at most N rows")
    parser.add_argument("--force", action="store_true", help="re-read rows already cached")
    args = parser.parse_args()

    configure_logging()
    if args.dry_run:
        return asyncio.run(dry_run())
    return asyncio.run(live(args.limit, args.force))


if __name__ == "__main__":
    raise SystemExit(main())
