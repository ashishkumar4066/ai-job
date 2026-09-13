"""LLM rate limiting — a sliding window per limit the provider declares.

Which windows exist is the provider's business (`Settings.llm.limits`):
Groq limits per minute and per day, Mistral per second, per minute and per
month, Cerebras per minute, per hour and per day. The machinery below is the
same for all of them. The notes that follow are about Groq, because that is
where every limit here was measured.

Mistral, for the record: it enforces requests per second, tokens per minute
and tokens per month, per organization, and publishes no free-mode numbers —
they are only on the account's Admin → Limits page. Its rate-limit header
names are not documented either, so its responses are not read for pacing;
local windows plus backoff on 429 do the work.

What Cerebras enforces
----------------------
Per model, as continuously refilling token buckets
(https://inference-docs.cerebras.ai/support/rate-limits). For qwen-3.8-27b
(https://inference-docs.cerebras.ai/models/qwen-3.8-27b):

    tier         RPM   uncached TPM   total TPM   daily tokens
    Free Trial     5        30K           90K          1M
    Developer    300       150K          450K         N/A

Two token buckets: UNCACHED counts cache misses only, TOTAL counts cached
tokens too and is 3x uncached. Every token is counted here as uncached, which
can only overstate uncached use, so the uncached bucket always binds before
the total one and total needs no window of its own.

What Cerebras tells us
----------------------
The docs name no headers. A live probe on 2026-09-13 found six on every
response, and they also cover the hour and day windows the docs do not list:

    x-ratelimit-limit-{requests,tokens}-{minute,hour,day}
    x-ratelimit-remaining-{requests,tokens}-{minute,hour,day}

The `tokens` pair is the UNCACHED bucket (150K/min, 9M/h, 216M/day for
qwen-3.8-27b on that key, whose requests read 450/min, 27K/h, 648K/day). One
quirk: `remaining-tokens-hour` read 26,999,935 against a limit of 9,000,000 —
it appears to report the total bucket. A server bucket is capped at, and
refills at the rate of, its LIMIT, so the quirk can only make it cautious.

Cerebras books a call as prompt + `max_completion_tokens` BEFORE generating,
then charges what was used (65 tokens left the bucket for a 63-token call).
So a call reserves that ceiling here too, and settles to actual usage.

What Groq enforces
------------------
From https://console.groq.com/docs/rate-limits, per model, free plan
(`qwen/qwen3.8-27b` and the gpt-oss family alike):

    RPM  30        requests per minute
    RPD  1,000     requests per day
    TPM  8,000     tokens per minute
    TPD  200,000   tokens per day

"You can hit any limit type depending on which threshold you reach first."
A breach is a `429 Too Many Requests`, and `retry-after` (seconds) "is only
set if you hit the rate limit".

What Groq tells us
------------------
The other headers are always sent, and describe exactly two of the four:

    x-ratelimit-limit-tokens / -remaining-tokens / -reset-tokens      TPM
    x-ratelimit-limit-requests / -remaining-requests / -reset-requests RPD

There is no header for RPM or for TPD. So this module keeps its own sliding
windows for all four, and additionally honours the two server-side buckets it
*can* see, taking whichever wait is longest.

TPD is the one that binds. At ~1,800 tokens a row, 200k tokens is ~110 rows a
day — long before 1,000 requests. It is also the one with no header, which is
why the count is seeded from the persisted `llm_usage` ledger: a pass that
restarts believing it has spent nothing walks straight into the cap. The
2026-09-13 pass did exactly that and failed every row once it crossed ~207k.

Buckets refill continuously
---------------------------
Measured live: 14 tokens spent answered `x-ratelimit-reset-tokens: 105ms`,
which is exactly 8,000/60 tokens per second. So the server buckets are
modelled as continuous refill, not as a counter that resets on a minute
boundary. Whether TPD is a rolling 24h window or resets at a fixed time is not
documented; a rolling window is used here, and a server 429 remains the
authority either way.

Two kinds of wait
-----------------
A per-minute shortfall is slept through — it clears in seconds. A per-day
shortfall longer than `max_wait` raises `LimitExhausted` instead: sleeping
hours inside a background task, one row at a time, is indistinguishable from a
hang, and the pass is resumable anyway.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

SECOND = 1.0
MINUTE = 60.0
HOUR = 3_600.0
DAY = 86_400.0
# A rolling 30 days. Mistral does not say whether its monthly cap resets on a
# calendar boundary; rolling is almost always the stricter reading, and a
# server 429 remains the authority either way.
MONTH = 30 * DAY


class LimitExhausted(Exception):
    """A limit that will not clear within `max_wait`. Stop, do not sleep."""

    def __init__(self, limit: str, retry_after: float, detail: str) -> None:
        super().__init__(f"{limit}: {detail} — clears in {format_wait(retry_after)}")
        self.limit = limit
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# Parsing what Groq sends
# --------------------------------------------------------------------------
# Go duration syntax, as used by `x-ratelimit-reset-*` ("2m59.56s", "7.66s")
# and by the 429 body ("try again in 640ms"). A bare number is seconds — the
# `retry-after` form.
_DURATION_PART = re.compile(r"([0-9]*\.?[0-9]+)(h|ms|m|s)")
_TRY_AGAIN = re.compile(r"try again in\s+([0-9hms.]+)", re.I)


def parse_duration(text: str | None) -> float | None:
    """Seconds in a Go duration or bare-number string, or None if unreadable."""
    if text is None:
        return None
    value = text.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    parts = _DURATION_PART.findall(value)
    if not parts or "".join(a + u for a, u in parts) != value:
        return None
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(amount) * scale[unit] for amount, unit in parts)


def retry_after_seconds(headers: Mapping[str, str], body: str) -> float | None:
    """The wait a 429 names.

    `retry-after` first: the docs define it, and it is rounded up to whole
    seconds, which is the safe direction. The body's "try again in 1m2.5s" is
    the fallback. The first version of this parser read only "Ns" from the
    body, so "640ms" and "7m32s" fell through to a flat 10s guess — observed
    live as rows failing four identical retries against a daily cap.
    """
    header = parse_duration(headers.get("retry-after"))
    if header is not None:
        return header
    match = _TRY_AGAIN.search(body or "")
    return parse_duration(match.group(1).rstrip(".")) if match else None


def backoff_delay(
    retry: int, *, base: float, cap: float, rng: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with equal jitter: uniform in [d/2, d], d = min(cap, base*2^retry).

    Jitter so a retry does not land on the same instant as the window that
    refused it; the half-floor so the "random" part can never mean retrying
    immediately.
    """
    ceiling = min(cap, base * (2 ** max(0, retry)))
    return ceiling / 2 + rng() * ceiling / 2


def format_wait(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds // 3600:.0f}h{(seconds % 3600) / 60:02.0f}m"


# --------------------------------------------------------------------------
# Local windows
# --------------------------------------------------------------------------
class SlidingWindow:
    """Amounts spent within the trailing `period` seconds.

    `reserve` books an amount before a call and returns a handle; `settle`
    replaces it with what was actually spent. A token reservation is a
    deliberately generous estimate, so an unsettled one errs toward waiting.
    """

    def __init__(self, limit: int, period: float, clock: Callable[[], float]) -> None:
        self.limit = max(1, int(limit))
        self.period = period
        self._clock = clock
        self._entries: deque[list[float]] = deque()  # [stamp, amount]

    def _prune(self, now: float) -> None:
        while self._entries and now - self._entries[0][0] >= self.period:
            self._entries.popleft()

    @property
    def used(self) -> int:
        self._prune(self._clock())
        return int(sum(entry[1] for entry in self._entries))

    def add(self, amount: float, *, stamp: float | None = None) -> list[float]:
        entry = [self._clock() if stamp is None else stamp, float(amount)]
        # Seeded entries can arrive out of order; keep the deque sorted so
        # pruning from the left stays correct.
        if self._entries and entry[0] < self._entries[-1][0]:
            items = sorted([*self._entries, entry], key=lambda e: e[0])
            self._entries = deque(items)
        else:
            self._entries.append(entry)
        return entry

    reserve = add

    @staticmethod
    def settle(entry: list[float], actual: float) -> None:
        entry[1] = float(actual)

    def wait_for(self, amount: float) -> float:
        """Seconds until `amount` more fits. 0 when it fits now.

        Waits only for as many of the oldest entries to age out as are needed,
        not for the whole window to clear. An amount larger than the limit fits
        once the window is empty — otherwise it could never be sent at all.
        """
        now = self._clock()
        self._prune(now)
        used = sum(entry[1] for entry in self._entries)
        if used + amount <= self.limit or not self._entries:
            return 0.0
        freed = 0.0
        for stamp, spent in self._entries:
            freed += spent
            if used - freed + amount <= self.limit:
                return max(0.0, self.period - (now - stamp))
        return max(0.0, self.period - (now - self._entries[-1][0]))


@dataclass
class _ServerBucket:
    """One continuously-refilling bucket as last reported by a response."""

    remaining: float
    limit: float
    period: float
    stamp: float

    def wait_for(self, amount: float, now: float) -> float:
        rate = self.limit / self.period
        have = min(self.limit, self.remaining + rate * (now - self.stamp))
        need = min(amount, self.limit)
        return 0.0 if have >= need else (need - have) / rate


# Every window a provider can declare: (period, what it counts). A window is
# SHORT when it clears within a minute — slept through, with headroom — and
# LONG otherwise — never slept through past `max_wait`, never given headroom.
WINDOWS: dict[str, tuple[float, str]] = {
    "requests_per_second": (SECOND, "requests"),
    "requests_per_minute": (MINUTE, "requests"),
    "requests_per_hour": (HOUR, "requests"),
    "requests_per_day": (DAY, "requests"),
    "tokens_per_minute": (MINUTE, "tokens"),
    "tokens_per_hour": (HOUR, "tokens"),
    "tokens_per_day": (DAY, "tokens"),
    "tokens_per_month": (MONTH, "tokens"),
}


def _is_short(period: float) -> bool:
    return period <= MINUTE


# Which response headers describe which window, per provider. The value is
# the header suffix: `x-ratelimit-remaining-<suffix>` / `x-ratelimit-limit-<suffix>`.
# A provider absent here has no documented or verified headers and is paced
# from local windows alone.
HEADER_SCHEMES: dict[str, dict[str, str]] = {
    # Groq: `*-tokens` is always per MINUTE, `*-requests` always per DAY.
    "groq": {"tokens_per_minute": "tokens", "requests_per_day": "requests"},
    # Cerebras: all six, verified live. `tokens` = the uncached bucket.
    "cerebras": {
        "requests_per_minute": "requests-minute",
        "requests_per_hour": "requests-hour",
        "requests_per_day": "requests-day",
        "tokens_per_minute": "tokens-minute",
        "tokens_per_hour": "tokens-hour",
        "tokens_per_day": "tokens-day",
    },
}


@dataclass
class Reservation:
    """What one in-flight call has booked, per window name."""

    entries: dict[str, list[float]]


@dataclass
class UsageSnapshot:
    used: dict[str, int]
    limits: dict[str, int]
    server_remaining_requests: int | None = None


class RateLimiter:
    """Paces calls under whichever limits a provider declares, before sending.

    Groq declares requests and tokens per minute and per day; Mistral declares
    requests per second, tokens per minute and tokens per month. Each named
    limit becomes one sliding window, and a call waits for the longest of them.

    Headroom applies to SHORT windows only. It exists to absorb a token
    estimate that comes in low, and on a 60-second window a 10% cushion costs
    seconds. On a daily token limit the same cushion would forfeit 20,000
    tokens — ~11 rows a day on Groq — to protect a single in-flight estimate
    that is already generous, and the server's 429 still backstops it.
    """

    def __init__(
        self,
        *,
        limits: Mapping[str, int | None],
        headroom: float = 0.9,
        max_wait: float = 300.0,
        header_scheme: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        unknown = set(limits) - set(WINDOWS)
        if unknown:
            raise ValueError(f"unknown rate-limit window(s): {sorted(unknown)}")
        if header_scheme is not None and header_scheme not in HEADER_SCHEMES:
            raise ValueError(f"unknown header scheme: {header_scheme}")
        self._clock = clock
        self._wall = wall
        self.max_wait = max_wait
        self.header_scheme = header_scheme
        self.windows: dict[str, SlidingWindow] = {}
        for name, limit in limits.items():
            if not limit:
                continue
            period, _kind = WINDOWS[name]
            effective = max(1, int(limit * headroom)) if _is_short(period) else limit
            self.windows[name] = SlidingWindow(effective, period, clock)
        # The server's own view of each window it reports, by window name.
        self._server: dict[str, _ServerBucket] = {}

    @property
    def longest_period(self) -> float:
        """How far back the ledger must be read to seed every window."""
        return max((w.period for w in self.windows.values()), default=MINUTE)

    # ------------------------------------------------------------ seeding
    def seed(self, events: Iterable[tuple[datetime, int]]) -> None:
        """Load already-billed calls (`created_at`, `total_tokens`) from the ledger.

        Timestamps are converted onto the monotonic clock by their age, so a
        call made 23 hours ago ages out of a day window in one hour. Each call
        lands in every window it still falls inside.
        """
        now_mono, now_wall = self._clock(), self._wall()
        for created_at, tokens in events:
            age = (now_wall - created_at).total_seconds()
            if age < 0:
                continue
            for name, window in self.windows.items():
                if age < window.period:
                    amount = tokens if WINDOWS[name][1] == "tokens" else 1
                    window.add(amount, stamp=now_mono - age)

    # ---------------------------------------------------------- observing
    def observe(self, headers: Mapping[str, str]) -> None:
        """Record the server's view of each window, where the header meaning is known."""
        if self.header_scheme is None:
            return
        now = self._clock()
        for name, suffix in HEADER_SCHEMES[self.header_scheme].items():
            bucket = _bucket(headers, suffix, WINDOWS[name][0], now)
            if bucket is not None:
                self._server[name] = bucket

    # ------------------------------------------------------------- pacing
    def wait_for(self, estimate: int) -> float:
        """Seconds to wait before a call of `estimate` tokens may be sent.

        Raises `LimitExhausted` when a LONG window (per day, per month) is the
        reason and the wait is longer than `max_wait`.
        """
        now = self._clock()
        waits: list[float] = []

        for name, window in self.windows.items():
            period, kind = WINDOWS[name]
            wait = window.wait_for(estimate if kind == "tokens" else 1)
            if not _is_short(period) and wait > self.max_wait:
                raise LimitExhausted(
                    name, wait, f"{window.used:,} of {window.limit:,} {kind} used"
                )
            waits.append(wait)

        # The server's buckets count spend the local windows cannot see — a
        # previous process, another tool on the same key.
        for name, bucket in self._server.items():
            period, kind = WINDOWS[name]
            wait = bucket.wait_for(estimate if kind == "tokens" else 1, now)
            if not _is_short(period) and wait > self.max_wait:
                raise LimitExhausted(
                    name,
                    wait,
                    f"server reports {int(bucket.remaining):,} of {int(bucket.limit):,} {kind} left",
                )
            waits.append(wait)

        return max(waits, default=0.0)

    async def acquire(
        self,
        estimate: int,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> Reservation:
        """Wait until every limit has room, then book the call.

        Loops rather than sleeping once: the wait is computed from a snapshot,
        and re-checking after it is what makes the booking correct.
        """
        while True:
            wait = self.wait_for(estimate)
            if wait <= 0:
                return Reservation(
                    entries={
                        name: window.reserve(estimate if WINDOWS[name][1] == "tokens" else 1)
                        for name, window in self.windows.items()
                    }
                )
            await sleep(wait + 0.05)

    def settle(self, reservation: Reservation, tokens: int) -> None:
        """A billed call: replace the token estimate with what was spent."""
        for name, entry in reservation.entries.items():
            if WINDOWS[name][1] == "tokens":
                SlidingWindow.settle(entry, tokens)

    def release(self, reservation: Reservation) -> None:
        """A call that was not billed — refused with a 429, or never landed.

        Its tokens, and its place in any LONG request count, are returned. Its
        slot in a SHORT request window is kept: whether a refused request counts
        toward RPM/RPS is undocumented, and keeping it errs toward sending
        slower. Charging the refused tokens was measured on Groq and is worse —
        it halved throughput to 1.6 rows/min by inflating every later wait with
        tokens nobody spent.
        """
        for name, entry in reservation.entries.items():
            period, kind = WINDOWS[name]
            if kind == "tokens" or not _is_short(period):
                SlidingWindow.settle(entry, 0)

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(
            used={name: w.used for name, w in self.windows.items()},
            limits={name: w.limit for name, w in self.windows.items()},
            server_remaining_requests=(
                int(self._server["requests_per_day"].remaining)
                if "requests_per_day" in self._server
                else None
            ),
        )


def _bucket(
    headers: Mapping[str, str], kind: str, period: float, now: float
) -> _ServerBucket | None:
    try:
        remaining = float(headers[f"x-ratelimit-remaining-{kind}"])
        limit = float(headers[f"x-ratelimit-limit-{kind}"])
    except (KeyError, ValueError):
        return None
    if limit <= 0:
        return None
    return _ServerBucket(remaining=remaining, limit=limit, period=period, stamp=now)
