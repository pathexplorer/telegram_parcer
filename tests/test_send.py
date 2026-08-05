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
        return _FakeResponse(self._resp_status, self._resp_text)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _FakeResponse:
    """Async context manager for aiohttp.ClientResponse."""

    def __init__(self, status=200, text="ok"):
        self.status = status
        self._text = text

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
        assert "test_channel" in payload_text

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
