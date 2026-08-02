"""Telegram alerts for newly-discovered jobs.

A notification failure must never fail an ingest run — the jobs are already
safely persisted by the time we get here, so every error is logged and
swallowed.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Protocol

import httpx

from app.config import Settings, get_settings
from app.models import JobPosting

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_MESSAGE_CHARS = 4096


class Notifier(Protocol):
    async def notify_new_jobs(self, jobs: list[JobPosting]) -> int: ...


def _format_job(job: JobPosting) -> str:
    location = ", ".join(job.locations[:3]) if job.locations else ("Remote" if job.remote else "—")
    if job.locations and len(job.locations) > 3:
        location += f" (+{len(job.locations) - 3} more)"

    lines = [
        "🆕 <b>New job</b>",
        f"<b>{html.escape(job.title)}</b>",
        f"🏢 {html.escape(job.company)}",
        f"📍 {html.escape(location)}" + ("  •  🌐 Remote" if job.remote else ""),
    ]
    if job.department:
        lines.append(f"🗂 {html.escape(job.department)}")
    if job.posted_at:
        lines.append(f"🗓 Posted {job.posted_at:%Y-%m-%d}")
    lines.append(f'\n<a href="{html.escape(job.apply_url, quote=True)}">Apply →</a>')

    message = "\n".join(lines)
    return message[:_MAX_MESSAGE_CHARS]


class NullNotifier:
    """Used when Telegram is unconfigured or notifications are disabled."""

    def __init__(self, reason: str = "notifications disabled") -> None:
        self.reason = reason
        self.sent: list[JobPosting] = []

    async def notify_new_jobs(self, jobs: list[JobPosting]) -> int:
        if jobs:
            log.info(
                "notify.skipped",
                extra={"count": len(jobs), "reason": self.reason},
            )
        self.sent.extend(jobs)
        return 0


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_per_run: int = 25,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._client = client
        self._owns_client = client is None
        self._max_per_run = max_per_run

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=get_settings().http_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send_text(self, text: str) -> bool:
        try:
            client = await self._get_client()
            response = await client.post(
                TELEGRAM_API.format(token=self._token),
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code != 200:
                log.warning(
                    "notify.telegram_rejected",
                    extra={"status": response.status_code, "body": response.text[:300]},
                )
                return False
            return True
        except Exception as exc:
            log.warning("notify.telegram_failed", extra={"error": str(exc)})
            return False

    async def notify_new_jobs(self, jobs: list[JobPosting]) -> int:
        """Send one message per new job. Returns how many were delivered."""
        if not jobs:
            return 0

        to_send = jobs[: self._max_per_run]
        suppressed = len(jobs) - len(to_send)
        sent = 0

        for index, job in enumerate(to_send):
            if await self.send_text(_format_job(job)):
                sent += 1
            # Telegram throttles at ~30 messages/second; stay well under.
            if index < len(to_send) - 1:
                await asyncio.sleep(0.05)

        if suppressed:
            await self.send_text(
                f"… and <b>{suppressed}</b> more new jobs this run "
                f"(capped at {self._max_per_run}). Check the dashboard."
            )
            log.info("notify.capped", extra={"suppressed": suppressed})

        log.info("notify.sent", extra={"sent": sent, "requested": len(jobs)})
        return sent


def build_notifier(
    settings: Settings | None = None, *, client: httpx.AsyncClient | None = None
) -> Notifier:
    settings = settings or get_settings()
    if not settings.notifications_enabled:
        return NullNotifier("notifications_enabled=false")
    if not settings.telegram_configured:
        return NullNotifier("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    return TelegramNotifier(
        settings.telegram_bot_token,  # type: ignore[arg-type]
        settings.telegram_chat_id,  # type: ignore[arg-type]
        client=client,
        max_per_run=settings.max_notifications_per_run,
    )
