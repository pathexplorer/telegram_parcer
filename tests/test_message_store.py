"""
Unit tests for telegram.message_store — message serialization, Firestore persistence,
truncation behaviour, and helper functions.

Covers:
  - ``_strip_nulls()`` — recursive None removal
  - ``_extract_tl_value()`` — type-aware conversion (int, str, bytes, list, dict, datetime, TLObject)
  - ``_tlobject_to_dict()`` — Telethon TLObject serialization
  - ``_estimate_size()`` — byte-size estimation for truncation guard
  - ``_serialize_message()`` — full message → dict pipeline
  - ``save_matched_message_to_firestore()`` — end-to-end persistence
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch, ANY

import pytest

from telegram.message_store import (
    _strip_nulls,
    _extract_tl_value,
    _tlobject_to_dict,
    _estimate_size,
    _serialize_message,
    _MAX_DOC_SIZE_BYTES,
    save_matched_message_to_firestore,
)


# ============================================================================
# _strip_nulls
# ============================================================================

class TestStripNulls:
    """Recursive removal of None values from nested structures."""

    def test_removes_null_values_from_flat_dict(self):
        result = _strip_nulls({"a": 1, "b": None, "c": "hello"})
        assert result == {"a": 1, "c": "hello"}

    def test_removes_null_values_recursively(self):
        result = _strip_nulls({
            "a": {"inner": None, "keep": 2},
            "b": None,
        })
        assert result == {"a": {"keep": 2}}

    def test_entirely_null_dict_becomes_none(self):
        result = _strip_nulls({"a": None, "b": None})
        assert result is None

    def test_empty_list_after_stripping_becomes_none(self):
        result = _strip_nulls([None, None])
        assert result is None

    def test_list_with_mixed_values(self):
        result = _strip_nulls([1, None, "text", None])
        assert result == [1, "text"]

    def test_nested_list_in_dict(self):
        result = _strip_nulls({"items": [1, None, 3], "meta": None})
        assert result == {"items": [1, 3]}

    def test_scalar_values_pass_through(self):
        assert _strip_nulls(42) == 42
        assert _strip_nulls("hello") == "hello"
        assert _strip_nulls(True) is True
        assert _strip_nulls(None) is None


# ============================================================================
# _extract_tl_value
# ============================================================================

class TestExtractTlValue:
    """Type-aware Telethon value → JSON-safe value conversion."""

    def test_none(self):
        assert _extract_tl_value(None) is None

    def test_bool(self):
        assert _extract_tl_value(True) is True
        assert _extract_tl_value(False) is False

    def test_int(self):
        assert _extract_tl_value(42) == 42

    def test_float(self):
        assert _extract_tl_value(3.14) == 3.14

    def test_str(self):
        assert _extract_tl_value("hello") == "hello"

    def test_bytes(self):
        raw = b"\x00\xFFhello"
        result = _extract_tl_value(raw)
        assert result["_bytes_b64"] == base64.b64encode(raw).decode("ascii")

    def test_list(self):
        result = _extract_tl_value([1, "a", None])
        assert result == [1, "a", None]

    def test_tuple(self):
        result = _extract_tl_value((1, 2))
        assert result == [1, 2]

    def test_set(self):
        result = _extract_tl_value({1, 2})
        assert sorted(result) == [1, 2]

    def test_dict_with_non_string_keys(self):
        result = _extract_tl_value({1: "one", 2: "two"})
        assert result == {"1": "one", "2": "two"}

    def test_datetime_to_isoformat(self):
        dt = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        result = _extract_tl_value(dt)
        assert result == "2025-06-15T14:30:00+00:00"

    def test_unknown_object_falls_back_to_str(self):
        class Weird:
            def __str__(self):
                return "weird-object"

        result = _extract_tl_value(Weird())
        assert result == "weird-object"


# ============================================================================
# _tlobject_to_dict — module-level mock TL classes (defined at module scope
# to avoid inspect.signature issues with function-local classes).
# ============================================================================

class _FakeTLSimple:
    CONSTRUCTOR_ID = 0xDEADBEEF

    def __init__(self, field_a, field_b):
        self.field_a = field_a
        self.field_b = field_b


class _FakeTLInner:
    CONSTRUCTOR_ID = 1

    def __init__(self, value):
        self.value = value


class _FakeTLOuter:
    CONSTRUCTOR_ID = 2

    def __init__(self, inner):
        self.inner = inner


class _FakeTLPartial:
    CONSTRUCTOR_ID = 1

    def __init__(self, a, b):
        self.a = a
        # 'b' is deliberately not set on the instance


class _FakeTLNoConstructorId:
    """An object WITHOUT CONSTRUCTOR_ID — should NOT be treated as TLObject."""

    def __init__(self, x):
        self.x = x


class TestTLObjectToDict:
    """Serialization of Telethon TL objects via constructor inspection."""

    def test_simple_tlobject(self):
        """A minimal TLObject with known fields."""
        obj = _FakeTLSimple("alpha", 42)
        result = _tlobject_to_dict(obj)
        assert result["field_a"] == "alpha"
        assert result["field_b"] == 42
        assert result["_tl_type"] == "_FakeTLSimple"

    def test_nested_tlobject(self):
        obj = _FakeTLOuter(_FakeTLInner(99))
        result = _tlobject_to_dict(obj)
        assert result["inner"]["value"] == 99
        assert result["inner"]["_tl_type"] == "_FakeTLInner"
        assert result["_tl_type"] == "_FakeTLOuter"

    def test_tlobject_with_missing_attribute(self):
        obj = _FakeTLPartial("yes", "missing")
        result = _tlobject_to_dict(obj)
        assert result["a"] == "yes"
        assert result["b"] is None  # getattr fails → None

    def test_no_constructor_id_not_treated_as_tlobject(self):
        """An object without CONSTRUCTOR_ID should fall through to str()."""
        obj = _FakeTLNoConstructorId(42)
        # Without CONSTRUCTOR_ID, _extract_tl_value won't deep-serialize it
        from telegram.message_store import _extract_tl_value
        result = _extract_tl_value(obj)
        # It should be treated as a plain object → str()
        assert isinstance(result, str)


# ============================================================================
# _estimate_size
# ============================================================================

class TestEstimateSize:
    """Byte-size estimation for truncation guard."""

    def test_small_dict(self):
        size = _estimate_size({"key": "value"})
        assert isinstance(size, int)
        assert size > 0

    def test_empty_dict(self):
        size = _estimate_size({})
        assert size == 2  # "{}"

    def test_estimate_returns_int_for_any_object(self):
        """Even unserializable objects produce a size via default=str fallback."""
        class Unserializable:
            def __str__(self):
                return "unserializable"

        size = _estimate_size(Unserializable())
        assert isinstance(size, int)
        assert size > 0  # default=str provides a string representation


# ============================================================================
# _serialize_message
# ============================================================================

class TestSerializeMessage:
    """Full message serialization pipeline."""

    @pytest.fixture
    def basic_message(self):
        """A mock Telethon message with common fields."""
        msg = MagicMock()
        msg.id = 12345
        msg.date = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        msg.text = "Hello world! This is a test message."
        msg.message = msg.text  # raw TL field
        msg.entities = []
        msg.media = None
        msg.post_author = None
        msg.reply_to = None
        msg.fwd_from = None
        msg.edit_date = None
        msg.grouped_id = None
        msg.views = 150
        msg.forwards = 3
        msg.reactions = None
        # peer_id & from_id
        peer = MagicMock()
        peer.channel_id = 111222
        peer.chat_id = None
        peer.user_id = None
        msg.peer_id = peer
        frm = MagicMock()
        frm.channel_id = None
        frm.user_id = 999888
        msg.from_id = frm
        return msg

    def test_basic_serialization(self, basic_message):
        data, truncated = _serialize_message(
            basic_message, ["urgent", "test"], "@test_channel", "111222"
        )
        assert truncated is False
        assert data["id"] == 12345
        assert data["message"] == "Hello world! This is a test message."
        assert data["peer_id"] == 111222
        assert data["from_id"] == 999888
        assert data["_meta"]["chat_id"] == "111222"
        assert data["_meta"]["chat_identifier"] == "@test_channel"
        assert data["_meta"]["matched_keywords"] == ["urgent", "test"]

    def test_truncation_for_large_message(self, basic_message):
        """A very long message should be truncated."""
        # Use a message well over _MAX_DOC_SIZE_BYTES to trigger truncation
        # _MAX_DOC_SIZE_BYTES = 900_000, so 2_000_000 chars of "X" is ~2MB
        long_text = "X" * 2_000_000
        basic_message.text = long_text
        basic_message.message = long_text

        data, truncated = _serialize_message(
            basic_message, ["kw"], "@chat", "111222"
        )
        assert truncated is True
        assert data["_meta"]["_truncated"] is True
        assert "_original_text_length" in data["_meta"]
        assert len(data["message"]) < len(long_text)
