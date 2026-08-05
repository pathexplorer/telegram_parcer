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

def _serialize_message(message, found_keywords, chat_identifier, chat_id):
    """Convert a Telethon Message into a Firestore-safe dict.

    Returns:
        (data_dict, is_truncated: bool)
    """
    # Telethon's built-in serialization
    raw = message.to_dict() if hasattr(message, 'to_dict') else _fallback_to_dict(message)

    sanitized = _sanitize_value(raw)

    # Attach pipeline metadata
    sanitized["_meta"] = {
        "chat_id": chat_id,
        "chat_identifier": chat_identifier,
        "matched_keywords": found_keywords,
    }

    # Size guard: truncate the ``message`` text field if the whole payload is too large.
    is_truncated = False
    size_estimate = _estimate_size(sanitized)
    if size_estimate > _MAX_DOC_SIZE_BYTES:
        text_field = sanitized.get("message", "")
        if isinstance(text_field, str) and text_field:
            # Rough heuristic: cut text in half and re-check; still too big → halve again.
            max_text_chars = len(text_field) // 2
            while max_text_chars > 0:
                sanitized["message"] = text_field[:max_text_chars]
                if _estimate_size(sanitized) <= _MAX_DOC_SIZE_BYTES:
                    break
                max_text_chars //= 2
            sanitized["_meta"]["_truncated"] = True
            sanitized["_meta"]["_original_text_length"] = len(text_field)
            is_truncated = True

    return sanitized, is_truncated


def _sanitize_value(obj):
    """Recursively convert a value to a Firestore-compatible type.

    Firestore natively supports: None, bool, int, float, str, bytes,
    datetime, geo_point, list, dict.  Everything else is coerced to str.
    """
    if obj is None:
        return None
    if isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        import base64
        return {"_bytes_b64": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, dict):
        return {str(k): _sanitize_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize_value(v) for v in obj]
    if hasattr(obj, 'isoformat'):
        # datetime / date → Firestore handles natively, but Telethon's
        # to_dict may already have converted them to timestamps / strings.
        # Keep the ISO string as a safe fallback.
        return obj.isoformat()
    # Last resort: string representation
    return str(obj)


def _estimate_size(obj) -> int:
    """Return a rough byte-size estimate for a Firestore-bound dict."""
    import json
    try:
        return len(json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0


def _fallback_to_dict(message) -> dict:
    """Manual fallback serialization if ``message.to_dict()`` is unavailable."""
    return {
        "id": getattr(message, "id", None),
        "message": getattr(message, "text", None),
        "date": getattr(message, "date", None),
        "peer_id": str(getattr(message, "peer_id", "")),
        "from_id": str(getattr(message, "from_id", "")),
        "out": getattr(message, "out", None),
        "mentioned": getattr(message, "mentioned", None),
        "media_unread": getattr(message, "media_unread", None),
        "silent": getattr(message, "silent", None),
        "post": getattr(message, "post", None),
        "grouped_id": getattr(message, "grouped_id", None),
    }
