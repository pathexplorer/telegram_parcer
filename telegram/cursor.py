"""Cursor-entry structure and cursor-safety helpers for the Telegram poller.

The cursor_base document maps chat ID → ``{"ref", "last_processed_id",
"alerted_keys", "schema_version"}``.  This module owns:

  * the ``alerted_keys`` CSV field (Firestore forbids nested arrays),
  * synchronous cursor persistence,
  * sanity guards that prevent a bad cursor from advancing,
  * cross-contamination detection across chats.
"""

import logging

logger = logging.getLogger(__name__)


def _save_cursor_sync(fs, previous_checked_ids):
    """Persist cursor to Firestore (blocking, called sparingly)."""
    try:
        fs.set_firejson(previous_checked_ids, merge=True)
        logger.info("💾 Cursor saved to Firestore.")
    except Exception as e:
        logger.error("❌ Failed to save cursor to Firestore: %s", e)


def _ensure_alerted_keys(values):
    """Ensure the cursor entry has an alerted_keys field.

    Cursor format: {"ref": str, "last_processed_id": int,
                    "alerted_keys": str, "schema_version": int}
    The alerted_keys field is a comma-separated string of alert keys
    (Firestore does not allow nested arrays).
    """
    if "alerted_keys" not in values:
        values["alerted_keys"] = ""


def _was_alerted(values, alert_key):
    """Check if a specific one-shot health alert was already sent for this chat."""
    _ensure_alerted_keys(values)
    alerted = values["alerted_keys"]
    return alert_key in (alerted.split(",") if alerted else [])


def _mark_alerted(values, alert_key):
    """Record that a health alert was sent (persisted on next Firestore save)."""
    _ensure_alerted_keys(values)
    existing = [k for k in values["alerted_keys"].split(",") if k] if values["alerted_keys"] else []
    if alert_key not in existing:
        existing.append(alert_key)
        values["alerted_keys"] = ",".join(existing)


def _sanitize_cursor(last_acked_id, newest_message_id, current_cursor):
    """Check a computed cursor against sanity bounds, returning a guard reason.

    Guards against two failure modes:
      * ``too_high`` — the computed cursor exceeds the newest fetched message
        (out-of-order batch / cross-chat contamination).  Cursor must be held
        at ``current_cursor`` and the caller should raise an alarm.
      * ``too_low``  — the computed cursor is below the current cursor
        (suspected cross-contamination).  Cursor must be held at
        ``current_cursor``.

    Returns:
        A reason string ("too_high" or "too_low") when a guard fired, else
        None.  The caller keeps the computed cursor for logging and clamps to
        ``current_cursor`` when a reason is returned.
    """
    if last_acked_id > newest_message_id:
        return "too_high"
    if last_acked_id < current_cursor:
        return "too_low"
    return None


def _find_cross_contamination(cursors_to_write: dict) -> dict:
    """Return the subset of *cursors_to_write* shared by more than one chat.

    Args:
        cursors_to_write: {chat_id: new_cursor_value} collected this cycle.

    Returns:
        {cursor_value: [chat_id, ...]} for any value claimed by >1 chat.
    """
    cursor_to_chats: dict = {}
    for chat_id, cursor_val in cursors_to_write.items():
        cursor_to_chats.setdefault(cursor_val, []).append(chat_id)
    return {val: chats for val, chats in cursor_to_chats.items() if len(chats) > 1}
