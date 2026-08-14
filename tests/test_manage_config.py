"""Tests for the user-friendly Firestore config manager (scripts.manage_config)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

import scripts.manage_config as mc


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("greenfield9000", "@greenfield9000"),
    ("  @greenfield9000  ", "@greenfield9000"),
    ("@greenfield9000", "@greenfield9000"),
    ("", ""),
])
def test_normalize_chat(raw, expected):
    assert mc._normalize_chat(raw) == expected


def test_split_phrases_comma_and_space():
    assert mc._split_phrases("a, b  c") == ["a", "b", "c"]


def test_dedupe_case_insensitive_preserves_order():
    assert mc._dedupe(["Aa", "b", "aa", "B"]) == ["Aa", "b"]


def test_field_list_array_vs_legacy_string():
    assert mc._field_list({"chats": ["@a", "@b"]}, "chats") == ["@a", "@b"]
    assert mc._field_list({"chats": "@a, @b"}, "chats") == ["@a", "@b"]
    assert mc._field_list({"chats": "garbage"}, "word") == []


# ---------------------------------------------------------------------------
# add_chats
# ---------------------------------------------------------------------------

def test_add_chats_skips_existing(monkeypatch, capsys):
    doc = MagicMock()
    doc.load_firejson.return_value = {"word": ["kw"], "chats": ["@already"]}
    monkeypatch.setattr(mc, "KEYWORDS_DOC", doc)
    monkeypatch.setattr(mc, "CURSOR_DOC", MagicMock())

    rc = mc.add_chats(["@already"], verify=False, dry_run=False, auto_yes=True)
    assert rc == 0
    assert "already in the config" in capsys.readouterr().out
    doc.set_firejson.assert_not_called()


def test_add_chats_writes_and_provisions(monkeypatch):
    kw_doc = MagicMock()
    kw_doc.load_firejson.return_value = {"word": ["kw"], "chats": ["@existing"]}
    cur_doc = MagicMock()
    cur_doc.load_firejson.return_value = {}
    monkeypatch.setattr(mc, "KEYWORDS_DOC", kw_doc)
    monkeypatch.setattr(mc, "CURSOR_DOC", cur_doc)

    entry = {"ref": "@newchat", "last_processed_id": 7,
             "alerted_keys": "", "schema_version": 1}
    monkeypatch.setattr(mc, "_resolve_chat",
                        lambda ref, verify, from_start=False: entry)

    rc = mc.add_chats(["newchat"], verify=True, dry_run=False, auto_yes=True)
    assert rc == 0
    kw_doc.set_firejson.assert_called_once_with(
        {"chats": ["@existing", "@newchat"]}, merge=True)
    cur_doc.set_firejson.assert_called_once_with(
        {"@newchat": entry}, merge=True)


def test_add_chats_rejects_chat_that_resolves_to_user(monkeypatch, capsys):
    kw_doc = MagicMock()
    kw_doc.load_firejson.return_value = {"word": [], "chats": []}
    cur_doc = MagicMock()
    monkeypatch.setattr(mc, "KEYWORDS_DOC", kw_doc)
    monkeypatch.setattr(mc, "CURSOR_DOC", cur_doc)

    def _bad(ref, verify, from_start=False):
        raise ValueError(f"'{ref}' resolved to a User")

    monkeypatch.setattr(mc, "_resolve_chat", _bad)

    rc = mc.add_chats(["@private_user"], verify=True, dry_run=False, auto_yes=True)
    assert rc == 1
    kw_doc.set_firejson.assert_not_called()
    assert "NOT added" in capsys.readouterr().out


def test_add_chats_dry_run_writes_nothing(monkeypatch):
    kw_doc = MagicMock()
    kw_doc.load_firejson.return_value = {"word": [], "chats": ["@a"]}
    monkeypatch.setattr(mc, "KEYWORDS_DOC", kw_doc)
    monkeypatch.setattr(mc, "CURSOR_DOC", MagicMock())

    rc = mc.add_chats(["@b"], verify=False, dry_run=True, auto_yes=True)
    assert rc == 0
    kw_doc.set_firejson.assert_not_called()


# ---------------------------------------------------------------------------
# add_keywords
# ---------------------------------------------------------------------------

def test_add_keywords_dedupes_and_writes(monkeypatch):
    kw_doc = MagicMock()
    kw_doc.load_firejson.return_value = {"word": ["existing"], "chats": ["@a"]}
    monkeypatch.setattr(mc, "KEYWORDS_DOC", kw_doc)

    rc = mc.add_keywords(["new1, new2", "NEW1"], dry_run=False, auto_yes=True)
    assert rc == 0
    kw_doc.set_firejson.assert_called_once_with(
        {"word": ["existing", "new1", "new2"]}, merge=True)


def test_add_keywords_all_existing(monkeypatch, capsys):
    kw_doc = MagicMock()
    kw_doc.load_firejson.return_value = {"word": ["kw"], "chats": []}
    monkeypatch.setattr(mc, "KEYWORDS_DOC", kw_doc)

    rc = mc.add_keywords(["KW"], dry_run=False, auto_yes=True)
    assert rc == 0
    kw_doc.set_firejson.assert_not_called()
    assert "already in the config" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# remove_chats / remove_keywords
# ---------------------------------------------------------------------------

def test_remove_chats_deletes_cursor_entries(monkeypatch):
    kw_doc = MagicMock()
    kw_doc.load_firejson.return_value = {"word": [], "chats": ["@old", "@keep"]}
    cur_doc = MagicMock()
    cur_doc.load_firejson.return_value = {
        "111": {"ref": "@old", "last_processed_id": 3,
                "alerted_keys": "", "schema_version": 1},
        "222": {"ref": "@keep", "last_processed_id": 5,
                "alerted_keys": "", "schema_version": 1},
    }
    monkeypatch.setattr(mc, "KEYWORDS_DOC", kw_doc)
    monkeypatch.setattr(mc, "CURSOR_DOC", cur_doc)

    rc = mc.remove_chats(["@old"], dry_run=False, auto_yes=True)
    assert rc == 0
    kw_doc.set_firejson.assert_called_once_with({"chats": ["@keep"]}, merge=True)
    cur_doc.delete_field_firejson.assert_called_once_with("111")


def test_remove_keywords(monkeypatch):
    kw_doc = MagicMock()
    kw_doc.load_firejson.return_value = {"word": ["a", "b"], "chats": []}
    monkeypatch.setattr(mc, "KEYWORDS_DOC", kw_doc)

    rc = mc.remove_keywords(["b"], dry_run=False, auto_yes=True)
    assert rc == 0
    kw_doc.set_firejson.assert_called_once_with({"word": ["a"]}, merge=True)


# ---------------------------------------------------------------------------
# reset_chat
# ---------------------------------------------------------------------------

def test_reset_chat_by_username(monkeypatch, capsys):
    cur_doc = MagicMock()
    cur_doc.load_firejson.return_value = {
        "4402366162": {"ref": "@greenfield9000", "last_processed_id": 3,
                       "alerted_keys": "", "schema_version": 1},
    }
    monkeypatch.setattr(mc, "CURSOR_DOC", cur_doc)

    rc = mc.reset_chat("@greenfield9000", 0, dry_run=False, auto_yes=True)
    assert rc == 0
    cur_doc.set_firejson.assert_called_once_with({
        "4402366162": {"ref": "@greenfield9000", "last_processed_id": 0,
                       "alerted_keys": "", "schema_version": 1},
    }, merge=True)
    assert "cursor 3 → 0" in capsys.readouterr().out


def test_reset_chat_unknown_ref(monkeypatch, capsys):
    cur_doc = MagicMock()
    cur_doc.load_firejson.return_value = {}
    monkeypatch.setattr(mc, "CURSOR_DOC", cur_doc)

    rc = mc.reset_chat("@nowhere", 0, dry_run=False, auto_yes=True)
    assert rc == 1
    cur_doc.set_firejson.assert_not_called()


def test_reset_chat_dry_run_writes_nothing(monkeypatch):
    cur_doc = MagicMock()
    cur_doc.load_firejson.return_value = {
        "1": {"ref": "@a", "last_processed_id": 5,
              "alerted_keys": "", "schema_version": 1},
    }
    monkeypatch.setattr(mc, "CURSOR_DOC", cur_doc)

    rc = mc.reset_chat("@a", 0, dry_run=True, auto_yes=True)
    assert rc == 0
    cur_doc.set_firejson.assert_not_called()


def test_verify_and_provision_from_start_sets_cursor_zero(monkeypatch):
    """_verify_and_provision_chat must start from 0 when from_start=True."""
    import scripts.manage_config as m
    fake = {"ref": "@x", "last_processed_id": 0,
            "alerted_keys": "", "schema_version": 1}
    calls = {}

    async def _fake_verify(ref, from_start=False):
        calls["from_start"] = from_start
        return fake

    monkeypatch.setattr(m, "_verify_and_provision_chat", _fake_verify)
    monkeypatch.setattr(m, "_secrets_available", lambda: True)
    m._resolve_chat("@x", verify=True, from_start=True)
    assert calls["from_start"] is True


# ---------------------------------------------------------------------------
# list_config
# ---------------------------------------------------------------------------

def test_list_config_flags_unregistered_chats(monkeypatch, capsys):
    kw_doc = MagicMock()
    kw_doc.load_firejson.return_value = {"word": ["kw"], "chats": ["@reg", "@new"]}
    cur_doc = MagicMock()
    cur_doc.load_firejson.return_value = {
        "1": {"ref": "@reg", "last_processed_id": 1,
              "alerted_keys": "", "schema_version": 1},
    }
    monkeypatch.setattr(mc, "KEYWORDS_DOC", kw_doc)
    monkeypatch.setattr(mc, "CURSOR_DOC", cur_doc)

    rc = mc.list_config()
    assert rc == 0
    out = capsys.readouterr().out
    assert "@reg" in out and "@new" in out
    assert "Not yet registered in cursor_base" in out and "@new" in out
