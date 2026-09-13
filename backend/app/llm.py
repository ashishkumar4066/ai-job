"""The deep-read pass — one LLM call per routed job, structured output.

The provider is `LLM_PROVIDER` in .env — `groq`, `mistral` or `cerebras`. All
speak the same OpenAI-style `/chat/completions` with `response_format:
json_schema`, so one client serves them all; what differs (key, model, base
URL, which rate limits exist, whether a 400 is retryable, which schema keywords
strict mode accepts) is resolved in `Settings.llm`.

What this is for
----------------
`matching.py` ranks every eligible row for free, and it is honest about the
limit of doing so: an overlap score computed from a JD that names three
technologies means something, and the same score from a JD that names none is
an artefact of the writing style. This module reads the ones the cheap pass
cannot settle.

**Validation and matching share one call.** CLAUDE.md asks for both — 2B wants
active-vs-ghost plus extracted fields, 2C wants fit and reasons — and the JD
has to be in the prompt either way. Two calls would double the only cost that
matters here.

The binding constraint is tokens, not time
------------------------------------------
Groq limits each model four ways — 30 requests/min, 1,000 requests/day, 8,000
tokens/min, 200,000 tokens/day on the free plan — and a single call costs
~1,800 tokens. Latency is ~2s. So concurrency buys nothing: per minute the
ceiling is ~4 jobs, and per DAY it is ~110, which is the one that actually
ends a pass. `app/ratelimit.py` paces every call under all four before it is
sent; see its docstring for which limits Groq reports and which it hides.

Choosing the model by measurement
---------------------------------
See `Settings.groq_model` for the numbers (measured on Groq; the Mistral
default has not been through the same probe yet). The short version: the largest
model was not the best one. `openai/gpt-oss-120b` intermittently emits JSON
that violates the strict schema it was given — 3 failures in 4 probe rows,
which no cheap retry policy fixes — and it read "Software Engineer II,
Enterprise AI Enablement" as an *excellent* match with no gaps, where
`qwen/qwen3.8-27b` read the same row as mid-level and named the missing
stacks. `gpt-oss-20b` cannot hold the schema at all.

Why every field carries a description and anchors
-------------------------------------------------
This is not decoration. Verified in an earlier session: without them
`gpt-oss-120b` answered a 0-100 scale on 0-10 and returned `fit_score: 6` for
a good match, stably, at temperature 0. Repeat runs also moved a numeric score
by ±7.5 while the extracted evidence stayed identical 5 times out of 5.

So this module **asks for a band, never a number**, and treats the extracted
evidence as the trustworthy half. A band the model can defend beats a score it
cannot.

Two schema details worth keeping. `compensation_text` is a plain string with
`""` meaning "not stated", never `["string", "null"]` — union types are the
usual trigger for strict-mode violations and the distinction buys nothing. And
it is named for what it holds, not for what it answers: while the field was
called `comp_stated` the model returned the literal string "No" on postings
that state no pay, ignoring a description that spelled out the empty-string
rule twice. A field NAME outweighs its description, which is also why
`location_policy` describes the posting instead of asking about the candidate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings, get_settings
from app.profile import Profile
from app.ratelimit import (
    LimitExhausted,
    RateLimiter,
    backoff_delay,
    format_wait,
    retry_after_seconds,
)

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The screen could not be produced for this job."""


class LLMNotConfigured(LLMError):
    """No API key for the selected provider. The pass is skipped, never faked."""


# --------------------------------------------------------------------------
# The contract with the model.
# --------------------------------------------------------------------------
# Kept as a literal dict rather than generated from the Pydantic model below:
# Groq's strict mode rejects several things Pydantic emits by default
# (`anyOf` for optionals, `$defs`/`$ref`, a missing `additionalProperties`),
# and silently-degraded structured output is the failure this whole module is
# built to avoid. The Pydantic model validates what comes back; this describes
# what to send. `test_llm.py` asserts the two stay in step.
SCREEN_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "posting_status",
        "status_reason",
        "fit_band",
        "fit_reasons",
        "strengths",
        "gaps",
        "seniority",
        "tech_stack",
        "sponsorship_required",
        "location_policy",
        "work_mode",
        "compensation_text",
        "inconsistencies",
    ],
    "properties": {
        "posting_status": {
            "type": "string",
            "enum": ["active", "evergreen", "ghost"],
            "description": (
                "Is this a real opening being hired for right now? 'active' = a specific role "
                "with concrete duties and a team. 'evergreen' = a perpetual talent-pool or "
                "pipeline post ('always looking', 'future openings', 'join our talent "
                "community'). 'ghost' = internally inconsistent, contentless, or an agency "
                "advert naming no real employer or duties. Default to 'active' unless the text "
                "gives positive evidence otherwise."
            ),
        },
        "status_reason": {
            "type": "string",
            "description": "One sentence, quoting the phrase from the posting that decided posting_status.",
        },
        "fit_band": {
            "type": "string",
            "enum": ["excellent", "strong", "moderate", "weak", "poor"],
            "description": (
                "How well the CANDIDATE PROFILE fits this job's stated requirements. Anchors: "
                "'excellent' = the JD's core stack and seniority are the candidate's strongest "
                "skills, years align, and no blocking requirement is missing. "
                "'strong' = most core requirements met, at most one non-blocking gap. "
                "'moderate' = right role family, but it leans on a stack the candidate has not "
                "shipped, or the seniority is off by one level. "
                "'weak' = the JD centres on the candidate's listed gap technologies, or wants a "
                "different specialization. "
                "'poor' = a different job entirely, or a hard blocker such as requiring work "
                "authorization the candidate does not have. "
                "Judge the FIT to this candidate, not how attractive the job is."
            ),
        },
        "fit_reasons": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 4,
            "description": (
                "2-4 short reasons for fit_band, each naming a specific requirement the posting "
                "actually states. Include the reasons against, not only the ones for."
            ),
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
            "description": "Candidate skills this posting explicitly asks for, in the posting's own wording.",
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
            "description": (
                "Requirements the posting states that the profile does not evidence. "
                "Empty array if there are none. Do not invent a gap the posting never asks for."
            ),
        },
        "seniority": {
            "type": "string",
            "enum": ["intern", "junior", "mid", "senior", "staff", "lead", "unclear"],
            "description": (
                "The level this posting is pitched at, from its title and stated requirements. "
                "'unclear' when the posting never indicates a level."
            ),
        },
        "tech_stack": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
            "description": (
                "Technologies the posting names as requirements. Only ones it actually writes "
                "down — an empty array is the correct answer for a JD that names none."
            ),
        },
        "sponsorship_required": {
            "type": "string",
            "enum": ["yes", "no", "unstated"],
            "description": (
                "'yes' = the posting requires existing work authorization the candidate lacks "
                "(e.g. 'must be authorized to work in the US without sponsorship'). "
                "'no' = it offers sponsorship or is open worldwide. 'unstated' = silent."
            ),
        },
        "location_policy": {
            "type": "string",
            "enum": ["open_to_india", "excludes_india", "unstated"],
            "description": (
                "What THE POSTING says about where a candidate may live. Describe the posting's "
                "own policy and ignore the candidate completely. "
                "'open_to_india' = it names India, IST, worldwide or anywhere, OR the job itself "
                "is located in an Indian city (Bengaluru, Hyderabad, Ahmedabad, ...). A job in "
                "India is open_to_india even when it is on-site. "
                "'excludes_india' = it restricts hiring to a region that does not include India, "
                "e.g. 'US applicants only', 'must reside in the EU'. "
                "'unstated' = the posting does not say. "
                "Whether the work is remote or on-site is a different question — answer that in "
                "work_mode and never let it change this field."
            ),
        },
        "work_mode": {
            "type": "string",
            "enum": ["remote", "hybrid", "onsite", "unstated"],
            "description": (
                "How the posting says the work is done. 'remote' = fully remote or "
                "work-from-anywhere. 'hybrid' = some days in an office. 'onsite' = requires "
                "working from a specific location. 'unstated' = the posting does not say."
            ),
        },
        "compensation_text": {
            "type": "string",
            "description": (
                "The compensation sentence copied VERBATIM from the posting, e.g. "
                "'$120k - $150k' or 'INR 25,00,000 per annum'. This is a quotation, not a "
                "question: if the posting mentions no pay at all, return an EMPTY STRING. "
                "Never answer 'No', 'None', 'N/A' or 'Not stated'."
            ),
        },
        "inconsistencies": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 4,
            "description": (
                "Internal contradictions in the posting — a title saying Senior while the body "
                "asks for one year, a remote tag with an on-site requirement. Empty if none."
            ),
        },
    },
}

_SYSTEM_PROMPT: Final[str] = (
    "You screen job postings for one specific candidate. Judge only what the posting text "
    "states; never assume a requirement it does not write down. Every field has a description "
    "and explicit anchors — follow them exactly. Prefer the evidence you can quote over your "
    "impression of the role."
)


class JobScreen(BaseModel):
    """One posting's screen. Mirrors `SCREEN_SCHEMA`."""

    model_config = ConfigDict(frozen=True)

    posting_status: str
    status_reason: str = ""
    fit_band: str
    fit_reasons: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    seniority: str = "unclear"
    tech_stack: list[str] = Field(default_factory=list)
    sponsorship_required: str = "unstated"
    location_policy: str = "unstated"
    work_mode: str = "unstated"
    compensation_text: str = ""
    inconsistencies: list[str] = Field(default_factory=list)

    # One call, two answers, two cache keys — and therefore two homes.
    #
    # VALIDITY_FIELDS describe the POSTING: is it real, what level is it
    # pitched at, what does it ask for, does it demand work authorization.
    # None of that depends on who is reading, so it is cached on
    # `JobPosting.llm_validity` under `content_hash` alone and survives a
    # profile edit.
    #
    # FIT_FIELDS describe the PAIRING of this posting with this profile, so
    # they are cached on `JobMatch.llm_verdict` under
    # `content_hash + profile_version`.
    #
    # The split is what stops a résumé edit from stranding validity answers
    # that were already paid for. Under a single key they lived in a
    # `job_matches` row keyed to the old `profile_version`, so after an edit
    # the validity layer could no longer see them — and any row that dropped
    # out of LLM routing on its new score lost its verdict entirely and had
    # to be re-read to badge the Jobs tile.
    VALIDITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "posting_status",
        "status_reason",
        "seniority",
        "tech_stack",
        "sponsorship_required",
        "location_policy",
        "work_mode",
        "compensation_text",
        "inconsistencies",
    )
    FIT_FIELDS: ClassVar[tuple[str, ...]] = (
        "fit_band",
        "fit_reasons",
        "strengths",
        "gaps",
    )

    def validity_half(self) -> dict[str, Any]:
        """The posting-only fields, for `JobPosting.llm_validity`."""
        return {name: getattr(self, name) for name in self.VALIDITY_FIELDS}

    def fit_half(self) -> dict[str, Any]:
        """The pairing-only fields, for `JobMatch.llm_verdict`.

        `blocked` is carried here rather than recomputed by readers: it is
        derived from validity fields but it is a statement about whether *this
        candidate* can take the job, which makes it a fit fact. Storing it
        also means the dashboard need not re-implement the rule.
        """
        out = {name: getattr(self, name) for name in self.FIT_FIELDS}
        out["blocked"] = self.blocked
        return out

    @property
    def blocked(self) -> bool:
        """A hard stop the deterministic pass cannot see.

        Rule 1 of eligibility reads the board's *structured* location fields.
        This reads the JD prose, which is where "must be authorized to work in
        the US without sponsorship" actually lives.

        `work_mode == "onsite"` counts because `profile.work_authorization`
        says remote-only. It is separate from `location_policy` on purpose: the
        first live run folded the two together and reported Steps AI's
        "On-Site, Hyderabad, India" role as "India cannot hold this" — the
        right answer for the wrong reason, and the wrong reason is what the
        dashboard would have shown.
        """
        return (
            self.sponsorship_required == "yes"
            or self.location_policy == "excludes_india"
            or self.work_mode == "onsite"
        )


def profile_brief(profile: Profile) -> str:
    """The candidate half of the prompt.

    A brief, not the whole `profile.yaml`. Everything here is scoring-relevant
    and nothing else is: the file also carries contact details, links and
    Phase 3 form answers, none of which help judge a JD and all of which would
    be billed on every one of the routed rows.

    Skills are grouped by weight rather than listed flat, because the weights
    are the point — `fit_band`'s anchors refer to "the candidate's strongest
    skills", and a flat list gives the model no way to tell which those are.
    """
    by_weight: dict[int, list[str]] = {}
    for group in profile.skills.values():
        for name, weight in group.items():
            by_weight.setdefault(int(weight), []).append(name)

    lines = [
        "CANDIDATE PROFILE",
        f"Seniority: {profile.seniority.total_years} years total, "
        f"{profile.seniority.ai_years} in AI. "
        f"Current title: {profile.seniority.current_title}.",
        f"Based in {profile.identity.location} ({profile.identity.timezone}). "
        f"Remote only: {profile.work_authorization.remote_only}.",
        f"Work authorization: citizen of {profile.work_authorization.citizenship}, "
        f"authorized in {', '.join(profile.work_authorization.authorized_in)}. "
        + (
            "REQUIRES visa sponsorship for roles outside those countries."
            if profile.work_authorization.needs_sponsorship
            else "Needs no sponsorship."
        ),
    ]
    for weight in sorted(by_weight, reverse=True):
        label = {3: "Strongest skills (shipped repeatedly)", 2: "Solid working experience"}.get(
            weight, "Used, would not headline"
        )
        lines.append(f"{label}: {', '.join(sorted(by_weight[weight]))}.")
    if profile.gaps:
        lines.append(
            "NOT SHIPPED — treat as genuine gaps when a posting requires them: "
            f"{', '.join(sorted(profile.gaps))}."
        )
    if profile.seniority.target_titles:
        lines.append(f"Target titles: {', '.join(profile.seniority.target_titles)}.")
    lines.append(
        f"Compensation floor: INR {profile.compensation.min_annual_inr:,.0f} per year."
    )
    return "\n".join(lines)


def build_messages(
    *,
    title: str,
    company: str,
    description_text: str | None,
    profile_text: str,
    max_jd_chars: int,
) -> list[dict[str, str]]:
    """The two-message prompt. JD last, so truncation never eats the profile."""
    jd = (description_text or "").strip() or "(this posting has no description text)"
    if len(jd) > max_jd_chars:
        jd = jd[:max_jd_chars] + "\n...[truncated]"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{profile_text}\n\nJOB POSTING\nTitle: {title}\nCompany: {company}\n\n{jd}"
            ),
        },
    ]


def estimate_tokens(messages: list[dict[str, str]], completion_reserve: int) -> int:
    """Rough prompt size plus a generous answer reservation.

    Deliberately no tokenizer: adding one would mean a model-specific
    dependency to approximate a number that only has to be in the right
    neighbourhood. Measured against real usage, chars/3.6 tracks Groq's
    reported `prompt_tokens` within a few percent on these JDs, and erring
    high is the safe direction.
    """
    chars = sum(len(m["content"]) for m in messages)
    return int(chars / 3.6) + completion_reserve


class LLMRateLimited(LLMError):
    """A limit that will not clear soon — usually the daily token cap. Stop the pass."""

    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class UsageEvent:
    """One billed call, for the `llm_usage` ledger the daily limits read."""

    created_at: datetime
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    status_code: int


# Transient server-side failures, retried with exponential backoff. 498 is
# Groq's flex-tier "capacity exceeded"; the rest are the standard set.
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 498, 500, 502, 503, 504})


def _without_keywords(node: Any, drop: frozenset[str]) -> Any:
    """A copy of a JSON Schema with `drop` removed at every level.

    Only schema keywords are dropped: under `properties` the keys are field
    names, which must survive even if one happened to share a keyword's name.
    """
    if not drop:
        return node
    if isinstance(node, list):
        return [_without_keywords(item, drop) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in drop:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _without_keywords(spec, drop) for name, spec in value.items()}
        else:
            out[key] = _without_keywords(value, drop)
    return out


def build_limiter(settings: Settings) -> RateLimiter:
    provider = settings.llm
    return RateLimiter(
        limits=provider.limits,
        headroom=settings.llm_budget_headroom,
        max_wait=settings.llm_max_wait_seconds,
        header_scheme=provider.header_scheme,
    )


class LLMScreener:
    """Calls the selected provider for one job at a time, paced under its rate limits.

    Pacing happens BEFORE each call (`RateLimiter.acquire`). Reacting to
    429s alone was measured and is strictly worse: the probe run tripped the
    limit on its third call and every retry paid the full prompt again.

    Retry policy, by response:

    * **429** — a window refused the call. Wait `max(named, backoff(n))`: the
      server's `retry-after` is a floor, and exponential backoff grows past it
      while 429s keep coming, so a window that is not clearing is not re-hit on
      the same short cadence. A named wait over `llm_max_wait_seconds` is a
      daily cap and stops the pass. 429s have their own budget
      (`llm_max_rate_limit_retries`) — they are not the row's fault.
    * **408/498/5xx, network errors** — transient. Exponential backoff,
      counted against `llm_max_retries`.
    * **400** — on Groq, a strict-schema violation by the model; intermittent,
      so retried at once against `llm_max_retries`. On other providers a 400
      is a malformed request and will not fix itself, so it fails the row.
    * **anything else** (401/403/404/413/422) — will not fix itself. Fail now.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = self.settings.llm
        if not self.provider.api_key:
            raise LLMNotConfigured(
                f"{self.provider.key_env} is not set (LLM_PROVIDER={self.provider.name})"
            )
        self._client = client
        self._owns_client = client is None
        self.limiter = limiter or build_limiter(self.settings)
        self._sleep = sleep
        self._rng = rng
        self.requests_made = 0
        self.tokens_spent = 0
        self.rate_limited = 0
        self.schema_retries = 0
        # Billed calls not yet written to the ledger; the runner drains these.
        self.usage: list[UsageEvent] = []

    async def __aenter__(self) -> LLMScreener:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def drain_usage(self) -> list[UsageEvent]:
        events, self.usage = self.usage, []
        return events

    def _body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        schema = _without_keywords(SCREEN_SCHEMA, self.provider.unsupported_schema_keywords)
        body: dict[str, Any] = {
            "model": self.provider.model,
            "temperature": 0,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "job_screen", "strict": True, "schema": schema},
            },
        }
        # Resolved per provider and model: Groq's qwen 400s on it.
        if self.provider.reasoning_effort:
            body["reasoning_effort"] = self.provider.reasoning_effort
        if self.provider.max_completion_tokens:
            body["max_completion_tokens"] = self.provider.max_completion_tokens
        return body

    def _backoff(self, retry: int) -> float:
        return backoff_delay(
            retry,
            base=self.settings.llm_backoff_base_seconds,
            cap=self.settings.llm_backoff_max_seconds,
            rng=self._rng,
        )

    def _record(self, status: int, usage: dict[str, Any], fallback_total: int) -> int:
        """Book one billed call for the ledger; returns its total tokens."""
        total = int(usage.get("total_tokens") or fallback_total)
        self.usage.append(
            UsageEvent(
                created_at=datetime.now(UTC),
                model=self.provider.model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=total,
                status_code=status,
            )
        )
        self.tokens_spent += total
        return total

    async def screen(
        self, *, title: str, company: str, description_text: str | None, profile_text: str
    ) -> JobScreen:
        """Screen one posting. Raises `LLMError` rather than returning a guess."""
        if self._client is None:
            raise LLMError("LLMScreener used outside its async context")

        messages = build_messages(
            title=title,
            company=company,
            description_text=description_text,
            profile_text=profile_text,
            max_jd_chars=self.settings.llm_max_jd_chars,
        )
        # Cerebras admits a call only if prompt + `max_completion_tokens` fits
        # its bucket, so reserve that ceiling; `settle` swaps in actual usage.
        estimate = estimate_tokens(
            messages,
            max(self.settings.llm_completion_reserve, self.provider.max_completion_tokens or 0),
        )
        body = self._body(messages)
        last_error = "unknown"
        retries = 0  # schema / transient failures: the row's budget
        throttled = 0  # 429s: the window's budget

        def spend_retry() -> None:
            nonlocal retries
            if retries >= self.settings.llm_max_retries:
                raise LLMError(last_error)
            retries += 1

        while True:
            try:
                reservation = await self.limiter.acquire(estimate, sleep=self._sleep)
            except LimitExhausted as exc:
                raise LLMRateLimited(str(exc), exc.retry_after) from exc

            try:
                response = await self._client.post(
                    f"{self.provider.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.provider.api_key}"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                self.limiter.release(reservation)
                last_error = f"{type(exc).__name__}: {exc}"
                spend_retry()
                wait = self._backoff(retries - 1)
                log.info(
                    "llm.network_retry", extra={"sleep_s": round(wait, 2), "error": last_error}
                )
                await self._sleep(wait)
                continue

            self.requests_made += 1
            self.limiter.observe(response.headers)
            status = response.status_code

            if status == 429:
                self.limiter.release(reservation)
                self.rate_limited += 1
                throttled += 1
                named = retry_after_seconds(response.headers, response.text)
                last_error = f"rate_limited: {response.text[:200]}"
                if named is not None and named > self.settings.llm_max_wait_seconds:
                    raise LLMRateLimited(
                        f"rate_limited: {self.provider.name} asks for {format_wait(named)} — "
                        f"{response.text[:200]}",
                        named,
                    )
                if throttled > self.settings.llm_max_rate_limit_retries:
                    raise LLMError(last_error)
                wait = max(named or 0.0, self._backoff(throttled - 1))
                log.info(
                    "llm.rate_limited",
                    extra={
                        "sleep_s": round(wait, 2),
                        "retry": throttled,
                        "named_s": named,
                        # The body names which limit refused the call. Always
                        # kept, so a limit nobody planned for shows up here.
                        "body": response.text[:200],
                    },
                )
                await self._sleep(wait)
                continue

            if status in _RETRYABLE_STATUS:
                self.limiter.release(reservation)
                last_error = f"HTTP {status}: {response.text[:200]}"
                spend_retry()
                wait = self._backoff(retries - 1)
                log.info("llm.server_retry", extra={"status": status, "sleep_s": round(wait, 2)})
                await self._sleep(wait)
                continue

            if status == 400 and self.provider.retries_bad_request:
                # The model generated and then violated its schema. Groq sends
                # no usage on this error, so the estimate is booked instead —
                # overcounting is the safe direction against a daily cap.
                self.limiter.settle(reservation, self._record(status, {}, estimate))
                last_error = f"HTTP 400: {response.text[:200]}"
                self.schema_retries += 1
                spend_retry()
                log.info("llm.schema_retry", extra={"retry": retries})
                continue

            if status != 200:
                self.limiter.release(reservation)
                raise LLMError(f"HTTP {status}: {response.text[:200]}")

            payload = response.json()
            actual = self._record(status, payload.get("usage") or {}, estimate)
            self.limiter.settle(reservation, actual)

            choice = (payload.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                # Cut off at `max_completion_tokens`, usually mid-reasoning. A
                # retry at temperature 0 hits the same ceiling, so fail now and
                # say which knob to turn.
                raise LLMError(
                    f"completion truncated at max_completion_tokens="
                    f"{self.provider.max_completion_tokens} — raise it"
                )
            content = choice.get("message", {}).get("content")
            if not content:
                last_error = "empty completion"
                spend_retry()
                continue
            try:
                return JobScreen.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError) as exc:
                # Strict mode is supposed to make this impossible. It is not,
                # so the parse is guarded and the row fails loudly.
                last_error = f"unparseable screen: {type(exc).__name__}: {exc}"
                spend_retry()

