"""Tests for the end-to-end smoke test helper (scripts.e2e_test)."""

from __future__ import annotations

import asyncio
import re
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scripts.e2e_test as et


# ---------------------------------------------------------------------------
# Keyword config guard
# ---------------------------------------------------------------------------

def test_ensure_keyword_configured_passes(monkeypatch):
    fs = MagicMock()
    fs.load_firejson.return_value = {"word": ["specialtestphrase", "urgent"]}
    monkeypatch.setattr(et, "FirestoreMagic", lambda *a, **k: fs)
    et._ensure_keyword_configured("specialtestphrase")  # must not raise


def test_ensure_keyword_configured_missing_raises(monkeypatch):
    fs = MagicMock()
    fs.load_firejson.return_value = {"word": ["urgent"]}
    monkeypatch.setattr(et, "FirestoreMagic", lambda *a, **k: fs)
    with pytest.raises(SystemExit):
        et._ensure_keyword_configured("specialtestphrase")


# ---------------------------------------------------------------------------
# Marker / message text
# ---------------------------------------------------------------------------

def test_marker_has_no_markdown_metacharacters():
    """The embedded marker must survive Markdown escaping in send.py."""
    marker = "E2E20260814T081530Z"
    assert re.fullmatch(r"[A-Za-z0-9]+", marker)


# ---------------------------------------------------------------------------
# Archive verification
# ---------------------------------------------------------------------------

def test_archive_verified_true(monkeypatch):
    fs = MagicMock()
    fs.load_firejson.return_value = {"message": "This my specialtestphrase E2E20260814T081530Z"}
    monkeypatch.setattr(et, "FirestoreMagic", lambda *a, **k: fs)
    assert et._archive_verified("4402366162", 4, "E2E20260814T081530Z") is True


def test_archive_verified_false(monkeypatch):
    fs = MagicMock()
    fs.load_firejson.return_value = None
    monkeypatch.setattr(et, "FirestoreMagic", lambda *a, **k: fs)
    assert et._archive_verified("4402366162", 4, "E2E20260814T081530Z") is False


# ---------------------------------------------------------------------------
# Alert detection in the notification chat
# ---------------------------------------------------------------------------

def test_find_alert_positive():
    msg = MagicMock()
    msg.id = 5
    msg.text = "🚨 **KEYWORD ALERT!** 🚨\n**Keywords:** specialtestphrase\n**Message:** This my specialtestphrase E2E20260814T081530Z"
    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[msg])

    result = asyncio.run(et._find_alert(client, "E2E20260814T081530Z"))
    assert result is True


def test_find_alert_passes_int_chat_id(monkeypatch):
    """The notification chat must be passed as an int, not a digit string."""
    monkeypatch.setenv("NOTIFICATION_CHAT", "-5074504391")
    msg = MagicMock()
    msg.id = 5
    msg.text = "🚨 **KEYWORD ALERT!** 🚨\n**Message:** This my specialtestphrase E2E20260814T081530Z"
    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[msg])

    assert asyncio.run(et._find_alert(client, "E2E20260814T081530Z")) is True
    assert client.get_messages.await_args.args[0] == -5074504391


def test_find_alert_negative():
    msg = MagicMock()
    msg.id = 6
    msg.text = "just a regular message"
    client = MagicMock()
    client.get_messages = AsyncMock(return_value=[msg])

    result = asyncio.run(et._find_alert(client, "E2E20260814T081530Z"))
    assert result is False


# ---------------------------------------------------------------------------
# Trigger command construction
# ---------------------------------------------------------------------------

def test_run_trigger_uses_scheduler(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)

    et._run_trigger("scheduler")
    cmd = popen.call_args[0][0]
    assert cmd[0] == "gcloud"
    assert cmd[2] == "jobs"
    assert cmd[3] == "run"
    assert "telegram-poll-job" in cmd
    assert "--project=test-project-123" in cmd


def test_run_trigger_uses_function(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)

    et._run_trigger("function")
    cmd = popen.call_args[0][0]
    assert cmd[0] == "gcloud"
    assert cmd[2] == "call"
    assert "telegramPoller" in cmd


def test_run_trigger_missing_project_raises(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    with pytest.raises(SystemExit):
        et._run_trigger("scheduler")
