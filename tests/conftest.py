"""
Shared pytest fixtures for GCF deployment simulation.

All GCP dependencies (Secret Manager, Firestore, Telethon, Cloud Logging)
are mocked so tests run without network access or real credentials.
"""

from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the *parent* of the project directory is on sys.path so that
# ``import telegram_parcer`` and ``import telegram`` both resolve correctly
# during test collection and execution.
# ---------------------------------------------------------------------------
_PROJECT_DIR = Path(__file__).resolve().parent.parent  # telegram_parcer/
_PARENT_DIR = _PROJECT_DIR.parent  # .../main/
if str(_PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(_PARENT_DIR))
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))


# ---------------------------------------------------------------------------
# Helpers — reusable mock factories
# ---------------------------------------------------------------------------

def _async_return(value):
    """Return a coroutine that resolves to *value*.

    Use as ``side_effect`` so each call gets a fresh coroutine.
    """
    async def _inner():
        return value

    return _inner()


def _make_secret_manager_mock() -> MagicMock:
    """Return a SecretManagerClient mock that returns fake Telegram secrets."""
    sm = MagicMock()
    sm.get_secret_json.return_value = {
        "API_ID": "12345",
        "API_HASH": "abc123hash",
        "session_string": "1AZT_mock_session",
        "BOT_TOKEN": "123:mock_bot_token",
    }
    return sm


def _make_firestore_mock(
    keywords: list[str] | None = None,
    chats: list[str] | None = None,
    cursors: dict[str, list[Any]] | None = None,
) -> MagicMock:
    """Return a FirestoreMagic mock with configurable keywords/chats/cursor data."""
    fs = MagicMock()
    kw = keywords or ["alert", "urgent"]
    ch = chats or ["@test_channel"]

    # load_firejson() is called twice in starter_conf.py:
    #   1st → "keywords" doc (fields: "word", "chats")
    #   2nd → "cursor_base" doc
    fs.load_firejson.side_effect = [
        {"word": kw, "chats": ch},
        cursors or {"123456789": ["@test_channel", 42]},
    ]

    # unpack_array_to_csv_string is used to convert Firestore arrays → CSV
    def _unpack(doc: dict, field: str) -> str:
        arr = doc.get(field, [])
        return ",".join(str(x) for x in arr)

    fs.unpack_array_to_csv_string.side_effect = _unpack
    return fs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def gcp_env() -> dict[str, str]:
    """Inject minimal GCF-style environment variables for every test.

    Cloud Functions sets K_SERVICE and K_REVISION at runtime.
    We mock them here so check_cloud_or_local_run() detects "cloud" mode.
    """
    env_vars = {
        "GCP_PROJECT_ID": "test-project-123",
        "K_SERVICE": "telegram-parcer",
        "K_REVISION": "telegram-parcer-00001",
        "TELEGRAM_SECRETS": "telegram-secrets",
        "NOTIFICATION_CHAT": "-1001234567890",
    }
    with patch.dict(os.environ, env_vars, clear=False):
        yield env_vars


@pytest.fixture
def mock_all_gcp_deps():
    """Mock all external GCP/Telegram dependencies at once.

    This fixture MUST be used by any test that imports from telegram_parcer.main
    because that module performs network calls at import time.
    """
    # Build shared mocks that need to survive across the patched scope
    mock_tg_client = MagicMock()

    # --- Async methods: use side_effect so each call gets a fresh coroutine ---
    mock_tg_client.start.side_effect = lambda: _async_return(None)
    mock_tg_client.disconnect.side_effect = lambda: _async_return(None)

    # get_entity returns a mock with .id and .title
    mock_entity = MagicMock()
    mock_entity.id = 123456789
    mock_entity.title = "Test Channel"
    mock_tg_client.get_entity.side_effect = lambda *a, **kw: _async_return(mock_entity)

    # get_messages returns a list with one mock message
    mock_msg = MagicMock()
    mock_msg.id = 42
    mock_msg.text = "Test message content"
    mock_tg_client.get_messages.side_effect = lambda *a, **kw: _async_return([mock_msg])

    # iter_dialogs returns an empty async iterable
    mock_tg_client.iter_dialogs.return_value = _async_iter([])

    # Mock the TelegramClient class itself (used as async context manager)
    class _MockTelegramClientCtx:
        """An async context manager that yields our mock client."""

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return mock_tg_client

        async def __aexit__(self, *args):
            return None

    mock_tg_class = _MockTelegramClientCtx

    # StringSession mock: return a MagicMock so it doesn't try to decode
    # the fake session string
    mock_string_session = MagicMock()
    mock_string_session.return_value = MagicMock()

    # 1. Cloud Logging — suppress real log setup
    with patch("gcp_actions.common_utils.handle_logs.run_handle_logs", return_value=None):
        # 2. Secret Manager
        with patch(
            "gcp_actions.common_utils.init_config.SecretManagerClient",
            return_value=_make_secret_manager_mock(),
        ):
            # 3. Firestore (FirestoreMagic is imported by starter_conf and listener)
            with patch(
                "gcp_actions.firestore_box.json_manipulations.FirestoreMagic",
                return_value=_make_firestore_mock(),
            ):
                # 4. Telethon — must be patched at the module where they're imported
                with patch(
                    "telegram.listener.TelegramClient", mock_tg_class
                ):
                    with patch(
                        "telegram.listener.StringSession", mock_string_session
                    ):
                        yield {
                            "secret_manager": _make_secret_manager_mock(),
                            "firestore": _make_firestore_mock(),
                            "telegram_client": mock_tg_client,
                        }


async def _async_iter(items: list):
    """Helper: yield items one by one as an async generator."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Auto-cleanup: prevent module caching issues between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_main_module_cache():
    """Remove telegram_parcer.main from sys.modules so each test gets a fresh import.

    Because main.py runs code at import time, cached modules would skip the
    mocked dependencies on subsequent tests.
    """
    # Clean before test
    for mod in list(sys.modules):
        if mod.startswith("telegram_parcer") or mod.startswith("telegram"):
            del sys.modules[mod]
    yield
    # Clean after test
    for mod in list(sys.modules):
        if mod.startswith("telegram_parcer") or mod.startswith("telegram"):
            del sys.modules[mod]
