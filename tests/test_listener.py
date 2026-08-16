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
    _find_matching_keywords,
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
        "has no public username" messages. Also: the typed-dict cursor must
        not crash the registration check (regression: KeyError on [0]).
        """
        client = MagicMock()
        client.start = AsyncMock()
        client.disconnect = AsyncMock()
        client.get_input_entity = AsyncMock(side_effect=lambda entity: entity)

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

        class _ClientCtx:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                return None

        with patch("telegram.listener.TelegramClient", _ClientCtx), \
             patch("telegram.listener.StringSession"), \
             patch("telegram.listener.FirestoreMagic", return_value=fs), \
             patch("telegram.listener.send_health_alert", new=AsyncMock()), \
             patch("telegram.listener.send_bot_notification", new=AsyncMock()), \
             patch("telegram.listener.asyncio.sleep", new=AsyncMock()):
            from telegram.listener import poll_telegram
            asyncio.run(poll_telegram(
                KEYWORDS_LIST=["urgent"],
                TARGET_CHATS_LIST=["1511100059"],
                previous_checked_ids=fs.load_firejson(),
                known_usernames_to_ids={},
            ))

        assert "Username '1511100059' not found" not in caplog.text
        assert "has no public username" not in caplog.text
        assert "Could not resolve or process" not in caplog.text
        assert "Chat resolution failed" not in caplog.text
        # No network username lookup for a numeric ref.
        assert not any(
            call.args == ("1511100059",)
            for call in client.get_entity.call_args_list
        )
        # The poller really polled the chat via the dialog cache.
        client.get_input_entity.assert_awaited()
        # Cursor advanced past the fetched message (99) and was saved.
        assert fs.set_firejson.call_args.args[0]["1511100059"]["last_processed_id"] == 99


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


# ============================================================================
# Helpers for poll_telegram tests
# ============================================================================

def _make_cursor(chat_id, last_processed_id, ref=None):
    """Build a typed cursor entry for a chat."""
    return {
        "ref": ref or str(chat_id),
        "last_processed_id": last_processed_id,
        "alerted_keys": "",
        "schema_version": 1,
    }


def _make_msg(msg_id, text):
    """Build a mock Telethon message with an id and text."""
    from datetime import datetime, timezone

    msg = MagicMock()
    msg.id = msg_id
    msg.text = text
    msg.date = datetime.now(timezone.utc)
    return msg


def _make_dialog(chat_id, title):
    """Build a mock Telethon Dialog resolvable by *chat_id*."""
    dialog = MagicMock()
    dialog.id = chat_id
    dialog.entity = MagicMock()
    dialog.entity.id = chat_id
    dialog.entity.title = title
    return dialog


def _poll_with_mocks(cursor_base, dialogs, messages_by_chat, *,
                     send_alert_side_effect=None, keywords=("urgent",),
                     fs=None):
    """Run poll_telegram against fully mocked Telethon/Firestore deps.

    Returns the (fs, client, send_alert, send_bot_notification,
    send_health_alert) mocks so tests can assert on cursor writes and
    alert/notification traffic.

    NOTE: ``MagicMock.__aenter__`` yields a *child* mock, not the mock
    itself — a bare ``patch("telegram.listener.TelegramClient")`` would
    hand poll_telegram an unconfigured client.  A dedicated context class
    (same approach as conftest's ``mock_all_gcp_deps``) makes the client
    reachable.
    """
    fs = fs or MagicMock()
    fs.load_firejson.return_value = dict(cursor_base)
    fs.backup_document.return_value = None
    fs.prune_old_backups.return_value = None

    client = MagicMock()
    client.start = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_input_entity = AsyncMock(side_effect=lambda entity: entity)

    async def _dialogs():
        for dialog in dialogs:
            yield dialog

    client.iter_dialogs = MagicMock()
    client.iter_dialogs.return_value = _dialogs()

    def _get_messages(entity, **kwargs):
        return messages_by_chat.get(str(getattr(entity, "id", "")), [])

    client.get_messages = AsyncMock(side_effect=_get_messages)

    class _ClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            return None

    send_alert = AsyncMock(side_effect=send_alert_side_effect)
    archive = AsyncMock()
    notif = AsyncMock()
    health = AsyncMock()

    with patch("telegram.listener.TelegramClient", _ClientCtx), \
         patch("telegram.listener.StringSession"), \
         patch("telegram.listener.FirestoreMagic", return_value=fs), \
         patch("telegram.listener.send_alert", new=send_alert), \
         patch("telegram.listener.send_bot_notification", new=notif), \
         patch("telegram.listener.send_health_alert", new=health), \
         patch("telegram.listener.save_matched_message_to_firestore", new=archive), \
         patch("telegram.listener.asyncio.sleep", new=AsyncMock()):
        from telegram.listener import poll_telegram
        asyncio.run(poll_telegram(
            KEYWORDS_LIST=list(keywords),
            TARGET_CHATS_LIST=list(cursor_base),
            previous_checked_ids=dict(cursor_base),
            known_usernames_to_ids={},
        ))
    return fs, client, send_alert, notif, health


# ============================================================================
# _find_matching_keywords — FR-MATCH-3 (substring, case, NFKC)
# ============================================================================

class TestMatching:
    """Keyword matching: substring semantics, case-insensitivity, NFKC."""

    def test_substring_match_within_longer_word(self):
        """Keywords match anywhere in the text — no word boundaries."""
        assert _find_matching_keywords("concatenation error", ["cat"]) == ["cat"]

    def test_case_insensitive(self):
        assert _find_matching_keywords("This is URGENT now", ["urgent"]) == ["urgent"]

    def test_nfkc_fullwidth_normalized(self):
        """Fullwidth Latin (ＵＲＧＥＮＴ) matches the ASCII keyword."""
        assert _find_matching_keywords("警告：ＵＲＧＥＮＴ", ["urgent"]) == ["urgent"]

    def test_nfkc_decomposed_vs_composed(self):
        """Decomposed 'cafe\\u0301' matches composed keyword 'café'."""
        assert _find_matching_keywords("cafe\u0301 ouvert", ["café"]) == ["café"]

    def test_no_match_returns_empty(self):
        assert _find_matching_keywords("nothing relevant here", ["urgent", "alert"]) == []

    def test_returns_only_matching_keywords_in_keyword_order(self):
        assert _find_matching_keywords(
            "alert and urgent", ["zzz", "urgent", "alert"]
        ) == ["urgent", "alert"]

    def test_empty_text_never_matches(self):
        assert _find_matching_keywords("", ["urgent"]) == []


# ============================================================================
# Cursor sanity guards — FR-STATE-5
# ============================================================================

class TestCursorGuards:
    """Guard behaviour: refuse invalid cursor advancement."""

    def test_computed_above_newest_keeps_cursor(self, caplog):
        """Computed cursor > newest message → keep old cursor + notify.

        Simulated via an out-of-order batch: the newest message (id 100)
        fails its alert, leaving last acked at id 200 > newest 100.
        """
        cursor_base = {"111": _make_cursor(111, 42)}

        def _flaky_alert(message, *args, **kwargs):
            if message.id == 100:
                raise RuntimeError("bot api down")
            return None

        fs, client, send_alert, notif, health = _poll_with_mocks(
            cursor_base,
            [_make_dialog(111, "Test Chat")],
            {"111": [_make_msg(100, "urgent"), _make_msg(200, "urgent")]},
            send_alert_side_effect=_flaky_alert,
        )
        assert "CURSOR GUARD" in caplog.text
        assert "Refusing to advance" in caplog.text
        notif.assert_awaited_once()
        assert "CURSOR GUARD TRIGGERED" in notif.await_args.args[0]
        saved = fs.set_firejson.call_args.args[0]["111"]["last_processed_id"]
        assert saved == 42  # old cursor preserved

    def test_computed_below_current_keeps_cursor(self, caplog):
        """Computed cursor < current cursor (cross-contamination hint) → keep old."""
        cursor_base = {"111": _make_cursor(111, 42)}
        # Mock bypasses min_id: the fetched message predates the cursor.
        fs, client, send_alert, notif, health = _poll_with_mocks(
            cursor_base,
            [_make_dialog(111, "Test Chat")],
            {"111": [_make_msg(5, "no keywords here")]},
        )
        assert "CURSOR GUARD" in caplog.text
        assert "LESS than current cursor" in caplog.text
        saved = fs.set_firejson.call_args.args[0]["111"]["last_processed_id"]
        assert saved == 42  # old cursor preserved


# ============================================================================
# Cross-contamination detection — FR-STATE-6
# ============================================================================

class TestCrossContaminationDetection:
    """Multiple chats must never receive the same new cursor value."""

    def test_duplicate_cursor_values_abort_final_save(self, caplog):
        """Two chats acked to the same value → CRITICAL + final save aborted."""
        cursor_base = {
            "111": _make_cursor(111, 42),
            "222": _make_cursor(222, 42),
        }
        # Anomaly: both chats produce a message with the same id.
        fs, client, send_alert, notif, health = _poll_with_mocks(
            cursor_base,
            [_make_dialog(111, "Chat One"), _make_dialog(222, "Chat Two")],
            {"111": [_make_msg(50, "urgent")], "222": [_make_msg(50, "urgent")]},
        )
        assert "CROSS-CONTAMINATION DETECTED" in caplog.text
        assert "Aborting save" in caplog.text
        notif.assert_awaited_once()
        assert "CROSS-CONTAMINATION DETECTED" in notif.await_args.args[0]
        # Only the two per-chat incremental saves happened; the final
        # merged save (and its backup) was aborted.
        assert fs.set_firejson.call_count == 2
        fs.backup_document.assert_not_called()


# ============================================================================
# Backup before save — FR-STATE-7
# ============================================================================

class TestBackupBeforeSave:
    """cursor_base is backed up (with pruning) before every write."""

    def test_backup_and_prune_called_before_save(self):
        cursor_base = {"111": _make_cursor(111, 42)}
        fs, client, send_alert, notif, health = _poll_with_mocks(
            cursor_base,
            [_make_dialog(111, "Test Chat")],
            {"111": [_make_msg(50, "urgent")]},
        )
        fs.backup_document.assert_called_once()
        fs.prune_old_backups.assert_called_once_with(max_backups=30)
        saved = fs.set_firejson.call_args.args[0]["111"]["last_processed_id"]
        assert saved == 50  # normal advance still written

    def test_backup_failure_is_non_fatal(self, caplog):
        """A failing backup must not block the cursor save."""
        cursor_base = {"111": _make_cursor(111, 42)}
        fs = MagicMock()
        fs.backup_document.side_effect = RuntimeError("backup exploded")
        fs, client, send_alert, notif, health = _poll_with_mocks(
            cursor_base,
            [_make_dialog(111, "Test Chat")],
            {"111": [_make_msg(50, "urgent")]},
            fs=fs,
        )
        assert "Backup failed (non-fatal)" in caplog.text
        fs.set_firejson.assert_called()  # save proceeded despite backup failure
        saved = fs.set_firejson.call_args.args[0]["111"]["last_processed_id"]
        assert saved == 50


# ============================================================================
# Alert-failure cursor stall — FR-STATE-2 (isolated)
# ============================================================================

class TestAlertFailureCursorStall:
    """A failed alert must stall the cursor and stop processing the batch."""

    def test_alert_failure_keeps_cursor_and_breaks_batch(self, caplog):
        cursor_base = {"111": _make_cursor(111, 42)}
        fs, client, send_alert, notif, health = _poll_with_mocks(
            cursor_base,
            [_make_dialog(111, "Test Chat")],
            {"111": [_make_msg(51, "urgent"), _make_msg(50, "urgent")]},
            send_alert_side_effect=RuntimeError("bot api down"),
        )
        # Only the first message (id 50) was attempted — the loop broke
        # instead of advancing past the failed alert.
        send_alert.assert_awaited_once()
        assert send_alert.await_args.args[0].id == 50
        assert "will retry on next run" in caplog.text
        saved = fs.set_firejson.call_args.args[0]["111"]["last_processed_id"]
        assert saved == 42  # cursor NOT advanced past the failed message
