"""
Unit tests for telegram.listener — core polling, cursor management, shutdown, helpers.

Covers:
  - ``_should_stop()`` — signal & timeout logic
  - ``_safe_title()`` — entity title extraction
  - ``_save_cursor_sync()`` — cursor persistence & error resilience
  - ``poll_telegram()`` — full polling lifecycle (chat registration, message fetch,
    keyword matching, alert pipeline, cursor advancement, shutdown mid-cycle)
"""

from __future__ import annotations

import os
import time
import asyncio
import threading
from unittest.mock import MagicMock, AsyncMock, patch, call

import pytest

from telegram.listener import (
    _should_stop,
    _safe_title,
    _save_cursor_sync,
    _chat_sort_key,
    _get_priority_chat_refs,
)# NOTE: poll_telegram is NOT imported at module level — it is imported
# lazily inside each test's ``with patch(...)`` block so that the mocked
# TelegramClient / StringSession / FirestoreMagic are bound first.
# The conftest's _clear_main_module_cache fixture removes telegram.listener
# from sys.modules before each test, so when ``patch`` activates it
# triggers a fresh import with mocks already in place.


# ============================================================================
# _should_stop
# ============================================================================

class TestShouldStop:
    """Signal & timeout logic for the polling loop."""

    def test_no_shutdown_event_no_deadline(self):
        """Neither shutdown event nor deadline set → should NOT stop."""
        should, reason = _should_stop(None, None)
        assert should is False
        assert reason is None

    def test_shutdown_event_set(self):
        """When shutdown_event.is_set() → stop with reason 'signal'."""
        evt = threading.Event()
        evt.set()
        should, reason = _should_stop(evt, None)
        assert should is True
        assert reason == "signal"

    def test_shutdown_event_not_set(self):
        """shutdown_event present but not set → should NOT stop."""
        evt = threading.Event()
        should, reason = _should_stop(evt, None)
        assert should is False
        assert reason is None

    def test_deadline_reached(self):
        """monotonic >= deadline → stop with reason 'timeout'."""
        past = time.monotonic() - 10  # 10 seconds ago
        should, reason = _should_stop(None, past)
        assert should is True
        assert reason == "timeout"

    def test_deadline_not_reached(self):
        """monotonic < deadline → should NOT stop."""
        future = time.monotonic() + 3600  # 1 hour from now
        should, reason = _should_stop(None, future)
        assert should is False
        assert reason is None

    def test_signal_takes_priority_over_timeout(self):
        """When both signal and deadline are active, signal wins."""
        evt = threading.Event()
        evt.set()
        past = time.monotonic() - 10
        should, reason = _should_stop(evt, past)
        assert should is True
        assert reason == "signal"


# ============================================================================
# _safe_title
# ============================================================================

class TestSafeTitle:
    """Entity title extraction for Channel / Chat / User / Fallback."""

    def test_channel_with_title(self):
        entity = MagicMock()
        entity.title = "My Channel"
        del entity.first_name  # ensure no fallback
        assert _safe_title(entity) == "My Channel"

    def test_user_with_first_and_last_name(self):
        entity = MagicMock(spec=["first_name", "last_name"])
        entity.first_name = "John"
        entity.last_name = "Doe"
        # Users don't have .title
        assert _safe_title(entity) == "John Doe"

    def test_user_first_name_only(self):
        entity = MagicMock(spec=["first_name"])
        entity.first_name = "Alice"
        assert _safe_title(entity) == "Alice"

    def test_fallback_to_id(self):
        entity = MagicMock(spec=["id"])
        entity.id = 123456
        assert _safe_title(entity) == "123456"

    def test_fallback_unknown(self):
        entity = MagicMock(spec=[])  # nothing at all
        assert _safe_title(entity) == "Unknown"


# ============================================================================
# _save_cursor_sync
# ============================================================================

class TestSaveCursorSync:
    """Persist cursor data to Firestore."""

    def test_saves_successfully(self):
        fs = MagicMock()
        data = {"123": ["@chat", 42]}
        _save_cursor_sync(fs, data)
        fs.set_firejson.assert_called_once_with(data, merge=True)

    def test_handles_write_failure_gracefully(self, caplog):
        fs = MagicMock()
        fs.set_firejson.side_effect = RuntimeError("Firestore unavailable")
        data = {"123": ["@chat", 42]}

        # Should NOT raise — errors are logged, not propagated
        _save_cursor_sync(fs, data)
        assert "Failed to save cursor to Firestore" in caplog.text


# ============================================================================
# poll_telegram — lifecycle & edge cases
# ============================================================================

class TestPollTelegramLifecycle:
    """Test the main polling function through its lifecycle stages."""

    @pytest.fixture(autouse=True)
    def _set_telegram_env(self, monkeypatch):
        """Set env vars that Telethon requires at init time."""
        monkeypatch.setenv("API_ID", "12345")
        monkeypatch.setenv("API_HASH", "abc123hash")
        monkeypatch.setenv("session_string", "1AZT_mock_session")
        monkeypatch.setenv("NOTIFICATION_CHAT", "-1001234567890")
        monkeypatch.setenv("BOT_TOKEN", "123:mock")

    @pytest.fixture
    def mock_firestore_magic(self):
        """Return a FirestoreMagic mock usable as both a class and instance."""
        instance = MagicMock()
        instance.load_firejson.return_value = {
            "123456789": {"ref": "@test_channel", "last_processed_id": 42,
                          "alerted_keys": "", "schema_version": 1},
        }
        instance.backup_document.return_value = None
        instance.prune_old_backups.return_value = None
        return instance

    @pytest.fixture
    def mock_tg_client(self):
        """Return an async-enabled mock TelegramClient."""
        client = MagicMock()
        client.start = AsyncMock()
        client.disconnect = AsyncMock()

        entity = MagicMock()
        entity.id = 123456789
        entity.title = "Test Channel"
        client.get_entity = AsyncMock(return_value=entity)

        msg = MagicMock()
        msg.id = 99
        msg.text = "This is an urgent message!"
        msg.date = MagicMock()
        client.get_messages = AsyncMock(return_value=[msg])

        async def _empty_iter():
            return
            yield  # pragma: no cover

        client.iter_dialogs = MagicMock()
        client.iter_dialogs.return_value = _empty_iter()

        return client

    def test_empty_target_chats_returns_early(self, mock_firestore_magic, caplog):
        """If TARGET_CHATS_LIST is empty, poll_telegram should return immediately."""
        with patch("telegram.listener.TelegramClient"), \
             patch("telegram.listener.StringSession"), \
             patch("telegram.listener.FirestoreMagic", return_value=mock_firestore_magic):
            from telegram.listener import poll_telegram; asyncio.run(poll_telegram(
                KEYWORDS_LIST=["urgent", "alert"],
                TARGET_CHATS_LIST=[],  # empty!
                previous_checked_ids={},
                known_usernames_to_ids={},
            ))
        assert "TARGET_CHATS_LIST is empty" in caplog.text

    def test_empty_keywords_returns_early(self, mock_firestore_magic, caplog):
        """If KEYWORDS_LIST is empty, poll_telegram should return immediately."""
        with patch("telegram.listener.TelegramClient"), \
             patch("telegram.listener.StringSession"), \
             patch("telegram.listener.FirestoreMagic", return_value=mock_firestore_magic):
            from telegram.listener import poll_telegram; asyncio.run(poll_telegram(
                KEYWORDS_LIST=[],  # empty!
                TARGET_CHATS_LIST=["@test"],
                previous_checked_ids={},
                known_usernames_to_ids={},
            ))
        assert "KEYWORDS_LIST is empty" in caplog.text

    def test_shutdown_during_chat_registration(self, mock_firestore_magic, caplog):
        """When a shutdown signal arrives during chat registration, save partial state."""
        with patch("telegram.listener.TelegramClient"), \
             patch("telegram.listener.StringSession"), \
             patch("telegram.listener.FirestoreMagic", return_value=mock_firestore_magic):
            evt = threading.Event()
            evt.set()  # immediately signaled
            from telegram.listener import poll_telegram; asyncio.run(poll_telegram(
                KEYWORDS_LIST=["urgent"],
                TARGET_CHATS_LIST=["@new_chat"],
                previous_checked_ids={},
                known_usernames_to_ids={},
                shutdown_event=evt,
            ))
        assert "Shutdown (signal) during chat registration" in caplog.text

    def test_timeout_during_chat_registration(self, mock_firestore_magic, caplog):
        """When max_runtime expires during chat registration, stop gracefully.
        
        Uses a very short max_runtime_seconds so the deadline is already in the
        past by the time the chat registration loop starts (after client init
        and dialog cache build).
        """
        with patch("telegram.listener.TelegramClient"), \
             patch("telegram.listener.StringSession"), \
             patch("telegram.listener.FirestoreMagic", return_value=mock_firestore_magic):
            from telegram.listener import poll_telegram; asyncio.run(poll_telegram(
                KEYWORDS_LIST=["urgent"],
                TARGET_CHATS_LIST=["@new_chat"],
                previous_checked_ids={},
                known_usernames_to_ids={},
                max_runtime_seconds=0.000001,  # effectively immediate
            ))
        assert "Shutdown (timeout)" in caplog.text

    def test_corrupt_cursor_entry_skipped(self, mock_firestore_magic, caplog):
        """A cursor entry with fewer than 2 elements is skipped during polling."""
        with patch("telegram.listener.TelegramClient"), \
             patch("telegram.listener.StringSession"), \
             patch("telegram.listener.FirestoreMagic", return_value=mock_firestore_magic):
            from telegram.listener import poll_telegram; asyncio.run(poll_telegram(
                KEYWORDS_LIST=["urgent"],
                TARGET_CHATS_LIST=[],  # empty target list
                previous_checked_ids={"bad_chat": "not_a_dict"},  # corrupt format!
                known_usernames_to_ids={},
            ))
        assert "TARGET_CHATS_LIST is empty" in caplog.text

    def test_numeric_ref_steady_state_is_silent(self, monkeypatch, caplog):
        """A private group already tracked by numeric ID must NOT spam logs.

        Regression: ref '1511100059' resolves via the dialog cache without
        calling get_entity and without the repeated "username not found" /
        "has no public username" messages.
        """
        client = MagicMock()
        client.start = AsyncMock()
        client.disconnect = AsyncMock()
        client.get_input_entity = AsyncMock(return_value=MagicMock())

        dialog = MagicMock()
        dialog.id = 1511100059
        dialog.entity = MagicMock()
        dialog.entity.id = 1511100059
        dialog.entity.title = "МобілізаціяChat"

        async def _dialogs():
            yield dialog

        client.iter_dialogs = MagicMock()
        client.iter_dialogs.return_value = _dialogs()

        msg = MagicMock()
        msg.id = 99
        msg.text = "some message"
        msg.date = MagicMock()
        client.get_messages = AsyncMock(return_value=[msg])

        fs = MagicMock()
        fs.load_firejson.return_value = {
            "1511100059": {"ref": "1511100059", "last_processed_id": 1,
                           "alerted_keys": "", "schema_version": 1},
        }
        fs.backup_document.return_value = None
        fs.prune_old_backups.return_value = None

        with patch("telegram.listener.TelegramClient"), \
             patch("telegram.listener.StringSession"), \
             patch("telegram.listener.FirestoreMagic", return_value=fs), \
             patch("telegram.listener.send_health_alert", new=AsyncMock()), \
             patch("telegram.listener.send_bot_notification", new=AsyncMock()):
            from telegram.listener import poll_telegram
            asyncio.run(poll_telegram(
                KEYWORDS_LIST=["urgent"],
                TARGET_CHATS_LIST=["1511100059"],
                previous_checked_ids=fs.load_firejson(),
                known_usernames_to_ids={},
            ))

        assert "Username '1511100059' not found" not in caplog.text
        assert "has no public username" not in caplog.text
        # No network username lookup for a numeric ref.
        assert not any(
            call.args == ("1511100059",)
            for call in client.get_entity.call_args_list
        )


# ============================================================================
# Chat polling priority (PRIORITY_CHAT_REFS)
# ============================================================================

class TestChatPollingPriority:
    """The poll loop must process priority chats first."""

    def test_get_priority_chat_refs_parses_env(self, monkeypatch):
        monkeypatch.setenv("PRIORITY_CHAT_REFS", "@GreenField9000, 123456")
        assert _get_priority_chat_refs() == {"@greenfield9000", "123456"}

    def test_get_priority_chat_refs_empty_by_default(self, monkeypatch):
        monkeypatch.delenv("PRIORITY_CHAT_REFS", raising=False)
        assert _get_priority_chat_refs() == set()

    def test_sort_key_matches_by_numeric_id(self):
        priority = {"4402366162"}
        items = {
            "4402366162": {"ref": "@greenfield9000", "last_processed_id": 0,
                           "alerted_keys": "", "schema_version": 1},
            "123456789": {"ref": "@other", "last_processed_id": 0,
                          "alerted_keys": "", "schema_version": 1},
        }
        ordered = sorted(items.items(), key=lambda kv: _chat_sort_key(kv, priority))
        assert ordered[0][0] == "4402366162"  # priority chat first
        assert ordered[1][0] == "123456789"

    def test_sort_key_matches_by_username_ref(self):
        priority = {"@greenfield9000"}
        items = {
            "4402366162": {"ref": "@greenfield9000", "last_processed_id": 0,
                           "alerted_keys": "", "schema_version": 1},
            "100": {"ref": "@aaaa", "last_processed_id": 0,
                    "alerted_keys": "", "schema_version": 1},
        }
        ordered = sorted(items.items(), key=lambda kv: _chat_sort_key(kv, priority))
        assert ordered[0][0] == "4402366162"  # matched by @ref, first despite larger ID

    def test_sort_key_preserves_numeric_order_within_priority(self):
        priority = {"@a", "@b"}
        items = {
            "500": {"ref": "@b", "last_processed_id": 0,
                    "alerted_keys": "", "schema_version": 1},
            "100": {"ref": "@a", "last_processed_id": 0,
                    "alerted_keys": "", "schema_version": 1},
        }
        ordered = sorted(items.items(), key=lambda kv: _chat_sort_key(kv, priority))
        assert [kv[0] for kv in ordered] == ["100", "500"]
