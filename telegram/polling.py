"""Phase logic for the Telegram polling pipeline.

This module holds the working bodies of the three pipeline phases so that
``telegram.listener.poll_telegram`` stays a thin orchestrator:

  * ``_register_chats``      — Phase 1: reconcile TARGET_CHATS_LIST vs cursor_base
  * ``_poll_all_chats``      — Phase 2: per-chat entity resolution + message poll
  * ``_finalize``            — Phase 3: cross-contamination, backup, final save

Shared mutable state (counters, date range, ``db_was_updated`` flag) is carried
in a :class:`_PollStats` instance.  Early-exit paths (shutdown signal, flood
wait) raise :class:`_StopPolling` after persisting partial state; the caller
owns session/client cleanup.
"""

import logging
import time
import asyncio
from dataclasses import dataclass
from typing import Any

from telethon.errors import FloodWaitError

from telegram.send import send_alert, send_health_alert, send_bot_notification
from telegram.message_store import save_matched_message_to_firestore
from telegram.helpers import (
    _chat_sort_key,
    _find_matching_keywords,
    _get_priority_chat_refs,
    _safe_title,
    _should_stop,
)
from telegram.cursor import (
    _find_cross_contamination,
    _mark_alerted,
    _sanitize_cursor,
    _save_cursor_sync,
    _was_alerted,
)
from telegram.resolution import (
    _lookup_dialog,
    _resolve_by_numeric_id,
)

logger = logging.getLogger(__name__)


@dataclass
class _PollStats:
    """Mutable counters and state accumulated across a single poll cycle."""

    processed: int = 0
    matches: int = 0
    alerts: int = 0
    first_msg_date: Any = None
    last_msg_date: Any = None
    db_was_updated: bool = False


class _StopPolling(Exception):
    """Internal signal to abort the poll cycle after partial state is saved."""


def _setup_deadline(max_runtime_seconds):
    """Compute the monotonic deadline (or None) from the runtime budget."""
    if max_runtime_seconds and max_runtime_seconds > 0:
        deadline = time.monotonic() + max_runtime_seconds
        logger.info("⏱️  Max poll runtime: %d seconds (deadline ≈ %s)",
                    max_runtime_seconds, time.strftime('%H:%M:%S', time.localtime(deadline)))
        return deadline
    logger.info("⏱️  No time limit set — runs until all messages processed or interrupted.")
    return None


async def _alert_access_lost(values, chat_id_str, value0, error, http_session) -> bool:
    """Send a one-shot "access lost" health alert; True if actually sent."""
    if _was_alerted(values, "access_lost"):
        return False
    await send_health_alert(
        "Lost access to chat",
        f"**Chat ID:** `{chat_id_str}`\n"
        f"**Last known ref:** `{value0}`\n"
        f"**Error:** {error}\n"
        f"Cannot resolve by username or numeric ID. "
        f"The account may have lost access or the chat was deleted.",
        level="error",
        session=http_session,
    )
    _mark_alerted(values, "access_lost")
    return True


async def _register_chats(client, dialog_cache, target_chats, known_usernames_to_ids,
                          cursor_base, http_session, shutdown_event, deadline, fs, stats):
    """Reconcile TARGET_CHATS_LIST against cursor_base (Phase 1).

    Resolves new/changed references, registers brand-new chats (seeding their
    cursor at the latest message ID so history isn't replayed), and updates
    renamed handles.  Raises :class:`_StopPolling` if a shutdown signal arrives
    mid-registration.
    """
    alerted_chats: set = set()  # deduplicate health alerts within one poll cycle

    for chat_ref in target_chats:  # TARGET_CHATS: @name1, @name2..
        # --- Shutdown check during registration ---
        should_stop, stop_reason = _should_stop(shutdown_event, deadline)
        if should_stop:
            logger.warning("🛑 Shutdown (%s) during chat registration. Saving partial state.", stop_reason)
            if stats.db_was_updated:
                _save_cursor_sync(fs, cursor_base)
            raise _StopPolling()

        try:
            # Determine how to resolve this chat reference:
            #   - "@username" → pass as string to get_entity
            #   - "123456789" (numeric) → resolve via dialog cache
            #   - numeric and already in cursor_base → skip network call
            is_numeric_ref = chat_ref.isdigit()

            if is_numeric_ref and chat_ref in cursor_base:
                # Numeric ID already known — no network call needed.
                # Update the stored ref to numeric ID (in case it was an old @username).
                old_ref = cursor_base[chat_ref]["ref"]
                if old_ref != chat_ref:
                    logger.warning(
                        "Chat %s: reference updated from '%s' to numeric ID '%s'.",
                        chat_ref, old_ref, chat_ref)
                    cursor_base[chat_ref]["ref"] = chat_ref
                    stats.db_was_updated = True
                else:
                    logger.debug("Chat '%s' already in database. Skipping.", chat_ref)
            elif chat_ref not in known_usernames_to_ids:
                logging.info("New or changed username: '%s', use the network to get its ID", chat_ref)
                # Resolve: digit-only → dialog cache (has access_hash), else → username string
                if is_numeric_ref:
                    dlg = _lookup_dialog(dialog_cache, chat_ref)
                    if dlg is None:
                        raise ValueError(
                            f"Chat ID {chat_ref} not found in dialogs. "
                            f"Account may not be a member, or the ID is wrong."
                        )
                    entity = dlg.entity
                    logger.info("Resolved numeric ID %s via dialog cache → '%s'.", chat_ref, getattr(entity, 'title', chat_ref))
                else:
                    entity = await client.get_entity(chat_ref)
                    # Guard: if a username now resolves to a User (e.g. after
                    # a group went private and the handle was reassigned),
                    # do NOT register it as a new chat — skip with a warning.
                    if not hasattr(entity, 'title'):
                        logger.warning(
                            "Username '%s' resolved to a %s (id=%s) instead of a channel/group. "
                            "The handle may have been reassigned after the original chat went private. "
                            "Skipping registration — use numeric ID instead.",
                            chat_ref, type(entity).__name__, entity.id)
                        continue
                chat_id_str = str(entity.id)

                # This logic handles two cases:
                # A) A brand-new chat ID.
                # B) A chat ID we know, but with a new username (a rename).
                if chat_id_str not in cursor_base:
                    # --- Case A: Brand new chat ---
                    logging.info(f"Adding new chat '{_safe_title(entity)}' ({chat_id_str}) to database.")
                    try:
                        last_msg = await client.get_messages(entity, limit=1)
                    except FloodWaitError as e:
                        # Telethon is *also* sleeping on its own.
                        logging.critical(f"We hit a flood wait for {e.seconds} seconds. My bot is too fast!")
                    except Exception as e:
                        logging.error(f"A different error: {e}")
                    last_id = last_msg[0].id if last_msg else 0
                    cursor_base[chat_id_str] = {
                        "ref": chat_ref,
                        "last_processed_id": last_id,
                        "alerted_keys": "",
                        "schema_version": 1,
                    }
                else:
                    # --- Case B: Renamed chat ---
                    old_ref = cursor_base[chat_id_str]["ref"]
                    logging.warning(
                        f"Username for {chat_id_str} changed from '{old_ref}' to '{chat_ref}'. Updating.")
                    cursor_base[chat_id_str]["ref"] = chat_ref  # Update the username
                stats.db_was_updated = True
            else:
                logging.debug(f"Chat '{chat_ref}' already in database. Skipping.")
        except Exception as e:
            # This will catch errors from get_entity (e.g., username not found)
            logger.critical("Could not resolve or process %s: %s", chat_ref, e)
            if chat_ref not in alerted_chats:
                alerted_chats.add(chat_ref)
                await send_health_alert(
                    "Chat resolution failed",
                    f"**Ref:** `{chat_ref}`\n"
                    f"**Error:** {e}\n"
                    f"Chat could not be resolved. It will not be monitored this cycle.",
                    level="error",
                    session=http_session,
                )

    # Upload to Firebase *ONCE* at the end of registration, only if needed.
    if stats.db_was_updated:
        logging.info("Database was updated, uploading to Firebase...")
        fs.set_firejson(cursor_base, merge=True)
    else:
        logging.debug("No database changes detected.")


async def _resolve_chat_peer(client, dialog_cache, chat_id_str, values, http_session, stats):
    """Resolve the InputPeer for one chat, updating the stored ref if needed.

    Returns the resolved peer, or None if the chat must be skipped this cycle.
    Side effects: may rewrite ``values["ref"]`` and send one-shot health alerts
    ("access_lost" / "username_lost"), setting ``stats.db_was_updated``.
    """
    value0 = values["ref"]

    # B1. Numeric ref → dialog cache (no network).  This is the steady state
    #     for private groups; failing here raises a one-shot "access_lost"
    #     health alert.
    if value0.isdigit():
        try:
            entity = _resolve_by_numeric_id(dialog_cache, chat_id_str)
            return await client.get_input_entity(entity)
        except Exception as e2:
            logging.error(f"Cannot resolve chat {chat_id_str} by numeric ID: {e2}. Skipping.")
            if await _alert_access_lost(values, chat_id_str, value0, e2, http_session):
                stats.db_was_updated = True
            return None

    # B2. Username ref → network lookup, then numeric fallback.  A stale handle
    #     (reassigned to another entity) or a lost handle triggers a one-shot
    #     "username_lost" alert and the ref is rewritten to the numeric ID.
    try:
        entity = await client.get_entity(value0)
        # Guard: if the username now resolves to a *different* entity, treat it
        # as a resolution failure and fall through to the numeric-ID fallback.
        if str(entity.id) != chat_id_str:
            raise ValueError(
                f"Entity ID mismatch for '{value0}': expected {chat_id_str}, "
                f"got {entity.id} (type={type(entity).__name__}). "
                f"The username may have been reassigned after the chat went private."
            )
        return entity  # from network — has access_hash
    except Exception:
        logging.warning(
            f"Username '{value0}' not found for chat {chat_id_str} "
            f"(group may have lost its public username or gone private). "
            f"Trying by numeric ID..."
        )
        try:
            entity = _resolve_by_numeric_id(dialog_cache, chat_id_str)
            msg_peer = await client.get_input_entity(entity)
        except Exception as e2:
            logging.error(f"Cannot resolve chat {chat_id_str} by numeric ID either: {e2}. Skipping.")
            if await _alert_access_lost(values, chat_id_str, value0, e2, http_session):
                stats.db_was_updated = True
            return None

        # Update stored reference: new username if available, else numeric ID.
        new_username = getattr(entity, 'username', None)
        if new_username:
            new_ref = f"@{new_username}"
            logging.info(f"Chat {chat_id_str} renamed from '{value0}' to '{new_ref}'. Updating cursor.")
            values["ref"] = new_ref
        else:
            logging.info(
                f"Chat {chat_id_str} ('{_safe_title(entity)}') has no public username. "
                f"Will resolve by numeric ID from now on."
            )
            values["ref"] = str(chat_id_str)

        # --- Health alert: username lost, now tracking by numeric ID ---
        if not _was_alerted(values, "username_lost"):
            await send_health_alert(
                "Chat username lost",
                f"**Chat:** `{_safe_title(entity)}`\n"
                f"**Old ref:** `{value0}`\n"
                f"**Now tracking by ID:** `{chat_id_str}`\n"
                f"The @username no longer resolves. Group may have gone private.",
                level="warning",
                session=http_session,
            )
            _mark_alerted(values, "username_lost")
            stats.db_was_updated = True
        stats.db_was_updated = True

        return msg_peer


async def _process_messages(messages, keywords, http_session, current_cursor,
                            chat_ref, chat_id, stats) -> int:
    """Match/alert over a message batch (oldest-first) and guard the cursor.

    Returns the final ``last_acked_id`` for the chat.  A failed alert breaks
    the batch so the cursor never skips past an unacknowledged message.
    """
    newest_message_id = messages[0].id
    last_acked_id = current_cursor  # fallback: don't advance if nothing acked

    # Track overall date range across all chats.
    if messages:
        if stats.first_msg_date is None or messages[-1].date < stats.first_msg_date:
            stats.first_msg_date = messages[-1].date  # oldest
        if stats.last_msg_date is None or messages[0].date > stats.last_msg_date:
            stats.last_msg_date = messages[0].date  # newest

    for message in reversed(messages):
        # --- C1. Match & alert pipeline -------------------------------
        # Iterate oldest-first so the cursor advances in order.  A failed
        # alert breaks the batch so the cursor never skips past an
        # unacknowledged message.
        stats.processed += 1
        message_text = message.text

        if message_text:
            found_keywords = _find_matching_keywords(message_text, keywords)

            logging.debug(f"Message ID {message.id}: {repr(message_text[:50])}...")
            logging.debug(f"Matched keywords: {found_keywords}")

            if found_keywords:
                stats.matches += 1
                # Save full message to Firestore (independent of alert success).
                try:
                    await save_matched_message_to_firestore(message, found_keywords)
                except Exception as fs_e:
                    logging.error("Failed to save message %s to Firestore: %s", message.id, fs_e)
                try:
                    await send_alert(message, found_keywords, session=http_session)
                    logging.info(f"Alarm sent successfully for message ID: {message.id}")
                    stats.alerts += 1
                    last_acked_id = message.id  # only advance cursor on success
                except Exception as alert_e:
                    logging.error(f"❌ ERROR sending alert for Message ID {message.id}: {alert_e}. "
                                  f"Cursor will NOT advance past this message — will retry on next run.")
                    break
        else:
            logging.debug(f"Message ID {message.id}: (Non-text message)")

        # For non-keyword messages, advance cursor normally.
        if not message_text or not found_keywords:
            last_acked_id = message.id

    # --- C2. Cursor sanity guards ---
    # Clamp the computed cursor to safe bounds: never above the newest fetched
    # message, never below the previous cursor.
    guard_reason = _sanitize_cursor(last_acked_id, newest_message_id, current_cursor)
    if guard_reason == "too_high":
        logger.critical(
            "CURSOR GUARD: computed cursor %d exceeds newest message %d "
            "for chat '%s' (%s). Refusing to advance — keeping %d.",
            last_acked_id, newest_message_id, chat_ref, chat_id, current_cursor
        )
        try:
            await send_bot_notification(
                f"⚠️ **CURSOR GUARD TRIGGERED**\n"
                f"Chat: `{chat_ref}` (`{chat_id}`)\n"
                f"Computed cursor ({last_acked_id}) > newest message ({newest_message_id})\n"
                f"Cursor NOT advanced — kept at {current_cursor}",
                session=http_session,
            )
        except Exception as alert_e:
            logger.error("Failed to send cursor guard alert: %s", alert_e)
        last_acked_id = current_cursor
    elif guard_reason == "too_low":
        logger.warning(
            "CURSOR GUARD: computed cursor %d is LESS than current cursor %d "
            "for chat '%s' (%s). May indicate cross-contamination. Keeping current cursor.",
            last_acked_id, current_cursor, chat_ref, chat_id
        )
        last_acked_id = current_cursor

    return last_acked_id


async def _poll_all_chats(client, dialog_cache, cursor_base, keywords, http_session,
                          fs, shutdown_event, deadline, stats) -> dict:
    """Poll messages for every known chat (Phase 2).

    Priority chats first, then numeric ID smallest first — see
    :func:`telegram.helpers._chat_sort_key`.  Returns the per-chat new cursor
    values used for cross-contamination detection.
    """
    cursors_to_write: dict[str, int] = {}
    priority_refs = _get_priority_chat_refs()

    for name, values in sorted(
        cursor_base.items(), key=lambda kv: _chat_sort_key(kv, priority_refs)
    ):
        # --- Shutdown check: skip remaining chats if stopping ---
        should_stop, stop_reason = _should_stop(shutdown_event, deadline)
        if should_stop:
            logger.warning("🛑 Shutdown (%s) — skipping remaining chats. Progress for completed chats saved.", stop_reason)
            break
        # --- Guard: validate cursor entry structure ---
        if not isinstance(values, dict):
            logger.error("Corrupt entry for chat '%s': %s. Skipping.", name, values)
            continue

        value0 = values["ref"]
        current_last_message_id = values["last_processed_id"]
        chat_id_str = str(name)
        logging.debug(f"Scanning '{value0}' (ID: {chat_id_str}) after ID {current_last_message_id}")

        await asyncio.sleep(2)  # Delay for stability

        # Resolve entity (username first, numeric-ID fallback).  None → skip.
        msg_peer = await _resolve_chat_peer(
            client, dialog_cache, chat_id_str, values, http_session, stats
        )
        if msg_peer is None:
            continue

        # Fetch messages newer than the cursor (newest-first order).
        try:
            messages = await client.get_messages(msg_peer, min_id=current_last_message_id, limit=None)
            if not messages:
                logging.debug("No new messages found.")
                continue
            logging.debug(f"Fetched {len(messages)} new messages.")
        except FloodWaitError as e:
            logging.critical(
                "⛈️  Flood wait %d s for '%s' (chat %s). Saving cursor and stopping to avoid timeout.",
                e.seconds, value0, chat_id_str,
            )
            _save_cursor_sync(fs, cursor_base)
            raise _StopPolling()
        except Exception as e:
            logging.error(f"Failed to fetch messages for '{value0}' (chat {chat_id_str}): {e}")
            continue  # skip this chat on error (e.g., bot account can't use GetHistoryRequest)
        stats.db_was_updated = True  # need to save our state

        last_acked_id = await _process_messages(
            messages, keywords, http_session, current_last_message_id,
            value0, chat_id_str, stats,
        )

        cursor_base[name]["last_processed_id"] = last_acked_id
        cursors_to_write[name] = last_acked_id

        # --- Save cursor after EACH chat (incremental persistence) ---
        _save_cursor_sync(fs, cursor_base)

        # --- If shutdown triggered mid-chat, stop processing further chats ---
        should_stop, stop_reason = _should_stop(shutdown_event, deadline)
        if should_stop:
            logger.warning("🛑 Shutdown (%s) mid-chat '%s'. Last acked message ID: %d.",
                           stop_reason, value0, last_acked_id)
            break

    return cursors_to_write


async def _finalize(cursors_to_write, cursor_base, http_session, fs, stats) -> None:
    """Cross-contamination check, cursor_base backup, and final write (Phase 3)."""
    # --- Cross-contamination detection before saving ---
    if stats.db_was_updated and cursors_to_write:
        duplicate_cursors = _find_cross_contamination(cursors_to_write)
        if duplicate_cursors:
            logger.critical(
                "CROSS-CONTAMINATION DETECTED: %d cursor value(s) shared by multiple chats: %s. "
                "Aborting save to protect cursor_base.",
                len(duplicate_cursors),
                {str(v): [str(c) for c in chats] for v, chats in duplicate_cursors.items()}
            )
            try:
                detail_lines = []
                for val, chats in duplicate_cursors.items():
                    chat_list = ", ".join(f"`{c}`" for c in chats)
                    detail_lines.append(f"• Cursor `{val}` shared by: {chat_list}")
                await send_bot_notification(
                    f"🚨 **CROSS-CONTAMINATION DETECTED**\n"
                    f"Multiple chats would get the same cursor value:\n"
                    + "\n".join(detail_lines) +
                    f"\n\nSave **ABORTED** — cursor_base was NOT updated.",
                    session=http_session,
                )
            except Exception as alert_e:
                logger.error("Failed to send cross-contam alert: %s", alert_e)
            stats.db_was_updated = False  # abort the save

    # --- Backup cursor_base before writing ---
    if stats.db_was_updated:
        logger.info("Creating backup of cursor_base before saving...")
        try:
            fs.backup_document()
            fs.prune_old_backups(max_backups=30)
        except Exception as e:
            logger.error("Backup failed (non-fatal): %s. Proceeding with save.", e)

    # Save to Firebase *only* if there were changes.
    if stats.db_was_updated:
        fs.set_firejson(cursor_base, merge=True)
        logging.debug(f"Saved previous_checked_ids to Firebase...: {cursor_base}")
