"""Firestore persistence for keyword-matched Telegram messages.

Stores the FULL, uncropped Telethon Message for every keyword-matched message
alongside the existing 300-character Telegram alert pipeline.
"""

import logging

from gcp_actions.firestore_box.json_manipulations import FirestoreMagic

logger = logging.getLogger(__name__)

# Firestore document limit is 1 MiB (1 048 576 bytes).
# We keep a conservative margin so metadata fields always fit.
_MAX_DOC_SIZE_BYTES = 900_000

# Top-level collection name — each document ID is "{chat_id}_{message_id}".
_COLLECTION = "matched_messages"


async def save_matched_message_to_firestore(message, found_keywords):
    """Persist the FULL Telethon message to Firestore.

    The document is keyed by ``{chat_id}_{message.id}`` which provides natural
    idempotency — re-running the same poll cycle cannot create duplicates.

    Args:
        message: A :class:`telethon.tl.custom.Message` object.
        found_keywords: List of matched keyword strings.

    Raises:
        Exception: On serialization or Firestore write failures (caller decides
                   whether to block the alert pipeline).
    """
    chat_entity = await message.get_chat()
    chat_id = str(chat_entity.id)
    chat_identifier = (
        chat_entity.username
        or getattr(chat_entity, 'title', None)
        or getattr(chat_entity, 'first_name', None)
        or chat_id
    )

    doc_id = f"{chat_id}_{message.id}"

    # 1. Serialize ----------------------------------------------------------------
    try:
        data, was_truncated = _serialize_message(
            message, found_keywords, chat_identifier, chat_id
        )
    except Exception:
        logger.exception("Failed to serialize message %s.", doc_id)
        raise

    # 2. Write to Firestore -------------------------------------------------------
    try:
        fs = FirestoreMagic(_COLLECTION, doc_id)
        fs.set_firejson(data, merge=False)
    except Exception:
        logger.exception("Failed to write message %s to Firestore.", doc_id)
        raise

    if was_truncated:
        logger.warning("✂️  Message %s saved (TRUNCATED — exceeded %d bytes).",
                       doc_id, _MAX_DOC_SIZE_BYTES)
    else:
        logger.info("💾 Message %s saved to Firestore (full text, %d chars).",
                    doc_id, len(message.text or ""))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Only these TL fields are persisted.  Everything else (service flags,
# paid-promotion metadata, rarely-used fields) is dropped to keep documents
# lean and human-readable.
_KEPT_FIELDS: frozenset = frozenset({
    # -- identity & time ----------------------------------------------------
    "id",
    "date",
    # -- content ------------------------------------------------------------
    "message",          # overridden with message.text below
    "entities",         # @mentions, #hashtags, links, formatting
    "media",            # photo / document / web-preview
    "post_author",      # channel post signature
    # -- context (only present when applicable) -----------------------------
    "reply_to",         # reply header
    "fwd_from",         # forward header
    "edit_date",        # last edit timestamp
    "grouped_id",       # album / grouped-media ID
    # -- metrics ------------------------------------------------------------
    "views",
    "forwards",
    "reactions",
})


def _serialize_message(message, found_keywords, chat_identifier, chat_id):
    """Convert a Telethon Message into a minimal, Firestore-safe dict.

    Returns:
        (data_dict, is_truncated: bool)
    """
    # 1. Extract only the fields we care about ----------------------------------
    raw: dict = {}
    for field_name in _KEPT_FIELDS:
        try:
            value = getattr(message, field_name, None)
        except Exception:
            value = None
        raw[field_name] = _extract_tl_value(value)

    # 2. OVERRIDE the ``message`` field with ``message.text`` — this is the
    #    canonical, full message text.
    raw["message"] = message.text

    # 3. Simplify peer / sender to plain IDs (instead of nested TL dicts) ------
    try:
        pid = message.peer_id
        raw["peer_id"] = getattr(pid, "channel_id", None) or getattr(pid, "chat_id", None) or getattr(pid, "user_id", None)
    except Exception:
        raw["peer_id"] = None
    try:
        fid = message.from_id
        raw["from_id"] = getattr(fid, "channel_id", None) or getattr(fid, "user_id", None)
    except Exception:
        raw["from_id"] = None

    # 4. Attach pipeline metadata -----------------------------------------------
    raw["_meta"] = {
        "chat_id": chat_id,
        "chat_identifier": chat_identifier,
        "matched_keywords": found_keywords,
    }

    # 5. Strip every ``None`` value (including deeply nested) so documents
    #    stay compact — no ``"factcheck": null`` noise.
    raw = _strip_nulls(raw)

    # 6. Size guard -------------------------------------------------------------
    is_truncated = False
    size_estimate = _estimate_size(raw)
    if size_estimate > _MAX_DOC_SIZE_BYTES:
        text_field = raw.get("message", "")
        if isinstance(text_field, str) and text_field:
            max_text_chars = len(text_field) // 2
            while max_text_chars > 0:
                raw["message"] = text_field[:max_text_chars]
                if _estimate_size(raw) <= _MAX_DOC_SIZE_BYTES:
                    break
                max_text_chars //= 2
            raw["_meta"]["_truncated"] = True
            raw["_meta"]["_original_text_length"] = len(text_field)
            is_truncated = True

    return raw, is_truncated


def _strip_nulls(obj):
    """Recursively remove keys whose value is ``None`` (or empty dicts/lists
    after stripping), returning a compact dict/list/scalar."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            v = _strip_nulls(v)
            if v is not None:
                cleaned[k] = v
        return cleaned or None
    if isinstance(obj, list):
        cleaned = [_strip_nulls(v) for v in obj if _strip_nulls(v) is not None]
        return cleaned or None
    return obj


def _extract_tl_value(obj):
    """Recursively convert a Telethon TL object (or any value) into a
    plain Python dict/list/scalar suitable for Firestore.

    * TLObject subclasses → dict of their constructor fields (recursively).
    * datetime        → ``.isoformat()`` string.
    * bytes           → base64-encoded string wrapper.
    * list/tuple/set  → list of recursively-converted items.
    * Everything else → returned as-is (str, int, float, bool, None).
    """
    if obj is None:
        return None
    if isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        import base64
        return {"_bytes_b64": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, (list, tuple, set)):
        return [_extract_tl_value(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _extract_tl_value(v) for k, v in obj.items()}

    # datetime / date → ISO string
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)

    # Telethon TLObject — recursively unpack its constructor fields
    if hasattr(obj, "__dict__") and hasattr(obj, "CONSTRUCTOR_ID"):
        return _tlobject_to_dict(obj)

    # Last resort: string representation
    return str(obj)


def _tlobject_to_dict(tl) -> dict:
    """Recursively serialize an arbitrary Telethon TLObject to a dict.

    Uses the same ``inspect.signature`` approach — extracts every
    constructor parameter and converts nested values.
    """
    import inspect
    result: dict = {}
    try:
        params = list(inspect.signature(tl.__init__).parameters.keys())
        params.remove("self")
    except Exception:
        return {"_str": str(tl)}

    for field_name in params:
        try:
            value = getattr(tl, field_name, None)
        except Exception:
            value = None
        result[field_name] = _extract_tl_value(value)

    result["_tl_type"] = type(tl).__name__
    return result


def _estimate_size(obj) -> int:
    """Return a rough byte-size estimate for a Firestore-bound dict."""
    import json
    try:
        return len(json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0
