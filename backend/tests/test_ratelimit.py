"""Rate limiter tests. A fake clock drives every window — no real sleeps.

Most limits under test are Groq's free plan for qwen/qwen3.8-27b, from
https://console.groq.com/docs/rate-limits: 30 RPM, 1K RPD, 8K TPM, 200K TPD.
The Mistral shape — per second, per minute, per month — is tested at the end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ratelimit import (
    WINDOWS,
    LimitExhausted,
    RateLimiter,
    Reservation,
    SlidingWindow,
    backoff_delay,
    parse_duration,
    retry_after_seconds,
)

pytestmark = pytest.mark.anyio


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.wall = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


GROQ_LIMITS = {
    "requests_per_minute": 30,
    "requests_per_day": 1_000,
    "tokens_per_minute": 8_000,
    "tokens_per_day": 200_000,
}


def limiter(
    clock: FakeClock,
    *,
    headroom: float = 1.0,
    max_wait: float = 300.0,
    header_scheme: str | None = "groq",
    **limits: int,
) -> RateLimiter:
    return RateLimiter(
        limits={**GROQ_LIMITS, **limits},
        headroom=headroom,
        max_wait=max_wait,
        header_scheme=header_scheme,
        clock=clock,
        wall=lambda: clock.wall,
    )


# --------------------------------------------------------------------------
# What Groq sends
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2", 2.0),  # retry-after: seconds
        ("7.66s", 7.66),  # x-ratelimit-reset-tokens, from the docs
        ("2m59.56s", 179.56),  # x-ratelimit-reset-requests, from the docs
        ("640ms", 0.64),
        ("1h2m3s", 3723.0),
        ("7m", 420.0),
        ("", None),
        ("soon", None),
        ("5x", None),
    ],
)
def test_parse_duration_reads_go_durations_and_bare_seconds(
    text: str, expected: float | None
) -> None:
    result = parse_duration(text)
    assert result == (pytest.approx(expected) if expected is not None else None)


def test_retry_after_header_wins_over_the_body() -> None:
    """The docs define `retry-after` in seconds; it is the authority."""
    assert retry_after_seconds({"retry-after": "9"}, "Please try again in 1.5s.") == 9.0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Please try again in 12.3s.", 12.3),
        ("Please try again in 640ms.", 0.64),
        ("Please try again in 7m32.4s.", 452.4),
    ],
)
def test_retry_after_falls_back_to_the_body(body: str, expected: float) -> None:
    """The first parser read only "Ns"; "7m32.4s" — a daily-cap wait — became
    a flat 10s guess, and every row failed four retries against it."""
    assert retry_after_seconds({}, body) == pytest.approx(expected)


def test_retry_after_is_none_when_nothing_is_named() -> None:
    assert retry_after_seconds({}, "rate limited") is None


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------
def test_backoff_doubles_per_retry() -> None:
    ceilings = [backoff_delay(n, base=1.0, cap=1_000.0, rng=lambda: 1.0) for n in range(5)]
    assert ceilings == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_is_capped() -> None:
    assert backoff_delay(20, base=1.0, cap=60.0, rng=lambda: 1.0) == 60.0


def test_backoff_jitter_never_drops_below_half_the_ceiling() -> None:
    """Equal jitter: randomness to avoid re-colliding, but never an instant retry."""
    assert backoff_delay(3, base=1.0, cap=60.0, rng=lambda: 0.0) == 4.0
    assert backoff_delay(3, base=1.0, cap=60.0, rng=lambda: 1.0) == 8.0


# --------------------------------------------------------------------------
# Sliding windows
# --------------------------------------------------------------------------
def test_window_fills_then_waits_only_for_the_oldest_entry() -> None:
    clock = FakeClock()
    window = SlidingWindow(10_000, 60.0, clock)
    window.reserve(3_000)
    clock.advance(10)
    window.reserve(3_000)
    clock.advance(10)
    window.reserve(3_000)
    assert window.used == 9_000
    # Freeing the first entry (40s from now) is enough; the whole window is not.
    assert window.wait_for(3_000) == pytest.approx(40.0)


def test_settling_below_the_estimate_frees_the_difference() -> None:
    """The reservation is deliberately generous. If an over-estimate were never
    reconciled, the pass would idle its way through a 400-row run."""
    clock = FakeClock()
    window = SlidingWindow(10_000, 60.0, clock)
    entry = window.reserve(9_000)
    window.settle(entry, 1_600)
    assert window.used == 1_600
    assert window.wait_for(5_000) == 0.0


def test_an_amount_larger_than_the_limit_still_fits_an_empty_window() -> None:
    clock = FakeClock()
    assert SlidingWindow(1_000, 60.0, clock).wait_for(5_000) == 0.0


# --------------------------------------------------------------------------
# The limiter
# --------------------------------------------------------------------------
def test_requests_per_minute_is_enforced_locally() -> None:
    """RPM has no response header, so only a local count can respect it."""
    clock = FakeClock()
    lim = limiter(clock, requests_per_minute=3)
    for _ in range(3):
        assert lim.wait_for(10) == 0.0
        lim.settle(_reserve(lim, 10), 10)
    assert lim.wait_for(10) == pytest.approx(60.0)


def test_tokens_per_minute_is_enforced_locally() -> None:
    clock = FakeClock()
    lim = limiter(clock)
    lim.settle(_reserve(lim, 7_000), 7_000)
    assert lim.wait_for(2_000) > 0


def test_headroom_applies_to_the_minute_limits_only() -> None:
    """10% of 8k TPM costs seconds; 10% of 200k TPD would forfeit ~11 rows a day."""
    clock = FakeClock()
    lim = limiter(clock, headroom=0.9)
    assert lim.windows["tokens_per_minute"].limit == 7_200
    assert lim.windows["requests_per_minute"].limit == 27
    assert lim.windows["tokens_per_day"].limit == 200_000
    assert lim.windows["requests_per_day"].limit == 1_000


def test_the_server_token_bucket_refills_continuously() -> None:
    """Measured: 14 tokens spent answered `reset-tokens: 105ms` — 8,000/60 per
    second. So a shortfall of 1,600 tokens is a 12s wait, not "next minute"."""
    clock = FakeClock()
    lim = limiter(clock, tokens_per_minute=1_000_000)  # local window out of the way
    lim.observe({"x-ratelimit-remaining-tokens": "400", "x-ratelimit-limit-tokens": "8000"})
    assert lim.wait_for(2_000) == pytest.approx(12.0)
    clock.advance(12)
    assert lim.wait_for(2_000) == pytest.approx(0.0, abs=1e-6)


def test_the_daily_token_cap_raises_instead_of_sleeping() -> None:
    """TPD is invisible on the wire. A wait of hours inside a background task is
    indistinguishable from a hang, so the limiter stops the pass."""
    clock = FakeClock()
    lim = limiter(clock, tokens_per_day=10_000)
    lim.seed([(clock.wall - timedelta(hours=2), 9_000)])
    with pytest.raises(LimitExhausted) as info:
        lim.wait_for(2_000)
    assert info.value.limit == "tokens_per_day"
    # The seeded spend ages out 22h from now.
    assert info.value.retry_after == pytest.approx(22 * 3600)


def test_seeding_ignores_calls_older_than_a_day() -> None:
    clock = FakeClock()
    lim = limiter(clock, tokens_per_day=10_000)
    lim.seed([(clock.wall - timedelta(hours=25), 9_000)])
    assert lim.wait_for(2_000) == 0.0
    assert lim.snapshot().used["tokens_per_day"] == 0


def test_seeding_counts_recent_calls_toward_the_minute_windows_too() -> None:
    """A pass restarted seconds after the last one must not burst into TPM."""
    clock = FakeClock()
    lim = limiter(clock)
    lim.seed([(clock.wall - timedelta(seconds=10), 7_500)])
    assert lim.wait_for(1_000) == pytest.approx(50.0)


def test_a_short_daily_wait_is_slept_not_raised() -> None:
    clock = FakeClock()
    lim = limiter(clock, tokens_per_day=10_000, max_wait=300.0)
    lim.seed([(clock.wall - timedelta(hours=23, minutes=58), 9_000)])
    assert lim.wait_for(2_000) == pytest.approx(120.0)


def test_exhausted_daily_requests_from_the_server_header_stop_the_pass() -> None:
    clock = FakeClock()
    lim = limiter(clock, max_wait=30.0)
    lim.observe(
        {"x-ratelimit-remaining-requests": "0", "x-ratelimit-limit-requests": "1000"}
    )
    with pytest.raises(LimitExhausted) as info:
        lim.wait_for(100)
    assert info.value.limit == "requests_per_day"


def test_a_released_reservation_returns_its_tokens_and_daily_request() -> None:
    """A 429 is not billed. Charging it was measured and halved throughput."""
    clock = FakeClock()
    lim = limiter(clock)
    res = _reserve(lim, 5_000)
    lim.release(res)
    assert lim.windows["tokens_per_minute"].used == 0
    assert lim.windows["tokens_per_day"].used == 0
    assert lim.windows["requests_per_day"].used == 0
    # Whether a refused call counts toward RPM is undocumented; kept, to err slow.
    assert lim.windows["requests_per_minute"].used == 1


async def test_acquire_sleeps_until_every_limit_has_room() -> None:
    clock = FakeClock()
    lim = limiter(clock, requests_per_minute=1)
    first = await lim.acquire(100, sleep=clock.sleep)
    lim.settle(first, 100)
    start = clock.now
    await lim.acquire(100, sleep=clock.sleep)
    assert clock.now - start == pytest.approx(60.0, abs=0.1)


# --------------------------------------------------------------------------
# The Mistral shape: per second, per minute, per month — no daily window
# --------------------------------------------------------------------------
MISTRAL_LIMITS = {
    "requests_per_second": 1,
    "tokens_per_minute": 500_000,
    "tokens_per_month": 1_000_000_000,
}


def mistral(clock: FakeClock, **over: int) -> RateLimiter:
    return RateLimiter(
        limits={**MISTRAL_LIMITS, **over},
        headroom=0.9,
        max_wait=300.0,
        header_scheme=None,
        clock=clock,
        wall=lambda: clock.wall,
    )


def test_only_the_declared_windows_exist() -> None:
    lim = mistral(FakeClock())
    assert set(lim.windows) == set(MISTRAL_LIMITS)


def test_an_unknown_window_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="requests_per_week"):
        RateLimiter(limits={"requests_per_week": 10})


async def test_one_request_per_second_is_spaced_a_second_apart() -> None:
    """Headroom cannot take 1 req/s below 1 — it floors at one request."""
    clock = FakeClock()
    lim = mistral(clock)
    lim.settle(await lim.acquire(100, sleep=clock.sleep), 100)
    start = clock.now
    await lim.acquire(100, sleep=clock.sleep)
    assert clock.now - start == pytest.approx(1.0, abs=0.1)


def test_the_monthly_token_cap_stops_the_pass() -> None:
    clock = FakeClock()
    lim = mistral(clock, tokens_per_month=100_000)
    lim.seed([(clock.wall - timedelta(days=3), 99_000)])
    with pytest.raises(LimitExhausted) as info:
        lim.wait_for(2_000)
    assert info.value.limit == "tokens_per_month"
    assert info.value.retry_after == pytest.approx(27 * 86_400)


def test_the_ledger_is_read_back_a_month_for_a_monthly_cap() -> None:
    assert mistral(FakeClock()).longest_period == 30 * 86_400
    assert limiter(FakeClock()).longest_period == 86_400


def test_groq_style_headers_are_ignored_where_their_meaning_is_unknown() -> None:
    """Groq's `x-ratelimit-*-tokens` means per MINUTE. Nothing says Mistral's
    headers mean the same, so they must not throttle a Mistral pass."""
    clock = FakeClock()
    lim = mistral(clock)
    lim.observe({"x-ratelimit-remaining-tokens": "0", "x-ratelimit-limit-tokens": "8000"})
    assert lim.wait_for(2_000) == 0.0


# --------------------------------------------------------------------------
# The Cerebras shape: requests and tokens, each per minute, hour and day
# --------------------------------------------------------------------------
# qwen-3.8-27b: Developer-tier minute figures from the model page, hour/day
# from the live headers.
CEREBRAS_LIMITS = {
    "requests_per_minute": 300,
    "requests_per_hour": 27_000,
    "requests_per_day": 648_000,
    "tokens_per_minute": 150_000,
    "tokens_per_hour": 9_000_000,
    "tokens_per_day": 216_000_000,
}

# Recorded verbatim from a live qwen-3.8-27b response, 2026-09-13.
CEREBRAS_HEADERS = {
    "x-ratelimit-remaining-requests-minute": "449",
    "x-ratelimit-remaining-requests-hour": "26999",
    "x-ratelimit-remaining-requests-day": "647999",
    "x-ratelimit-remaining-tokens-minute": "149935",
    "x-ratelimit-remaining-tokens-hour": "26999935",
    "x-ratelimit-remaining-tokens-day": "215999935",
    "x-ratelimit-limit-requests-minute": "450",
    "x-ratelimit-limit-requests-hour": "27000",
    "x-ratelimit-limit-requests-day": "648000",
    "x-ratelimit-limit-tokens-minute": "150000",
    "x-ratelimit-limit-tokens-hour": "9000000",
    "x-ratelimit-limit-tokens-day": "216000000",
}


def cerebras(clock: FakeClock, **over: int) -> RateLimiter:
    return RateLimiter(
        limits={**CEREBRAS_LIMITS, **over},
        headroom=0.9,
        max_wait=300.0,
        header_scheme="cerebras",
        clock=clock,
        wall=lambda: clock.wall,
    )


def test_cerebras_declares_all_six_windows() -> None:
    assert set(cerebras(FakeClock()).windows) == set(CEREBRAS_LIMITS)


def test_cerebras_headers_are_read_for_all_six_windows() -> None:
    lim = cerebras(FakeClock())
    lim.observe(CEREBRAS_HEADERS)
    assert set(lim._server) == set(CEREBRAS_LIMITS)
    assert lim.snapshot().server_remaining_requests == 647_999
    # A fresh key with nothing spent: every limit has room.
    assert lim.wait_for(5_000) == 0.0


def test_a_cerebras_hour_bucket_refills_at_its_limit_rate() -> None:
    """`remaining-tokens-hour` read 26,999,935 against a 9,000,000 limit, so the
    two do not describe one bucket. Refill follows the LIMIT (the uncached
    bucket): an empty hour refills 2,500 tokens a second, not 7,500."""
    clock = FakeClock()
    lim = cerebras(clock, tokens_per_hour=100_000_000)  # local window out of the way
    lim.observe({"x-ratelimit-remaining-tokens-hour": "0", "x-ratelimit-limit-tokens-hour": "9000000"})
    assert lim.wait_for(5_000) == pytest.approx(2.0)


def test_a_lower_tier_key_is_paced_by_what_the_server_reports() -> None:
    """Defaults assume the Developer tier. A Free Trial key (5 requests/min)
    must be held to 5 by its headers, not allowed 270 by the local window."""
    clock = FakeClock()
    lim = cerebras(clock)
    lim.observe(
        {"x-ratelimit-remaining-requests-minute": "0", "x-ratelimit-limit-requests-minute": "5"}
    )
    assert lim.wait_for(100) == pytest.approx(12.0)  # 5/min refills one every 12s


def test_an_exhausted_cerebras_day_from_headers_stops_the_pass() -> None:
    clock = FakeClock()
    lim = cerebras(clock, tokens_per_day=1_000_000_000)
    lim.observe(
        {"x-ratelimit-remaining-tokens-day": "0", "x-ratelimit-limit-tokens-day": "1000000"}
    )
    # 1M/day refills ~11.6 tokens a second: 5,000 tokens is ~7 minutes.
    with pytest.raises(LimitExhausted) as info:
        lim.wait_for(5_000)
    assert info.value.limit == "tokens_per_day"


def test_cerebras_headers_do_not_leak_into_a_groq_limiter() -> None:
    clock = FakeClock()
    lim = limiter(clock)
    lim.observe({"x-ratelimit-remaining-tokens-minute": "0", "x-ratelimit-limit-tokens-minute": "5"})
    assert lim.wait_for(100) == 0.0


def test_an_unknown_header_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="header scheme"):
        RateLimiter(limits={}, header_scheme="openai")


def _reserve(lim: RateLimiter, tokens: int) -> Reservation:
    return Reservation(
        entries={
            name: window.reserve(tokens if WINDOWS[name][1] == "tokens" else 1)
            for name, window in lim.windows.items()
        }
    )
