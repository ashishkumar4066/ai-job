"""Telegram notifier tests."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import respx

from app.models import JobPosting
from app.notify.telegram import NullNotifier, TelegramNotifier, _format_job, build_notifier

SEND_URL = "https://api.telegram.org/bottest-token/sendMessage"


def make_job(**overrides: object) -> JobPosting:
    defaults: dict[str, object] = {
        "id": 1,
        "source_key": "greenhouse:stripe:1",
        "source_id": "greenhouse:stripe",
        "ats": "greenhouse",
        "company": "Stripe",
        "title": "Staff Engineer",
        "locations": ["San Francisco, CA"],
        "remote": False,
        "department": "Engineering",
        "apply_url": "https://boards.greenhouse.io/stripe/jobs/1",
        "posted_at": datetime(2026, 7, 1, tzinfo=UTC),
        "first_seen_at": datetime(2026, 8, 1, tzinfo=UTC),
        "last_seen_at": datetime(2026, 8, 1, tzinfo=UTC),
        "status": "open",
    }
    defaults.update(overrides)
    return JobPosting(**defaults)


class TestFormatting:
    def test_includes_the_essentials(self) -> None:
        message = _format_job(make_job())
        assert "Staff Engineer" in message
        assert "Stripe" in message
        assert "San Francisco, CA" in message
        assert "https://boards.greenhouse.io/stripe/jobs/1" in message

    def test_escapes_html_in_titles(self) -> None:
        message = _format_job(make_job(title="Engineer <script>alert(1)</script>"))
        assert "<script>" not in message
        assert "&lt;script&gt;" in message

    def test_marks_remote(self) -> None:
        assert "Remote" in _format_job(make_job(remote=True, locations=[]))

    def test_summarizes_many_locations(self) -> None:
        message = _format_job(make_job(locations=["A", "B", "C", "D", "E"]))
        assert "+2 more" in message

    def test_truncates_to_telegram_limit(self) -> None:
        assert len(_format_job(make_job(title="x" * 9000))) <= 4096


class TestSending:
    @respx.mock
    async def test_sends_one_message_per_job(self) -> None:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        async with httpx.AsyncClient() as client:
            notifier = TelegramNotifier("test-token", "123", client=client)
            sent = await notifier.notify_new_jobs([make_job(id=1), make_job(id=2)])

        assert sent == 2
        assert route.call_count == 2

    @respx.mock
    async def test_api_error_is_swallowed(self) -> None:
        respx.post(SEND_URL).mock(return_value=httpx.Response(403, text="forbidden"))
        async with httpx.AsyncClient() as client:
            notifier = TelegramNotifier("test-token", "123", client=client)
            assert await notifier.notify_new_jobs([make_job()]) == 0

    @respx.mock
    async def test_network_error_is_swallowed(self) -> None:
        respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("no route"))
        async with httpx.AsyncClient() as client:
            notifier = TelegramNotifier("test-token", "123", client=client)
            assert await notifier.notify_new_jobs([make_job()]) == 0

    @respx.mock
    async def test_caps_messages_per_run(self) -> None:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        async with httpx.AsyncClient() as client:
            notifier = TelegramNotifier("test-token", "123", client=client, max_per_run=2)
            sent = await notifier.notify_new_jobs([make_job(id=i) for i in range(5)])

        assert sent == 2
        assert route.call_count == 3  # 2 jobs + 1 "and N more" summary

    async def test_empty_list_sends_nothing(self) -> None:
        async with httpx.AsyncClient() as client:
            notifier = TelegramNotifier("test-token", "123", client=client)
            assert await notifier.notify_new_jobs([]) == 0


class TestBuildNotifier:
    def test_null_notifier_when_unconfigured(self, db: None) -> None:  # noqa: ARG002
        from app.config import Settings

        settings = Settings(notifications_enabled=True, telegram_bot_token=None)
        assert isinstance(build_notifier(settings), NullNotifier)

    def test_null_notifier_when_disabled(self) -> None:
        from app.config import Settings

        settings = Settings(
            notifications_enabled=False, telegram_bot_token="t", telegram_chat_id="c"
        )
        assert isinstance(build_notifier(settings), NullNotifier)

    def test_telegram_notifier_when_configured(self) -> None:
        from app.config import Settings

        settings = Settings(
            notifications_enabled=True, telegram_bot_token="t", telegram_chat_id="c"
        )
        assert isinstance(build_notifier(settings), TelegramNotifier)
