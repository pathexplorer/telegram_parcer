"""Telethon entity resolution helpers for the Telegram poller.

Resolving a chat by its numeric ID needs a proper ``access_hash``, which the
dialog cache carries.  These helpers build that cache and look up entities by
their public numeric ID.
"""

import logging

logger = logging.getLogger(__name__)


async def _build_dialog_cache(client) -> dict:
    """Build a {dialog_id: Dialog} cache for numeric-ID fallback resolution.

    Dialog objects include input_entity with proper access_hash, essential for
    API calls like get_messages on private groups.
    """
    dialog_cache: dict = {}
    try:
        async for dialog in client.iter_dialogs():
            dialog_cache[dialog.id] = dialog
        logger.info("📇 Cached %d dialogs for fallback resolution.", len(dialog_cache))
    except Exception as e:
        logger.warning("⚠️  Could not fetch dialogs for fallback cache: %s", e)
    return dialog_cache


def _lookup_dialog(dialog_cache, numeric_id_str):
    """Look up a Dialog in the cache by public numeric ID.

    Telethon entity.id returns the positive public ID (e.g. 1511100059),
    but dialog.id uses the internal peer ID (e.g. -1001511100059 for supergroups).
    We try both formats. Returns a Telethon Dialog object or None.
    """
    nid = int(numeric_id_str)
    for candidate in (nid, -nid, int(f"-100{numeric_id_str}")):
        dlg = dialog_cache.get(candidate)
        if dlg is not None:
            return dlg
    return None


def _resolve_by_numeric_id(dialog_cache, chat_id_str):
    """Return the entity for chat_id_str from the dialog cache.

    ``dlg.entity.id`` is the positive public ID (e.g. 1511100059),
    while ``dialog.id`` is the internal peer ID (-100…).
    """
    dlg = _lookup_dialog(dialog_cache, chat_id_str)
    if dlg is None:
        raise ValueError(
            f"Chat ID {chat_id_str} not found in dialogs "
            f"(account may have lost access or chat was deleted)."
        )
    return dlg.entity
