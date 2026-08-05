"""
Unit tests for telegram.starter_conf — Firestore configuration loading,
cursor validation/migration, and username→ID lookup construction.

Covers:
  - ``forming_configuration()`` — happy path & all error paths
  - Empty keywords / chats documents
  - Missing or corrupt cursor_base document
  - Cursor validation (suspicious entries: malformed structure, non-integer cursor,
    negative cursor, suspiciously large cursor)
  - Legacy nested-array alerted-set migration
  - known_usernames_to_ids construction
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Patch target: the *source* module so all importers get the mock.
# The conftest's _clear_main_module_cache fixture clears telegram.* from
# sys.modules on every test, so patching telegram.starter_conf.FirestoreMagic
# is unreliable — the fresh import may bind the real class first.
_FIRESTORE_PATCH_TARGET = "gcp_actions.firestore_box.json_manipulations.FirestoreMagic"


# ---------------------------------------------------------------------------
# Helper: build a pair of FirestoreMagic mocks (keywords doc + cursor_base doc)
# ---------------------------------------------------------------------------

def _make_fs_keywords_mock(words=None, chats=None):
    """Return a FirestoreMagic mock for the 'keywords' document."""
    fs = MagicMock()
    fs.load_firejson.return_value = {
        "word": words if words is not None else ["urgent", "alert"],
        "chats": chats if chats is not None else ["@channel_a", "@channel_b"],
    }
    fs.unpack_array_to_csv_string.side_effect = (
        lambda doc, field: ",".join(str(x) for x in doc.get(field, []))
    )
    return fs


def _make_fs_cursor_mock(cursor_data=None):
    """Return a FirestoreMagic mock for the 'cursor_base' document."""
    fs = MagicMock()
    fs.load_firejson.return_value = cursor_data
    return fs


# ============================================================================
# Happy-path tests
# ============================================================================

class TestFormingConfigurationHappyPath:
    """Normal operation: all Firestore documents are present and valid."""

    def test_returns_keywords_chats_and_cursors(self):
        fsk = _make_fs_keywords_mock(
            words=["urgent", "alert", "critical"],
            chats=["@channel_a", "@channel_b", "1122334455"],
        )
        fsc = _make_fs_cursor_mock({
            "111222333": ["@channel_a", 42],
            "444555666": ["@channel_b", 99],
        })

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            kw, chats, cursors, known = forming_configuration()

        assert kw == ["urgent", "alert", "critical"]
        assert chats == ["@channel_a", "@channel_b", "1122334455"]
        assert cursors == {
            "111222333": ["@channel_a", 42],
            "444555666": ["@channel_b", 99],
        }
        assert known == {"@channel_a": "111222333", "@channel_b": "444555666"}

    def test_empty_cursor_base_returns_empty_dicts(self):
        """When cursor_base document is missing, start with empty state."""
        fsk = _make_fs_keywords_mock(words=["kw1"], chats=["@c1"])
        fsc = _make_fs_cursor_mock(None)

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            kw, chats, cursors, known = forming_configuration()

        assert kw == ["kw1"]
        assert cursors == {}
        assert known == {}

    def test_whitespace_is_stripped_from_csv(self):
        """Chats/keywords with extra whitespace around commas are cleaned."""
        fsk = _make_fs_keywords_mock(
            words=["  urgent  ", " alert "],
            chats=[" @c1 ", " @c2 "],
        )
        fsc = _make_fs_cursor_mock({})

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            kw, chats, _cur, _kn = forming_configuration()
        assert kw == ["urgent", "alert"]
        assert chats == ["@c1", "@c2"]


# ============================================================================
# Error paths
# ============================================================================

class TestFormingConfigurationErrors:
    """Missing or corrupt data should raise RuntimeError."""

    def test_empty_keywords_document_raises(self):
        fs = MagicMock()
        fs.load_firejson.return_value = {}
        fs.unpack_array_to_csv_string.return_value = ""

        with patch(_FIRESTORE_PATCH_TARGET, return_value=fs):
            from telegram.starter_conf import forming_configuration
            with pytest.raises(RuntimeError, match="Cannot continue without keywords"):
                forming_configuration()

    def test_none_keywords_document_raises(self):
        fs = MagicMock()
        fs.load_firejson.return_value = None
        fs.unpack_array_to_csv_string.return_value = ""

        with patch(_FIRESTORE_PATCH_TARGET, return_value=fs):
            from telegram.starter_conf import forming_configuration
            with pytest.raises(RuntimeError, match="Cannot continue without keywords"):
                forming_configuration()

    def test_empty_word_field_raises(self):
        """If 'word' field is empty list, RuntimeError is raised."""
        fs_keywords = _make_fs_keywords_mock(words=[], chats=["@c"])

        with patch(_FIRESTORE_PATCH_TARGET, return_value=fs_keywords):
            from telegram.starter_conf import forming_configuration
            with pytest.raises(RuntimeError, match="Keywords list is empty"):
                forming_configuration()

    def test_empty_chats_field_raises(self):
        """If 'chats' field is empty list, RuntimeError is raised."""
        fs_keywords = _make_fs_keywords_mock(words=["kw"], chats=[])

        with patch(_FIRESTORE_PATCH_TARGET, return_value=fs_keywords):
            from telegram.starter_conf import forming_configuration
            with pytest.raises(RuntimeError, match="Target chats list is empty"):
                forming_configuration()

    def test_corrupt_cursor_base_wrong_type_raises(self):
        """cursor_base is not a dict → RuntimeError."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock(["not", "a", "dict"])

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            with pytest.raises(RuntimeError, match="cursor_base document is corrupt"):
                forming_configuration()


# ============================================================================
# Cursor validation (load-time)
# ============================================================================

class TestCursorValidation:
    """Detection of suspicious cursor entries during load."""

    def test_malformed_cursor_structure_logs_warning(self, caplog):
        """Cursor entry with only 1 element (should be >=2) logs warning."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({"123": ["@name"]})  # only 1 element

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            forming_configuration()
        assert "suspicious cursor" in caplog.text.lower()
        assert "malformed structure" in caplog.text.lower()

    def test_non_integer_cursor_logs_warning(self, caplog):
        """Cursor value that is not an int triggers a warning."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({"123": ["@name", "not_a_number"]})

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            forming_configuration()
        assert "non-integer cursor" in caplog.text.lower()

    def test_negative_cursor_logs_warning(self, caplog):
        """Negative cursor values trigger a warning."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({"123": ["@name", -5]})

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            forming_configuration()
        assert "negative cursor" in caplog.text.lower()

    def test_suspiciously_large_cursor_logs_warning(self, caplog):
        """Cursor > 2^31-1 (max Telegram message ID) triggers warning."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({"123": ["@name", 3_000_000_000]})

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            forming_configuration()
        assert "suspiciously large cursor" in caplog.text.lower()

    def test_valid_cursor_does_not_trigger_warnings(self, caplog):
        """A well-formed cursor entry should not produce suspicious-entry logs."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({"123": ["@name", 42]})

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            forming_configuration()
        assert "suspicious cursor" not in caplog.text.lower()


# ============================================================================
# Legacy migration
# ============================================================================

class TestLegacyMigration:
    """Migration of legacy nested-array alerted entries to CSV string format."""

    def test_legacy_list_alerted_migrated_to_csv(self, caplog):
        """A cursor entry with alerted as a list should be migrated to CSV string."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({
            "123": ["@name", 42, ["alert_key_1", "alert_key_2"]],  # legacy format
        })

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            _kw, _ch, cursors, _kn = forming_configuration()

        # The legacy list should now be a CSV string
        assert cursors["123"][2] == "alert_key_1,alert_key_2"
        assert "Migrated" in caplog.text

    def test_no_migration_needed_for_modern_format(self, caplog):
        """Modern CSV-string alerted format should not trigger migration."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({
            "123": ["@name", 42, "alert_key_1,alert_key_2"],  # already CSV
        })

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            _kw, _ch, cursors, _kn = forming_configuration()

        assert cursors["123"][2] == "alert_key_1,alert_key_2"
        assert "Migrated" not in caplog.text  # no migration needed


# ============================================================================
# known_usernames_to_ids construction
# ============================================================================

class TestKnownUsernamesConstruction:
    """Building the username → numeric ID lookup map."""

    def test_builds_lookup_from_cursor_data(self):
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({
            "111": ["@alpha", 1],
            "222": ["@beta", 2],
        })

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            _kw, _ch, _cur, known = forming_configuration()

        assert known == {"@alpha": "111", "@beta": "222"}

    def test_corrupt_entry_triggers_rebuild(self, caplog):
        """If a cursor entry can't be unpacked, the lookup is rebuilt from scratch."""
        fsk = _make_fs_keywords_mock(words=["kw"], chats=["@c"])
        fsc = _make_fs_cursor_mock({
            "111": 12345,  # integer, not a list → TypeError on values[0]
        })

        with patch(_FIRESTORE_PATCH_TARGET, side_effect=[fsk, fsc]):
            from telegram.starter_conf import forming_configuration
            _kw, _ch, cursors, known = forming_configuration()

        # Should have rebuilt from scratch
        assert known == {}
        assert cursors == {}
        assert "Database cursor_base is corrupt" in caplog.text
