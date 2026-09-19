"""Deep-read pass tests. No live calls — every response is served by respx.

The load-bearing ones are the cache tests. CLAUDE.md's acceptance criterion is
"a re-run with no profile change and no JD change issues zero LLM calls", and
at ~1,700 tokens a row against an 8,000/minute ceiling, a cache that quietly
misses does not cost a little extra — it costs another 90 minutes and 40% of
the day's request budget.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.llm import (
    SCREEN_SCHEMA,
    JobScreen,
    LLMError,
    LLMNotConfigured,
    LLMRateLimited,
    LLMScreener,
    build_messages,
    estimate_tokens,
    profile_brief,
)
from app.llm_runner import run_llm_screen
from app.matching import MatchWeights
from app.models import JobMatch, JobPosting, utcnow
from app.profile import Profile

pytestmark = pytest.mark.anyio

COMPLETIONS = "https://api.groq.com/openai/v1/chat/completions"


def settings(**over: Any) -> Settings:
    base = {
        # Pinned, so an LLM_PROVIDER in the developer's .env cannot leak in.
        "llm_provider": "groq",
        "groq_api_key": "test-key",
        "groq_model": "qwen/qwen3.8-27b",
        "llm_max_retries": 2,
        "llm_completion_reserve": 100,
        # Pacing is tested directly in test_ratelimit.py, not here.
        "groq_tokens_per_minute": 1_000_000,
        "groq_requests_per_minute": 100_000,
    }
    base.update(over)
    return Settings(**base)


def screener_for(
    cfg: Settings,
    *,
    sleeps: list[float] | None = None,
    rng: Callable[[], float] = lambda: 0.5,
) -> LLMScreener:
    """A screener whose sleeps are recorded instead of slept."""
    record = sleeps if sleeps is not None else []

    async def fake_sleep(seconds: float) -> None:
        record.append(seconds)

    return LLMScreener(cfg, sleep=fake_sleep, rng=rng)


def screen_payload(**over: Any) -> dict[str, Any]:
    body = {
        "posting_status": "active",
        "status_reason": "names a team and concrete duties",
        "fit_band": "strong",
        "fit_reasons": ["asks for Python and FastAPI"],
        "strengths": ["Python", "FastAPI"],
        "must_have_gaps": ["AWS"],
        "nice_to_have_gaps": [],
        "seniority": "senior",
        "tech_stack": ["Python", "FastAPI", "AWS"],
        "sponsorship_required": "unstated",
        "location_policy": "open_to_india",
        "work_mode": "remote",
        "compensation_text": "",
        "inconsistencies": [],
    }
    body.update(over)
    return body


def ok_response(**over: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(screen_payload(**over))}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 400, "total_tokens": 1600},
        },
    )


@pytest.fixture
def profile() -> Profile:
    return Profile(
        skills={"backend": {"python": 3, "fastapi": 3}, "cloud": {"gcp": 2}},
        gaps={"aws": 2, "kubernetes": 2},
    )


# --------------------------------------------------------------------------
# The contract with the model
# --------------------------------------------------------------------------
def test_schema_and_model_describe_the_same_object() -> None:
    """`SCREEN_SCHEMA` is hand-written; `JobScreen` parses the reply.

    They are two statements of one contract, so a field added to either and
    forgotten in the other is a silent hole — the model would be asked for
    something nothing reads, or the parse would drop a field that was paid for.
    """
    assert set(SCREEN_SCHEMA["properties"]) == set(JobScreen.model_fields)
    assert set(SCREEN_SCHEMA["required"]) == set(JobScreen.model_fields)


def test_every_field_carries_a_description() -> None:
    """Verified live: without descriptions and anchors the model answered a
    0-100 scale on 0-10 and returned 6 for a good match, stably, at
    temperature 0. The descriptions are load-bearing, not documentation."""
    for name, spec in SCREEN_SCHEMA["properties"].items():
        assert spec.get("description"), f"{name} has no description"


def test_no_union_types_in_the_schema() -> None:
    """A `["string", "null"]` union is the usual trigger for a strict-mode
    violation, and `comp_stated` gains nothing from the distinction — an empty
    string already means "not stated"."""
    for name, spec in SCREEN_SCHEMA["properties"].items():
        assert isinstance(spec["type"], str), f"{name} uses a union type"


def test_enum_fields_match_the_anchors_the_description_defines() -> None:
    assert SCREEN_SCHEMA["properties"]["fit_band"]["enum"] == [
        "excellent",
        "strong",
        "moderate",
        "weak",
        "poor",
    ]
    assert SCREEN_SCHEMA["properties"]["posting_status"]["enum"] == [
        "active",
        "evergreen",
        "ghost",
    ]


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------
def test_jd_is_truncated_but_the_profile_never_is(profile: Profile) -> None:
    messages = build_messages(
        title="Senior Engineer",
        company="Acme",
        description_text="x" * 50_000,
        profile_text=profile_brief(profile),
        max_jd_chars=1_000,
    )
    user = messages[1]["content"]
    assert "CANDIDATE PROFILE" in user
    assert "[truncated]" in user
    assert len(user) < 3_000


def test_a_posting_with_no_description_still_produces_a_prompt(profile: Profile) -> None:
    """3,266 of 5,491 open rows come from sources with thin metadata; an empty
    JD must be screenable, not a crash."""
    messages = build_messages(
        title="Engineer",
        company="Acme",
        description_text=None,
        profile_text=profile_brief(profile),
        max_jd_chars=8_000,
    )
    assert "no description text" in messages[1]["content"]


def test_profile_brief_groups_skills_by_weight(profile: Profile) -> None:
    """`fit_band`'s anchors refer to "the candidate's strongest skills", which
    a flat list gives the model no way to identify."""
    brief = profile_brief(profile)
    assert "Strongest skills" in brief
    assert "python" in brief and "fastapi" in brief
    assert "NOT SHIPPED" in brief and "aws" in brief


def test_profile_brief_states_the_sponsorship_position(profile: Profile) -> None:
    assert "REQUIRES visa sponsorship" in profile_brief(profile)


def test_profile_brief_tags_evidence_by_depth() -> None:
    """The point of `evidence`: "used an LLM API in a side project" and "built
    LLM infrastructure in production" both put `llm` in `skills`, and only the
    depth tag lets the model tell a production-experience JD they differ."""
    profile = Profile(
        skills={"ai": {"rag": 3, "qdrant": 2, "pytorch": 2}},
        evidence=[
            {"area": "RAG", "depth": "production", "proof": "Text-to-SQL with rag"},
            {"area": "Vector search", "depth": "project", "proof": "qdrant side project"},
        ],
        unproven=["LLM fine-tuning"],
    )
    brief = profile_brief(profile)
    assert "[P] RAG: Text-to-SQL with rag" in brief
    assert "[S] Vector search: qdrant side project" in brief
    assert "LLM fine-tuning" in brief
    # Named in the evidence already, so not billed a second time in the list.
    skill_lines = [line for line in brief.splitlines() if "skills" in line or "experience:" in line]
    assert not any("qdrant" in line or "rag" in line.split(":", 1)[1] for line in skill_lines)
    assert any("pytorch" in line for line in skill_lines)


def test_evidence_changes_the_profile_version() -> None:
    """Evidence is what the LLM reads, so a verdict read against the old
    evidence must not be shown as current."""
    base = {"skills": {"ai": {"rag": 3}}}
    before = Profile(**base)
    after = Profile(**base, evidence=[{"area": "RAG", "proof": "shipped"}])
    assert before.version != after.version


def test_the_band_is_generated_after_the_evidence_it_summarizes() -> None:
    """Strict structured output generates properties in order. The band last
    means it is decided from strengths and gaps already written, not asserted
    first and rationalized after."""
    order = SCREEN_SCHEMA["required"]
    assert order[-1] == "fit_band"
    assert order.index("must_have_gaps") < order.index("fit_band")
    assert list(SCREEN_SCHEMA["properties"]) == order


def test_gaps_are_split_into_must_have_and_nice_to_have() -> None:
    screen = JobScreen(**screen_payload(must_have_gaps=["AWS"], nice_to_have_gaps=["Go"]))
    assert screen.fit_half()["must_have_gaps"] == ["AWS"]
    assert screen.fit_half()["nice_to_have_gaps"] == ["Go"]


# --------------------------------------------------------------------------
# Token estimate
# --------------------------------------------------------------------------
def test_estimate_is_generous_rather_than_exact() -> None:
    messages = [{"role": "user", "content": "x" * 3_600}]
    assert estimate_tokens(messages, 0) == 1_000
    assert estimate_tokens(messages, 500) == 1_500


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------
async def test_missing_key_is_refused_not_faked() -> None:
    with pytest.raises(LLMNotConfigured):
        LLMScreener(Settings(llm_provider="groq", groq_api_key=None))


@respx.mock
async def test_a_successful_screen_is_parsed() -> None:
    respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with screener_for(settings()) as screener:
        screen = await screener.screen(
            title="Senior Backend Engineer",
            company="Acme",
            description_text="Python, FastAPI, AWS",
            profile_text="CANDIDATE PROFILE",
        )
    assert screen.fit_band == "strong"
    assert screen.must_have_gaps == ["AWS"]
    assert screener.tokens_spent == 1600
    assert screener.requests_made == 1


@respx.mock
async def test_a_billed_call_is_recorded_for_the_daily_ledger() -> None:
    """Tokens-per-day has no response header; the ledger is the only count."""
    respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with screener_for(settings()) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")
        events = screener.drain_usage()
    assert [(e.total_tokens, e.prompt_tokens, e.status_code) for e in events] == [(1600, 1200, 200)]
    assert screener.drain_usage() == []


@respx.mock
async def test_a_429_waits_at_least_the_retry_after_it_names() -> None:
    """`retry-after` is the floor. Backoff may wait longer, never shorter."""
    sleeps: list[float] = []
    route = respx.post(COMPLETIONS).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}, text="rate limit"),
            ok_response(),
        ]
    )
    async with screener_for(settings(), sleeps=sleeps) as screener:
        screen = await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert screen.fit_band == "strong"
    assert route.call_count == 2
    assert max(sleeps) >= 7.0


@respx.mock
async def test_repeated_429s_back_off_exponentially() -> None:
    """With no named wait, each retry's ceiling doubles — base 2s here, so the
    n-th wait lies in [2^n, 2^(n+1)] seconds (equal jitter, rng pinned high)."""
    sleeps: list[float] = []
    respx.post(COMPLETIONS).mock(
        side_effect=[httpx.Response(429, text="rate limit")] * 4 + [ok_response()]
    )
    cfg = settings(llm_backoff_base_seconds=2.0, llm_backoff_max_seconds=60.0)
    async with screener_for(cfg, sleeps=sleeps, rng=lambda: 1.0) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert [round(s) for s in sleeps] == [2, 4, 8, 16]


@respx.mock
async def test_backoff_is_capped() -> None:
    sleeps: list[float] = []
    respx.post(COMPLETIONS).mock(
        side_effect=[httpx.Response(429, text="rate limit")] * 6 + [ok_response()]
    )
    cfg = settings(llm_backoff_base_seconds=2.0, llm_backoff_max_seconds=10.0)
    async with screener_for(cfg, sleeps=sleeps, rng=lambda: 1.0) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert max(sleeps) == 10.0


@respx.mock
async def test_rate_limits_do_not_spend_the_rows_retry_budget() -> None:
    """A 429 is the window's fault, not the row's. With `llm_max_retries=2`,
    four 429s in a row used to fail the row outright."""
    route = respx.post(COMPLETIONS).mock(
        side_effect=[httpx.Response(429, text="try again in 0.01s")] * 4 + [ok_response()]
    )
    async with screener_for(settings(llm_max_retries=2)) as screener:
        screen = await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert screen.fit_band == "strong"
    assert route.call_count == 5


@respx.mock
async def test_429s_are_bounded_so_a_row_cannot_hang() -> None:
    route = respx.post(COMPLETIONS).mock(return_value=httpx.Response(429, text="rate limit"))
    async with screener_for(settings(llm_max_rate_limit_retries=3)) as screener:
        with pytest.raises(LLMError, match="rate_limited"):
            await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert route.call_count == 4


@respx.mock
async def test_a_long_named_wait_stops_the_pass_rather_than_sleeping() -> None:
    """A multi-minute wait is the daily cap; sitting it out row by row would
    stamp the whole remaining queue as failed — which is what happened on
    2026-09-13, when "try again in 7m32s" was misread as a 10s wait."""
    sleeps: list[float] = []
    respx.post(COMPLETIONS).mock(
        return_value=httpx.Response(
            429,
            text='{"error":{"message":"Rate limit reached on tokens per day (TPD): '
            'Limit 200000, Used 199000, Requested 1800. Please try again in 42m10s."}}',
        )
    )
    async with screener_for(settings(), sleeps=sleeps) as screener:
        with pytest.raises(LLMRateLimited) as info:
            await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert info.value.retry_after == pytest.approx(2530.0)
    assert sleeps == []


@respx.mock
async def test_the_daily_token_cap_stops_before_sending() -> None:
    """Seeded with today's spend, the limiter refuses a call that cannot fit —
    no request is made, so nothing is billed or refused."""
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    cfg = settings(groq_tokens_per_day=10_000)
    async with screener_for(cfg) as screener:
        screener.limiter.seed([(datetime.now(UTC) - timedelta(hours=1), 9_950)])
        with pytest.raises(LLMRateLimited, match="tokens_per_day"):
            await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert route.call_count == 0


@respx.mock
async def test_server_errors_back_off_and_count_against_the_row() -> None:
    sleeps: list[float] = []
    route = respx.post(COMPLETIONS).mock(
        side_effect=[httpx.Response(503, text="over capacity"), ok_response()]
    )
    async with screener_for(settings(), sleeps=sleeps) as screener:
        screen = await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert screen.fit_band == "strong"
    assert route.call_count == 2
    assert len(sleeps) == 1


@respx.mock
async def test_persistent_server_errors_fail_the_row() -> None:
    route = respx.post(COMPLETIONS).mock(return_value=httpx.Response(500, text="boom"))
    async with screener_for(settings(llm_max_retries=2)) as screener:
        with pytest.raises(LLMError, match="HTTP 500"):
            await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert route.call_count == 3


@respx.mock
async def test_network_errors_are_retried_with_backoff() -> None:
    route = respx.post(COMPLETIONS).mock(
        side_effect=[httpx.ConnectError("reset"), ok_response()]
    )
    async with screener_for(settings()) as screener:
        screen = await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert screen.fit_band == "strong"
    assert route.call_count == 2


@respx.mock
async def test_schema_violation_is_retried() -> None:
    """Measured: `gpt-oss-120b` violates its own strict schema intermittently —
    3 of 4 probe rows, then the same row succeeded on the next attempt."""
    route = respx.post(COMPLETIONS).mock(
        side_effect=[
            httpx.Response(400, text='{"error":{"message":"Generated JSON does not match"}}'),
            ok_response(),
        ]
    )
    async with screener_for(settings()) as screener:
        screen = await screener.screen(
            title="Engineer", company="Acme", description_text="jd", profile_text="p"
        )
    assert screen.posting_status == "active"
    assert route.call_count == 2


@respx.mock
async def test_auth_failure_is_not_retried() -> None:
    """A 401 will not fix itself; burning the retry ladder on it wastes a
    minute per row across the whole pass."""
    route = respx.post(COMPLETIONS).mock(return_value=httpx.Response(401, text="bad key"))
    async with screener_for(settings()) as screener:
        with pytest.raises(LLMError):
            await screener.screen(
                title="Engineer", company="Acme", description_text="jd", profile_text="p"
            )
    assert route.call_count == 1


@respx.mock
async def test_unparseable_content_raises_rather_than_returning_a_guess() -> None:
    respx.post(COMPLETIONS).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "not json at all"}}],
                "usage": {"total_tokens": 100},
            },
        )
    )
    async with screener_for(settings(llm_max_retries=0)) as screener:
        with pytest.raises(LLMError):
            await screener.screen(
                title="Engineer", company="Acme", description_text="jd", profile_text="p"
            )


@respx.mock
async def test_reasoning_effort_is_sent_only_to_models_that_accept_it() -> None:
    """qwen 400s on `reasoning_effort`; the gpt-oss family needs it to stay
    affordable. The flag is per-model, not global."""
    captured: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return ok_response()

    respx.post(COMPLETIONS).mock(side_effect=capture)

    async with screener_for(settings(groq_model="qwen/qwen3.8-27b")) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    async with screener_for(settings(groq_model="openai/gpt-oss-120b")) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")

    assert "reasoning_effort" not in captured[0]
    assert captured[1]["reasoning_effort"] == "low"


@respx.mock
async def test_strict_json_schema_is_always_requested() -> None:
    captured: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return ok_response()

    respx.post(COMPLETIONS).mock(side_effect=capture)
    async with screener_for(settings()) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")

    fmt = captured[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert captured[0]["temperature"] == 0


# --------------------------------------------------------------------------
# Provider switch — LLM_PROVIDER=groq|mistral|cerebras
# --------------------------------------------------------------------------
MISTRAL_COMPLETIONS = "https://api.mistral.ai/v1/chat/completions"


def mistral_settings(**over: Any) -> Settings:
    base = {
        "llm_provider": "mistral",
        "mistral_api_key": "mistral-test-key",
        "mistral_model": "mistral-small-latest",
        "llm_max_retries": 2,
        "llm_completion_reserve": 100,
        "mistral_tokens_per_minute": 1_000_000,
        "mistral_requests_per_second": 1_000,
    }
    base.update(over)
    return Settings(**base)


@respx.mock
async def test_mistral_is_called_with_its_own_url_key_model_and_schema() -> None:
    captured: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return ok_response()

    respx.post(MISTRAL_COMPLETIONS).mock(side_effect=capture)
    groq_route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with screener_for(mistral_settings()) as screener:
        screen = await screener.screen(title="t", company="c", description_text="d", profile_text="p")

    assert screen.fit_band == "strong"
    assert groq_route.call_count == 0
    body = json.loads(captured[0].content)
    assert captured[0].headers["authorization"] == "Bearer mistral-test-key"
    assert body["model"] == "mistral-small-latest"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "reasoning_effort" not in body


async def test_a_missing_mistral_key_names_the_variable_to_set() -> None:
    with pytest.raises(LLMNotConfigured, match="MISTRAL_API_KEY"):
        LLMScreener(mistral_settings(mistral_api_key=None))


def test_the_groq_key_does_not_configure_mistral() -> None:
    cfg = Settings(llm_provider="mistral", groq_api_key="g", mistral_api_key=None)
    assert not cfg.llm_configured


@respx.mock
async def test_a_mistral_400_is_not_retried() -> None:
    """On Groq a 400 is the model breaking its schema, intermittently. On
    Mistral it is a malformed request, which will fail identically every time."""
    route = respx.post(MISTRAL_COMPLETIONS).mock(
        return_value=httpx.Response(400, text='{"message":"invalid schema"}')
    )
    async with screener_for(mistral_settings()) as screener:
        with pytest.raises(LLMError, match="HTTP 400"):
            await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert route.call_count == 1


def test_mistral_paces_per_second_and_per_month() -> None:
    limiter = LLMScreener(mistral_settings()).limiter
    assert set(limiter.windows) == {"requests_per_second", "tokens_per_minute", "tokens_per_month"}


CEREBRAS_COMPLETIONS = "https://api.cerebras.ai/v1/chat/completions"


def cerebras_settings(**over: Any) -> Settings:
    base = {
        "llm_provider": "cerebras",
        "cerebras_api_key": "cerebras-test-key",
        "cerebras_model": "qwen-3.8-27b",
        "llm_max_retries": 2,
        "llm_completion_reserve": 100,
        "cerebras_requests_per_minute": 100_000,
        "cerebras_tokens_per_minute": 1_000_000,
    }
    base.update(over)
    return Settings(**base)


def _keywords(node: Any) -> set[str]:
    """Every schema keyword used anywhere, ignoring field names under `properties`."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties":
                for spec in value.values():
                    found |= _keywords(spec)
            else:
                found.add(key)
                found |= _keywords(value)
    elif isinstance(node, list):
        for item in node:
            found |= _keywords(item)
    return found


@respx.mock
async def test_cerebras_is_called_with_its_own_url_key_model_and_knobs() -> None:
    captured: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return ok_response()

    respx.post(CEREBRAS_COMPLETIONS).mock(side_effect=capture)
    groq_route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with screener_for(cerebras_settings()) as screener:
        screen = await screener.screen(title="t", company="c", description_text="d", profile_text="p")

    assert screen.fit_band == "strong"
    assert groq_route.call_count == 0
    body = json.loads(captured[0].content)
    assert captured[0].headers["authorization"] == "Bearer cerebras-test-key"
    assert body["model"] == "qwen-3.8-27b"
    assert body["response_format"]["json_schema"]["strict"] is True
    # qwen on Cerebras reasons at HIGH unless told otherwise.
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == 4096


@respx.mock
async def test_cerebras_schema_drops_the_array_bounds_strict_mode_rejects() -> None:
    """Cerebras documents minItems/maxItems as unsupported in strict mode.
    Everything else — descriptions, enums, required — must still be sent."""
    captured: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return ok_response()

    respx.post(CEREBRAS_COMPLETIONS).mock(side_effect=capture)
    async with screener_for(cerebras_settings()) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")

    sent = captured[0]["response_format"]["json_schema"]["schema"]
    assert "maxItems" in _keywords(SCREEN_SCHEMA)  # the shared schema is untouched
    assert not _keywords(sent) & {"minItems", "maxItems"}
    assert set(sent["properties"]) == set(SCREEN_SCHEMA["properties"])
    assert sent["required"] == SCREEN_SCHEMA["required"]
    assert sent["properties"]["fit_band"] == SCREEN_SCHEMA["properties"]["fit_band"]


@respx.mock
async def test_the_groq_schema_keeps_its_array_bounds() -> None:
    captured: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return ok_response()

    respx.post(COMPLETIONS).mock(side_effect=capture)
    async with screener_for(settings()) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")

    assert captured[0]["response_format"]["json_schema"]["schema"] == SCREEN_SCHEMA
    assert "max_completion_tokens" not in captured[0]


@respx.mock
async def test_a_truncated_completion_fails_once_and_names_the_knob() -> None:
    """At temperature 0 a retry hits the same ceiling, so it is not retried."""
    route = respx.post(CEREBRAS_COMPLETIONS).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"posting_st'}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 4096, "total_tokens": 5296},
            },
        )
    )
    async with screener_for(cerebras_settings()) as screener:
        with pytest.raises(LLMError, match="max_completion_tokens=4096"):
            await screener.screen(title="t", company="c", description_text="d", profile_text="p")
        assert screener.tokens_spent == 5296
    assert route.call_count == 1


@respx.mock
async def test_a_cerebras_400_is_not_retried() -> None:
    route = respx.post(CEREBRAS_COMPLETIONS).mock(
        return_value=httpx.Response(400, text='{"message":"bad request"}')
    )
    async with screener_for(cerebras_settings()) as screener:
        with pytest.raises(LLMError, match="HTTP 400"):
            await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    assert route.call_count == 1


async def test_a_missing_cerebras_key_names_the_variable_to_set() -> None:
    with pytest.raises(LLMNotConfigured, match="CEREBRAS_API_KEY"):
        LLMScreener(cerebras_settings(cerebras_api_key=None))


def test_cerebras_paces_requests_and_tokens_per_minute_hour_and_day() -> None:
    limiter = LLMScreener(cerebras_settings()).limiter
    assert set(limiter.windows) == {
        "requests_per_minute",
        "requests_per_hour",
        "requests_per_day",
        "tokens_per_minute",
        "tokens_per_hour",
        "tokens_per_day",
    }
    assert limiter.header_scheme == "cerebras"


def test_cerebras_defaults_are_qwens_developer_tier_limits() -> None:
    # No .env: a developer's tuned CEREBRAS_* values must not leak in.
    cfg = Settings(_env_file=None, llm_provider="cerebras", cerebras_api_key="k")
    assert cfg.llm.model == "qwen-3.8-27b"
    assert cfg.llm.limits == {
        "requests_per_minute": 300,
        "requests_per_hour": 27_000,
        "requests_per_day": 648_000,
        "tokens_per_minute": 150_000,
        "tokens_per_hour": 9_000_000,
        "tokens_per_day": 216_000_000,
    }


@respx.mock
async def test_a_cerebras_call_is_admitted_against_its_completion_ceiling() -> None:
    """Cerebras books prompt + max_completion_tokens before generating. A call
    whose ceiling does not fit the minute bucket must wait, even though its
    real usage (1,600 tokens) would have fit."""
    respx.post(CEREBRAS_COMPLETIONS).mock(return_value=ok_response())
    sleeps: list[float] = []
    cfg = cerebras_settings(cerebras_tokens_per_minute=5_000, llm_budget_headroom=1.0)
    async with screener_for(cfg, sleeps=sleeps) as screener:
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")
        # Settled to actual usage, not the 4,096 ceiling.
        assert screener.limiter.windows["tokens_per_minute"].used == 1_600
        await screener.screen(title="t", company="c", description_text="d", profile_text="p")
    # 1,600 used + ~4,200 reserved > 5,000: the second call waited.
    assert sleeps and sleeps[0] > 0


def test_an_empty_reasoning_effort_is_omitted() -> None:
    assert cerebras_settings(cerebras_reasoning_effort="").llm.reasoning_effort is None


def test_old_groq_env_names_still_set_the_shared_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing .env written before the switch must keep meaning the same."""
    monkeypatch.setenv("GROQ_MAX_JD_CHARS", "1234")
    monkeypatch.setenv("LLM_MAX_RETRIES", "7")
    cfg = Settings()
    assert cfg.llm_max_jd_chars == 1234
    assert cfg.llm_max_retries == 7


@respx.mock
async def test_verdicts_record_which_provider_wrote_them(
    session_factory, profile: Profile
) -> None:
    """Cached verdicts outlive a provider switch, so each one says who wrote it."""
    respx.post(MISTRAL_COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        job, match = await _seed(session, score=90, confident=True, version=profile.version)
        await session.commit()
        await run_llm_screen(
            session, settings=mistral_settings(), profile=profile, weights=MatchWeights()
        )
        await session.refresh(match)
        await session.refresh(job)

    assert match.llm_verdict["provider"] == "mistral"
    assert match.llm_verdict["model"] == "mistral-small-latest"
    assert job.llm_validity["provider"] == "mistral"


# --------------------------------------------------------------------------
# Blockers the deterministic pass cannot see
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sponsorship_required", "yes"),
        ("location_policy", "excludes_india"),
        # The candidate is remote-only, so an on-site requirement is a hard
        # stop in its own right.
        ("work_mode", "onsite"),
    ],
)
def test_prose_only_blockers_are_flagged(field: str, value: str) -> None:
    """Eligibility rule 1 reads the board's structured location fields. "Must be
    authorized to work in the US without sponsorship" lives in the JD prose,
    which is exactly what this pass is for."""
    assert JobScreen(**screen_payload(**{field: value})).blocked


def test_an_ordinary_screen_is_not_blocked() -> None:
    assert not JobScreen(**screen_payload()).blocked


def test_an_indian_onsite_role_is_blocked_for_being_onsite_not_for_location() -> None:
    """The first live run reported Steps AI's on-site Hyderabad role as
    `india_eligible: no` — the right answer for the wrong reason, and the wrong
    reason is what the dashboard would have shown. Residency and work mode are
    separate questions and must stay separate fields."""
    screen = JobScreen(**screen_payload(location_policy="open_to_india", work_mode="onsite"))
    assert screen.location_policy == "open_to_india"
    assert screen.blocked


def test_comp_unstated_is_an_empty_string_not_a_word() -> None:
    """The model returned the literal string 'No' for postings that state no
    pay, which is indistinguishable from a real answer once stored."""
    spec = SCREEN_SCHEMA["properties"]["compensation_text"]["description"]
    assert "EMPTY STRING" in spec
    assert "Never answer 'No'" in spec


def test_work_mode_is_not_allowed_to_leak_into_india_eligible() -> None:
    spec = SCREEN_SCHEMA["properties"]["location_policy"]["description"]
    assert "on-site" in spec and "work_mode" in spec


# --------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------
async def _seed(
    session,
    *,
    score: int,
    confident: bool,
    version: str,
    content_hash: str = "h1",
    validity_score: int | None = 90,
    blocked: bool = False,
):
    job = JobPosting(
        source_key=f"test:acme:{score}:{content_hash}",
        source_id="test",
        ats="greenhouse",
        company="Acme",
        title="Senior Backend Engineer",
        locations=["Remote"],
        apply_url="https://example.com/j",
        description_text="Python and FastAPI",
        status="open",
        eligibility_pass=True,
        content_hash=content_hash,
        validity_score=validity_score,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    session.add(job)
    await session.flush()
    match = JobMatch(
        job_id=job.id,
        profile_version=version,
        score=score,
        confident=confident,
        blocked=blocked,
        blockers=["no_visa_sponsorship:No visa sponsorship"] if blocked else [],
        llm_content_hash=content_hash,
        scored_at=utcnow(),
    )
    session.add(match)
    await session.flush()
    return job, match


@respx.mock
async def test_only_shortlisted_rows_are_read(session_factory, profile: Profile) -> None:
    """Only a confident, verified, unblocked row scoring ≥ 65 is worth a call.

    Low confidence used to route on its own and carried 142 of 198 routed rows,
    most of which the LLM then called weak or poor. It no longer does."""
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    v = profile.version
    async with session_factory() as session:
        await _seed(session, score=80, confident=True, version=v, content_hash="good")
        await _seed(session, score=20, confident=True, version=v, content_hash="low-score")
        await _seed(session, score=20, confident=False, version=v, content_hash="low-conf")
        await _seed(session, score=90, confident=False, version=v, content_hash="high-low-conf")
        await _seed(
            session, score=90, confident=True, version=v, content_hash="unverified",
            validity_score=None,
        )
        await _seed(
            session, score=90, confident=True, version=v, content_hash="suspect",
            validity_score=40,
        )
        await _seed(session, score=90, confident=True, version=v, content_hash="blk", blocked=True)
        await session.commit()

        result = await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights()
        )

    assert result.routed == 1
    assert result.screened == 1
    assert route.call_count == 1


@respx.mock
async def test_a_pass_never_reads_more_than_fifty(session_factory, profile: Profile) -> None:
    """The ceiling holds even when a caller (or an old `.env` with 500) asks for more."""
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        for i in range(55):
            await _seed(
                session, score=90, confident=True, version=profile.version, content_hash=f"c{i}"
            )
        await session.commit()
        result = await run_llm_screen(
            session,
            settings=settings(llm_max_requests_per_run=500),
            profile=profile,
            weights=MatchWeights(),
            limit=500,
        )

    assert result.screened == 50
    assert result.skipped_budget == 5
    assert route.call_count == 50


@respx.mock
async def test_the_default_pass_reads_fifteen(session_factory, profile: Profile) -> None:
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        for i in range(20):
            await _seed(
                session, score=90, confident=True, version=profile.version, content_hash=f"d{i}"
            )
        await session.commit()
        result = await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights()
        )

    assert result.screened == 15
    assert route.call_count == 15


@respx.mock
async def test_one_job_can_be_read_on_request_even_off_the_shortlist(
    session_factory, profile: Profile
) -> None:
    """Low-confidence jobs are read from the drawer, one call at a time."""
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        job, match = await _seed(
            session, score=30, confident=False, version=profile.version, content_hash="prose"
        )
        await _seed(session, score=90, confident=True, version=profile.version, content_hash="x")
        await session.commit()
        result = await run_llm_screen(
            session,
            settings=settings(),
            profile=profile,
            weights=MatchWeights(),
            job_ids=[job.id],
            limit=1,
        )
        await session.refresh(match)

    assert result.screened == 1
    assert route.call_count == 1
    assert match.llm_used is True


@respx.mock
async def test_rows_preferences_exclude_are_never_read(session_factory, profile: Profile) -> None:
    """The estimate counts only preference-passing rows, so the pass must too —
    the first paid run read 118 excluded rows the confirm dialog never priced."""
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        await _seed(session, score=90, confident=True, version=profile.version, content_hash="a")
        _job, excluded = await _seed(
            session, score=90, confident=True, version=profile.version, content_hash="b"
        )
        excluded.prefs_pass = False
        await session.commit()

        result = await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights()
        )

    assert result.routed == 1
    assert route.call_count == 1
    assert excluded.llm_verdict is None


@respx.mock
async def test_a_second_pass_with_nothing_changed_calls_nothing(
    session_factory, profile: Profile
) -> None:
    """CLAUDE.md's acceptance criterion, and the reason the cache is two keys."""
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        await _seed(session, score=90, confident=True, version=profile.version)
        await session.commit()

        first = await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights()
        )
        second = await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights()
        )

    assert first.screened == 1
    assert second.screened == 0
    assert second.cached == 1
    assert route.call_count == 1


@respx.mock
async def test_a_changed_jd_invalidates_the_cached_verdict(
    session_factory, profile: Profile
) -> None:
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        job, match = await _seed(session, score=90, confident=True, version=profile.version)
        await session.commit()
        await run_llm_screen(session, settings=settings(), profile=profile, weights=MatchWeights())

        job.content_hash = "rewritten"
        await session.commit()

        again = await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights()
        )

    assert again.screened == 1
    assert route.call_count == 2


@respx.mock
async def test_a_different_profile_version_is_a_different_pairing(
    session_factory, profile: Profile
) -> None:
    """A score is a fact about a (job, profile) pair. Editing the résumé must
    not leave yesterday's verdict on screen looking current."""
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        await _seed(session, score=90, confident=True, version=profile.version)
        await session.commit()
        await run_llm_screen(session, settings=settings(), profile=profile, weights=MatchWeights())

        other = Profile(skills={"backend": {"go": 3}}, gaps={})
        assert other.version != profile.version
        result = await run_llm_screen(
            session, settings=settings(), profile=other, weights=MatchWeights()
        )

    # No JobMatch rows exist for the new version yet — the deterministic pass
    # owns creating those. The deep read must not invent them.
    assert result.routed == 0
    assert route.call_count == 1


@respx.mock
async def test_a_failed_row_is_recorded_not_left_looking_unread(
    session_factory, profile: Profile
) -> None:
    """A 90-minute pass is resumable, so "failed" and "not reached yet" have to
    be distinguishable in the table."""
    respx.post(COMPLETIONS).mock(return_value=httpx.Response(401, text="nope"))
    async with session_factory() as session:
        _job, match = await _seed(session, score=90, confident=True, version=profile.version)
        await session.commit()

        result = await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights()
        )
        await session.refresh(match)

    assert result.failed == 1
    assert result.screened == 0
    assert match.llm_used is False
    assert "error" in (match.llm_verdict or {})


@respx.mock
async def test_the_request_budget_stops_a_pass_early(session_factory, profile: Profile) -> None:
    route = respx.post(COMPLETIONS).mock(return_value=ok_response())
    async with session_factory() as session:
        for i in range(4):
            await _seed(
                session,
                score=90 - i,
                confident=True,
                version=profile.version,
                content_hash=f"h{i}",
            )
        await session.commit()

        result = await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights(), limit=2
        )

    assert result.screened == 2
    assert result.skipped_budget == 2
    assert route.call_count == 2


@respx.mock
async def test_the_highest_ranked_rows_are_read_first(session_factory, profile: Profile) -> None:
    """When the budget runs out it should have been spent on the rows most
    likely to be worth applying to."""
    titles: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        titles.append(json.loads(request.content)["messages"][1]["content"])
        return ok_response()

    respx.post(COMPLETIONS).mock(side_effect=capture)
    async with session_factory() as session:
        job_low, _ = await _seed(
            session, score=66, confident=True, version=profile.version, content_hash="low"
        )
        job_low.title = "Low Ranked Role"
        job_high, _ = await _seed(
            session, score=95, confident=True, version=profile.version, content_hash="high"
        )
        job_high.title = "High Ranked Role"
        await session.commit()

        await run_llm_screen(
            session, settings=settings(), profile=profile, weights=MatchWeights(), limit=1
        )

    assert "High Ranked Role" in titles[0]


async def test_a_missing_key_reports_rather_than_crashing_the_pass(
    session_factory, profile: Profile
) -> None:
    """Without a key the deep read is skipped; the deterministic ranking still
    stands on its own and the dashboard keeps working."""
    async with session_factory() as session:
        await _seed(session, score=90, confident=True, version=profile.version)
        await session.commit()
        result = await run_llm_screen(
            session,
            settings=Settings(llm_provider="groq", groq_api_key=None),
            profile=profile,
            weights=MatchWeights(),
        )
    assert result.error and "GROQ_API_KEY" in result.error
    assert result.screened == 0
