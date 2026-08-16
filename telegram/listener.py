import logging
import asyncio
import time
import os
import unicodedata

import aiohttp
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from gcp_actions.firestore_box.json_manipulations import FirestoreMagic
from telegram.send import send_alert, send_health_alert, send_bot_notification
from telegram.message_store import save_matched_message_to_firestore
from project_env.config import session_string, API_ID, API_HASH

logger = logging.getLogger(__name__)


def _get_priority_chat_refs() -> set[str]:
    """Read the comma-separated PRIORITY_CHAT_REFS env var (case-insensitive).

    Chats matching these refs (username, e.g. ``@greenfield9000``, or numeric
    ID) are polled FIRST so e.g. an e2e test chat does not wait behind all
    other channels.  Returns a lowercased set of refs.
    """
    raw = os.getenv("PRIORITY_CHAT_REFS", "")
    return {r.strip().casefold() for r in raw.split(",") if r.strip()}


def _chat_sort_key(item, priority_refs: set[str]) -> tuple:
    """Sort key: priority chats first (by numeric ID), then the remaining chats.

    The default order is numeric chat ID ascending, which means a newly-added
    (often large-ID) test chat lands at the end of the queue.  Marking it as
    priority moves it to the front without changing the rest of the order.
    """
    name, values = item
    ref = ""
    if isinstance(values, dict):
        ref = str(values.get("ref", ""))
    is_priority = name.casefold() in priority_refs or ref.casefold() in priority_refs
    return (0 if is_priority else 1, int(name))


def _should_stop(shutdown_event, deadline):
    """Check if polling should stop due to signal or time limit.

    Args:
        shutdown_event: threading.Event or None — set by SIGINT/SIGTERM handler.
        deadline: float or None — time.monotonic() timestamp after which to stop.

    Returns:
        (should_stop: bool, reason: str or None)
    """
    if shutdown_event and shutdown_event.is_set():
        return True, "signal"
    if deadline is not None and time.monotonic() >= deadline:
        return True, "timeout"
    return False, None


def _save_cursor_sync(fs, previous_checked_ids):
    """Persist cursor to Firestore (blocking, called sparingly)."""
    try:
        fs.set_firejson(previous_checked_ids, merge=True)
        logger.info("💾 Cursor saved to Firestore.")
    except Exception as e:
        logger.error("❌ Failed to save cursor to Firestore: %s", e)


def _find_matching_keywords(text: str, keywords: list[str]) -> list[str]:
    """Return the subset of *keywords* present in *text* (substring match).

    Matching is case-insensitive and Unicode-aware: both sides are
    NFKC-normalized and casefolded so fullwidth Latin, composed/decomposed
    forms and case variants all match (e.g. "café" matches "CAFÉ" and
    "cafe\u0301"). Substring semantics — a keyword matches when it appears
    anywhere in the text, with no word-boundary or regex logic.

    Args:
        text: Raw message text (may be empty/None).
        keywords: Normalized keyword list (see starter_conf).

    Returns:
        The keywords found in *text*, in the order they appear in *keywords*.
    """
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    return [kw for kw in keywords if kw in normalized_text]


def _safe_title(entity):
    """Return a human-readable title for any Telethon entity type.

    Channel / Chat / megagroup → .title
    User                       → .first_name (+ .last_name if present)
    Fallback                   → str(entity.id)
    """
    title = getattr(entity, 'title', None)
    if title:
        return title
    first = getattr(entity, 'first_name', None)
    if first:
        last = getattr(entity, 'last_name', None)
        return f"{first} {last}".strip() if last else first
    return str(getattr(entity, 'id', 'Unknown'))


async def poll_telegram(KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids,
                        shutdown_event=None, max_runtime_seconds=None):

    # --- Per-invocation counters (reset on every call, not module globals) ---
    count_processed = 0
    count_matches = 0
    count_alerts = 0

    # --- Initialize Firestore client for cursor_base ---
    fs = FirestoreMagic("telegram", "cursor_base")

    # --- Guard: validate inputs before entering the loop ---
    if not TARGET_CHATS_LIST:
        logger.critical("FATAL: TARGET_CHATS_LIST is empty. Nothing to scan.")
        return
    if not KEYWORDS_LIST:
        logger.critical("FATAL: KEYWORDS_LIST is empty. Nothing to match.")
        return
    first_msg_date = None
    last_msg_date = None

    # --- Time limit setup ---
    deadline = None
    if max_runtime_seconds and max_runtime_seconds > 0:
        deadline = time.monotonic() + max_runtime_seconds
        logger.info("⏱️  Max poll runtime: %d seconds (deadline ≈ %s)",
                    max_runtime_seconds, time.strftime('%H:%M:%S', time.localtime(deadline)))
    else:
        logger.info("⏱️  No time limit set — runs until all messages processed or interrupted.")

    async with TelegramClient(StringSession(session_string), API_ID, API_HASH,flood_sleep_threshold=60) as client:
        await client.start()

        # --- Create shared HTTP session for this poll cycle -------------------
        # One session reused for all Bot API calls — avoids per-call
        # connection setup and provides a uniform timeout.
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

        # --- Build dialog cache for numeric-ID fallback resolution ---
        # Dialog objects include input_entity with proper access_hash, essential for
        # API calls like get_messages on private groups.
        _dialog_cache = {}  # {dialog_id: Dialog}
        try:
            async for dialog in client.iter_dialogs():
                _dialog_cache[dialog.id] = dialog
            logger.info("📇 Cached %d dialogs for fallback resolution.", len(_dialog_cache))
        except Exception as e:
            logger.warning("⚠️  Could not fetch dialogs for fallback cache: %s", e)

        def _lookup_dialog(numeric_id_str):
            """Look up a Dialog in the cache by public numeric ID.
            
            Telethon entity.id returns the positive public ID (e.g. 1511100059),
            but dialog.id uses the internal peer ID (e.g. -1001511100059 for supergroups).
            We try both formats. Returns a Telethon Dialog object or None.
            """
            nid = int(numeric_id_str)
            for candidate in (nid, -nid, int(f"-100{numeric_id_str}")):
                dlg = _dialog_cache.get(candidate)
                if dlg is not None:
                    return dlg
            return None

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

        db_was_updated = False
        _alerted_chats = set()  # deduplicate health alerts within one poll cycle

        for chat_ref in TARGET_CHATS_LIST:  # TARGET_CHATS: @name1, @name2..
            # --- Shutdown check during registration ---
            should_stop, stop_reason = _should_stop(shutdown_event, deadline)
            if should_stop:
                logger.warning("🛑 Shutdown (%s) during chat registration. Saving partial state.", stop_reason)
                if db_was_updated:
                    _save_cursor_sync(fs, previous_checked_ids)
                await http_session.close()
                return

            try:
                # Determine how to resolve this chat reference:
                #   - "@username" → pass as string to get_entity
                #   - "123456789" (numeric) → pass as int to get_entity
                #   - If numeric and already in previous_checked_ids → skip network call
                is_numeric_ref = chat_ref.isdigit()

                if is_numeric_ref and chat_ref in previous_checked_ids:
                    # Numeric ID already known — no network call needed.
                    # Update the stored reference to use numeric ID (in case it was an old @username).
                    old_ref = previous_checked_ids[chat_ref]["ref"]
                    if old_ref != chat_ref:
                        logger.warning(
                            "Chat %s: reference updated from '%s' to numeric ID '%s'.",
                            chat_ref, old_ref, chat_ref)
                        previous_checked_ids[chat_ref]["ref"] = chat_ref
                        db_was_updated = True
                    else:
                        logger.debug("Chat '%s' already in database. Skipping.", chat_ref)
                elif chat_ref not in known_usernames_to_ids:
                    logging.info("New or changed username: '%s', use the network to get its ID", chat_ref)
                    # Resolve: digit-only → dialog cache (has access_hash), else → username string
                    if is_numeric_ref:
                        dlg = _lookup_dialog(chat_ref)
                        if dlg is None:
                            raise ValueError(
                                f"Chat ID {chat_ref} not found in dialogs. "
                                f"Account may not be a member, or the ID is wrong."
                            )
                        entity = dlg.entity
                        logger.info("Resolved numeric ID %s via dialog cache → '%s'.", chat_ref, getattr(entity, 'title', chat_ref))
                    else:
                        dlg = None  # no dialog for username-based resolution
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
                    if chat_id_str not in previous_checked_ids:
                        # --- Case A: Brand new chat ---
                        logging.info(f"Adding new chat '{_safe_title(entity)}' ({chat_id_str}) to database.")
                        try:
                            last_msg = await client.get_messages(entity, limit=1)
                        except FloodWaitError as e:
                            # This code runs, but Telethon is *also* sleeping
                            logging.critical(f"We hit a flood wait for {e.seconds} seconds. My bot is too fast!")
                            # No need to add 'await asyncio.sleep(e.seconds)',
                            # Telethon is already doing it.
                        except Exception as e:
                            logging.error(f"A different error: {e}")
                        last_id = last_msg[0].id if last_msg else 0
                        previous_checked_ids[chat_id_str] = {
                            "ref": chat_ref,
                            "last_processed_id": last_id,
                            "alerted_keys": "",
                            "schema_version": 1,
                        }
                    else:
                        # --- Case B: Renamed chat ---
                        old_ref = previous_checked_ids[chat_id_str]["ref"]
                        logging.warning(
                            f"Username for {chat_id_str} changed from '{old_ref}' to '{chat_ref}'. Updating.")
                        previous_checked_ids[chat_id_str]["ref"] = chat_ref  # Update the username
                    db_was_updated = True
                else:
                    logging.debug(f"Chat '{chat_ref}' already in database. Skipping.")
            except Exception as e:
                # This will catch errors from get_entity (e.g., username not found)
                logger.critical("Could not resolve or process %s: %s", chat_ref, e)
                if chat_ref not in _alerted_chats:
                    _alerted_chats.add(chat_ref)
                    await send_health_alert(
                        "Chat resolution failed",
                        f"**Ref:** `{chat_ref}`\n"
                        f"**Error:** {e}\n"
                        f"Chat could not be resolved. It will not be monitored this cycle.",
                        level="error",
                        session=http_session,
                    )

        # 4. Upload to Firebase *ONCE* at the end, only if needed.
        if db_was_updated:
            logging.info("Database was updated, uploading to Firebase...")
            fs.set_firejson(previous_checked_ids, merge=True)
        else:
            logging.debug("No database changes detected.")
        # 5. Poll messages for each known chat (priority chats first, then by
        #    numeric ID smallest first — see _chat_sort_key).
        cursors_to_write: dict[str, int] = {}  # track per-chat new cursor values for cross-contam detection
        priority_refs = _get_priority_chat_refs()
        for name, values in sorted(
            previous_checked_ids.items(), key=lambda kv: _chat_sort_key(kv, priority_refs)
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
            value1 = values["last_processed_id"]

            # ----- Get the lastest message ID in a channel
            # A. Load previous state
            chat_id_str = str(name)
            current_last_message_id = value1 # >> as sample, 34
            logging.debug(f"Scanning '{value0}' (ID: {chat_id_str}) after ID {current_last_message_id}")

            await asyncio.sleep(2)  # Delay for stability

            # B. Resolve entity — try username first, fall back to numeric ID.
            #    This handles groups that changed their @username or went private.
            #    `msg_peer` is the InputPeer used for API calls (needs access_hash).
            msg_peer = None

            def _resolve_by_numeric_id():
                """Return the entity for chat_id_str from the dialog cache.

                ``dlg.entity.id`` is the positive public ID (e.g. 1511100059),
                while ``dialog.id`` is the internal peer ID (-100…).
                """
                dlg = _lookup_dialog(chat_id_str)
                if dlg is None:
                    raise ValueError(
                        f"Chat ID {chat_id_str} not found in dialogs "
                        f"(account may have lost access or chat was deleted)."
                    )
                return dlg.entity

            if value0.isdigit():
                # Steady state for a private group: the stored ref is already a
                # numeric ID, so a username lookup is impossible. Resolve straight
                # from the dialog cache without logging the "username lost"
                # warning over and over — this is the expected condition.
                try:
                    entity = _resolve_by_numeric_id()
                    msg_peer = await client.get_input_entity(entity)
                except Exception as e2:
                    logging.error(
                        f"Cannot resolve chat {chat_id_str} by numeric ID: {e2}. Skipping."
                    )
                    if not _was_alerted(values, "access_lost"):
                        await send_health_alert(
                            "Lost access to chat",
                            f"**Chat ID:** `{chat_id_str}`\n"
                            f"**Last known ref:** `{value0}`\n"
                            f"**Error:** {e2}\n"
                            f"Cannot resolve by username or numeric ID. "
                            f"The account may have lost access or the chat was deleted.",
                            level="error",
                            session=http_session,
                        )
                        _mark_alerted(values, "access_lost")
                        db_was_updated = True
                    continue
            else:
                # Username-based tracking — consult the network.
                try:
                    entity = await client.get_entity(value0)
                    # Guard: if the username now resolves to a *different* entity
                    # (e.g. a User that grabbed the old handle after a group went
                    # private), treat as a resolution failure and fall through to
                    # the numeric-ID fallback below.
                    if str(entity.id) != chat_id_str:
                        raise ValueError(
                            f"Entity ID mismatch for '{value0}': expected {chat_id_str}, "
                            f"got {entity.id} (type={type(entity).__name__}). "
                            f"The username may have been reassigned after the chat went private."
                        )
                    msg_peer = entity  # from network — has access_hash
                except Exception:
                    logging.warning(
                        f"Username '{value0}' not found for chat {chat_id_str} "
                        f"(group may have lost its public username or gone private). "
                        f"Trying by numeric ID..."
                    )
                    try:
                        entity = _resolve_by_numeric_id()
                        msg_peer = await client.get_input_entity(entity)
                    except Exception as e2:
                        logging.error(
                            f"Cannot resolve chat {chat_id_str} by numeric ID either: {e2}. Skipping."
                        )
                        if not _was_alerted(values, "access_lost"):
                            await send_health_alert(
                                "Lost access to chat",
                                f"**Chat ID:** `{chat_id_str}`\n"
                                f"**Last known ref:** `{value0}`\n"
                                f"**Error:** {e2}\n"
                                f"Cannot resolve by username or numeric ID. "
                                f"The account may have lost access or the chat was deleted.",
                                level="error",
                                session=http_session,
                            )
                            _mark_alerted(values, "access_lost")
                            db_was_updated = True
                        continue
                    # Update stored reference: new username if available, else use numeric ID
                    new_username = getattr(entity, 'username', None)
                    if new_username:
                        new_ref = f"@{new_username}"
                        logging.info(
                            f"Chat {chat_id_str} renamed from '{value0}' to '{new_ref}'. Updating cursor."
                        )
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
                        db_was_updated = True
                    db_was_updated = True

            # C. Get actual messages — fetches only messages newer than the cursor.
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
                _save_cursor_sync(fs, previous_checked_ids)
                await http_session.close()
                await client.disconnect()
                return
            except Exception as e:
                logging.error(f"Failed to fetch messages for '{value0}' (chat {chat_id_str}): {e}")
                continue  # skip this chat on error (e.g., bot account can't use GetHistoryRequest)
            db_was_updated = True # need to save our state

            newest_message_id = messages[0].id

            last_acked_id = current_last_message_id  # fallback: don't advance cursor if no messages processed

            # Track overall date range across all chats
            if messages:
                if first_msg_date is None or messages[-1].date < first_msg_date:
                    first_msg_date = messages[-1].date  # oldest
                if last_msg_date is None or messages[0].date > last_msg_date:
                    last_msg_date = messages[0].date      # newest

            for message in reversed(messages):
                count_processed += 1
                message_text = message.text

                if message_text:
                    found_keywords = _find_matching_keywords(message_text, KEYWORDS_LIST)

                    logging.debug(f"Message ID {message.id}: {repr(message_text[:50])}...")
                    logging.debug(f"Matched keywords: {found_keywords}")

                    if found_keywords:
                        count_matches += 1
                        # Save full message to Firestore (independent of alert success)
                        try:
                            await save_matched_message_to_firestore(message, found_keywords)
                        except Exception as fs_e:
                            logging.error("Failed to save message %s to Firestore: %s", message.id, fs_e)
                        try:
                            await send_alert(message, found_keywords, session=http_session)
                            logging.info(f"Alarm sent successfully for message ID: {message.id}")
                            count_alerts += 1
                            last_acked_id = message.id  # only advance cursor on success
                        except Exception as alert_e:
                            logging.error(f"❌ ERROR sending alert for Message ID {message.id}: {alert_e}. "
                                          f"Cursor will NOT advance past this message — will retry on next run.")
                            # Stop processing this chat immediately so the cursor
                            # stays at the last successfully-acked message.
                            # A later message in this batch must not advance the
                            # cursor past a failed one.
                            break
                else:
                    logging.debug(f"Message ID {message.id}: (Non-text message)")

                # For non-keyword messages, advance cursor normally
                if not message_text or not found_keywords:
                    last_acked_id = message.id

            # --- Cursor sanity guards ---
            if last_acked_id > newest_message_id:
                logger.critical(
                    "CURSOR GUARD: computed cursor %d exceeds newest message %d "
                    "for chat '%s' (%s). Refusing to advance — keeping %d.",
                    last_acked_id, newest_message_id, value0, name, current_last_message_id
                )
                try:
                    await send_bot_notification(
                        f"⚠️ **CURSOR GUARD TRIGGERED**\n"
                        f"Chat: `{value0}` (`{name}`)\n"
                        f"Computed cursor ({last_acked_id}) > newest message ({newest_message_id})\n"
                        f"Cursor NOT advanced — kept at {current_last_message_id}",
                        session=http_session,
                    )
                except Exception as alert_e:
                    logger.error("Failed to send cursor guard alert: %s", alert_e)
                last_acked_id = current_last_message_id

            if last_acked_id < current_last_message_id:
                logger.warning(
                    "CURSOR GUARD: computed cursor %d is LESS than current cursor %d "
                    "for chat '%s' (%s). May indicate cross-contamination. Keeping current cursor.",
                    last_acked_id, current_last_message_id, value0, name
                )
                last_acked_id = current_last_message_id

            previous_checked_ids[name]["last_processed_id"] = last_acked_id
            cursors_to_write[name] = last_acked_id

            # --- Save cursor after EACH chat (incremental persistence) ---
            _save_cursor_sync(fs, previous_checked_ids)

            # --- If shutdown triggered mid-chat, stop processing further chats ---
            should_stop, stop_reason = _should_stop(shutdown_event, deadline)
            if should_stop:
                logger.warning("🛑 Shutdown (%s) mid-chat '%s'. Last acked message ID: %d.",
                               stop_reason, value0, last_acked_id)
                break
        date_range = ""
        if first_msg_date and last_msg_date:
            date_range = f" | Range: {first_msg_date.strftime('%d.%m.%Y')} → {last_msg_date.strftime('%d.%m.%Y')}"
        logging.info(f"--- Stats: Processed= {count_processed} | Matches= {count_matches} | Alerts= {count_alerts}{date_range} ---")

        # --- Cross-contamination detection before saving ---
        if db_was_updated and cursors_to_write:
            cursor_to_chats: dict[int, list[str]] = {}
            for chat_id, cursor_val in cursors_to_write.items():
                cursor_to_chats.setdefault(cursor_val, []).append(chat_id)

            duplicate_cursors = {
                val: chats for val, chats in cursor_to_chats.items() if len(chats) > 1
            }
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
                db_was_updated = False  # abort the save

        # --- Backup cursor_base before writing ---
        if db_was_updated:
            logger.info("Creating backup of cursor_base before saving...")
            try:
                fs.backup_document()
                fs.prune_old_backups(max_backups=30)
            except Exception as e:
                logger.error("Backup failed (non-fatal): %s. Proceeding with save.", e)

        # Save to Firebase *only* if there were changes
        if db_was_updated:
            fs.set_firejson(previous_checked_ids, merge=True)
            logging.debug(f"Saved previous_checked_ids to Firebase...: {previous_checked_ids}")

        # --- Final summary ---
        should_stop, stop_reason = _should_stop(shutdown_event, deadline)
        if should_stop:
            logger.warning("🛑 Shutdown (%s) — final state saved.", stop_reason)
        else:
            logger.info("✅ Polling completed normally — all chats processed.")
        await http_session.close()
        await client.disconnect()
