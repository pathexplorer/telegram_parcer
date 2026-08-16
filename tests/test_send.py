"""
Unit tests for telegram.send — Bot API notifications, keyword alerts,
and health/operational alerts.

Covers:
  - ``send_bot_notification()`` — HTTP POST to Telegram Bot API
  - ``send_alert()`` — keyword match alert formatting
  - ``send_health_alert()`` — operational health alert formatting
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


# ============================================================================
# send_bot_notification  (tested via a proper async-context-manager mock)
# ============================================================================

class _FakeSessionCtx:
    """Async context manager mock for aiohttp.ClientSession — same pattern
    used by conftest.py for TelegramClient, which is known to work."""

    def __init__(self, *args, **kwargs):
        self._resp_status = 200
        self._resp_text = "ok"
        # Use MagicMock for .post so we can inspect call_args
        self.post = MagicMock()
        self.post.side_effect = self._make_response

    def _make_response(self, *args, **kwargs):
        status = self._resp_status
        text = self._resp_text
        headers = {}
        if isinstance(status, (list, tuple)):
            status, headers = status
        return _FakeResponse(status, text, headers)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _FakeResponse:
    """Async context manager for aiohttp.ClientResponse."""

    def __init__(self, status=200, text="ok", headers=None):
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self):
        return self._text


class TestSendBotNotification:
    """Bot API HTTP notification delivery."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "123:test_bot_token")
        for mod in list(sys.modules):
            if mod.startswith("project_env") or mod.startswith("telegram.send"):
                del sys.modules[mod]
        monkeypatch.setenv("NOTIFICATION_CHAT", "-1001234567890")

    def test_successful_notification(self):
        """A 200 OK from Bot API should succeed."""
        fake_session = _FakeSessionCtx()

        with patch("aiohttp.ClientSession", return_value=fake_session):
            import telegram.send
            asyncio.run(telegram.send.send_bot_notification("Hello from test!"))

        call_args = fake_session.post.call_args
        assert "api.telegram.org/bot123:test_bot_token/sendMessage" in call_args[0][0]
        assert call_args[1]["json"]["chat_id"] == -1001234567890
        assert call_args[1]["json"]["text"] == "Hello from test!"

    def test_failed_notification_raises_runtime_error(self):
        """Non-200 response must raise RuntimeError."""
        fake_session = _FakeSessionCtx()
        fake_session._resp_status = 403
        fake_session._resp_text = "Forbidden"

        with patch("aiohttp.ClientSession", return_value=fake_session):
            import telegram.send
            with pytest.raises(RuntimeError, match="Bot API returned 403"):
                asyncio.run(telegram.send.send_bot_notification("This will fail"))

    def test_parse_error_falls_back_to_plain_text(self):
        """A 400 'can't parse entities' must retry as plain text (no
        parse_mode) instead of dropping the alert (regression for the
        recurring 'can't parse entities' error)."""
        fake_session = _FakeSessionCtx()
        responses = {
            "markdown": (400, '{"ok":false,"description":"Bad Request: '
                               'can\'t parse entities: Can\'t find end of the '
                               'entity"}'),
            "plain": (200, "ok"),
        }

        original_post = fake_session.post

        def _side_effect(*args, **kwargs):
            if "parse_mode" in kwargs.get("json", {}):
                return _FakeResponse(*responses["markdown"])
            return _FakeResponse(*responses["plain"])

        fake_session.post.side_effect = _side_effect

        with patch("aiohttp.ClientSession", return_value=fake_session):
            import telegram.send
            # Should NOT raise: falls back to plain text and succeeds.
            asyncio.run(telegram.send.send_bot_notification("bad *markdown"))

        # Ensure a plain-text (no parse_mode) call was attempted.
        plain_calls = [
            c for c in fake_session.post.call_args_list
            if "parse_mode" not in c.kwargs.get("json", {})
        ]
        assert plain_calls, "expected a plain-text fallback call"

    def test_retries_transient_then_succeeds(self):
        """A transient 500 followed by 200 must retry and succeed."""
        fake_session = _FakeSessionCtx()
        responses = [(500, '{"ok":false}'), (200, "ok")]
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            status = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return _FakeResponse(*status)

        fake_session.post.side_effect = _side_effect

        with patch("telegram.send.asyncio.sleep", AsyncMock()), \
             patch("aiohttp.ClientSession", return_value=fake_session):
            import telegram.send
            asyncio.run(telegram.send.send_bot_notification("retry me"))

        assert call_count == 2

    def test_retries_exhausted_raises(self):
        """Persistent transient 500s must raise after _MAX_RETRIES attempts."""
        fake_session = _FakeSessionCtx()
        fake_session._resp_status = 500
        fake_session._resp_text = '{"ok":false}'

        with patch("telegram.send.asyncio.sleep", AsyncMock()), \
             patch("aiohttp.ClientSession", return_value=fake_session):
            import telegram.send
            with pytest.raises(RuntimeError, match="delivery failed"):
                asyncio.run(telegram.send.send_bot_notification("always fails"))

        assert fake_session.post.call_count == telegram.send._MAX_RETRIES

    def test_retry_after_honored(self):
        """A 429 with Retry-After must wait that long before retrying."""
        fake_session = _FakeSessionCtx()
        responses = [(429, '{"ok":false}', {"Retry-After": "7"}), (200, "ok", {})]
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            status, text, headers = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return _FakeResponse(status, text, headers)

        fake_session.post.side_effect = _side_effect

        with patch("telegram.send.asyncio.sleep", AsyncMock()) as mock_sleep, \
             patch("aiohttp.ClientSession", return_value=fake_session):
            import telegram.send
            asyncio.run(telegram.send.send_bot_notification("rate limited"))

        assert call_count == 2
        mock_sleep.assert_called_once_with(7)


# ============================================================================
# send_alert  (test formatting — mocks send_bot_notification to avoid HTTP)
# ============================================================================

class TestSendAlert:
    """Keyword match alert formatting and delivery."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "123:test_bot_token")
        for mod in list(sys.modules):
            if mod.startswith("project_env") or mod.startswith("telegram.send"):
                del sys.modules[mod]
        monkeypatch.setenv("NOTIFICATION_CHAT", "-1001234567890")

    @pytest.fixture
    def message(self):
        msg = MagicMock()
        msg.id = 555
        msg.text = "URGENT: Something happened! " + "extra text " * 30
        chat = MagicMock()
        chat.id = -100111222333
        chat.username = "test_channel"
        msg.get_chat = AsyncMock(return_value=chat)
        return msg

    def test_alert_formatting_and_delivery(self, message):
        mock_notify = AsyncMock()

        with patch("telegram.send.send_bot_notification", mock_notify):
            import telegram.send
            asyncio.run(telegram.send.send_alert(message, ["urgent", "keyword2"]))

        mock_notify.assert_called_once()
        payload_text = mock_notify.call_args[0][0]
        assert "URGENT: Something" in payload_text
        assert "urgent" in payload_text.lower()
        assert "keyword2" in payload_text.lower()
        assert "test\\_channel" in payload_text

    def test_alert_shows_first_name_when_no_username(self, message):
        chat = MagicMock()
        chat.id = 999
        chat.username = None
        chat.title = None
        chat.first_name = "Private Chat"
        message.get_chat = AsyncMock(return_value=chat)

        mock_notify = AsyncMock()

        with patch("telegram.send.send_bot_notification", mock_notify):
            import telegram.send
            asyncio.run(telegram.send.send_alert(message, ["test"]))

        assert "Private Chat" in mock_notify.call_args[0][0]

    def test_alert_fallback_to_id_string(self, message):
        chat = MagicMock()
        chat.id = 999888
        chat.username = None
        chat.title = None
        chat.first_name = None
        message.get_chat = AsyncMock(return_value=chat)

        mock_notify = AsyncMock()

        with patch("telegram.send.send_bot_notification", mock_notify):
            import telegram.send
            asyncio.run(telegram.send.send_alert(message, ["test"]))

        assert "999888" in mock_notify.call_args[0][0]

    def test_alert_escapes_malformed_markdown_in_message_text(self):
        """Raw user text with stray/unclosed Markdown chars must be escaped
        so it can't break the alert message (regression for the recurring
        'can't parse entities' 400 error)."""
        msg = MagicMock()
        msg.id = 777
        msg.text = "Price *is _broken [today: `see\u00a0"  # unclosed entities
        chat = MagicMock()
        chat.username = "chan"
        chat.id = -100555
        msg.get_chat = AsyncMock(return_value=chat)

        mock_notify = AsyncMock()
        with patch("telegram.send.send_bot_notification", mock_notify):
            import telegram.send
            asyncio.run(telegram.send.send_alert(msg, ["price"]))

        payload_text = mock_notify.call_args[0][0]
        assert "\\*is" in payload_text
        assert "\\_" in payload_text
        assert "\\[" in payload_text
        assert "\\`" in payload_text


# ============================================================================
# send_health_alert
# ============================================================================

class TestSendHealthAlert:
    """Operational health/status alerts."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "123:test_bot_token")
        for mod in list(sys.modules):
            if mod.startswith("project_env") or mod.startswith("telegram.send"):
                del sys.modules[mod]
        monkeypatch.setenv("NOTIFICATION_CHAT", "-1001234567890")

    def test_error_health_alert(self):
        mock_notify = AsyncMock()

        with patch("telegram.send.send_bot_notification", mock_notify):
            import telegram.send
            asyncio.run(telegram.send.send_health_alert(
                "Test Error", "Something broke", level="error"
            ))

        payload = mock_notify.call_args[0][0]
        assert "❌" in payload
        assert "Test Error" in payload

    def test_warning_health_alert(self):
        mock_notify = AsyncMock()

        with patch("telegram.send.send_bot_notification", mock_notify):
            import telegram.send
            asyncio.run(telegram.send.send_health_alert(
                "Warning Title", "Some warning", level="warning"
            ))

        payload = mock_notify.call_args[0][0]
        assert "⚠️" in payload
        assert "Warning Title" in payload

    def test_default_level_is_error(self):
        """When level is not specified, default to 'error' → ❌ emoji."""
        mock_notify = AsyncMock()

        with patch("telegram.send.send_bot_notification", mock_notify):
            import telegram.send
            asyncio.run(telegram.send.send_health_alert(
                "Default Title", "Some body"
            ))

        payload = mock_notify.call_args[0][0]
        assert "❌" in payload
        assert "Default Title" in payload
