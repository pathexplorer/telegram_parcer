"""Telegram polling pipeline — thin orchestrator.

``poll_telegram`` ties the three pipeline phases together.  The phase bodies
live in :mod:`telegram.polling` so this module stays readable:

   PHASE 0 — Setup & validation
       Counters, Firestore cursor client, runtime deadline, Telethon client,
       shared aiohttp session, and the dialog cache used to resolve chats by
       numeric ID (private groups need the access_hash that dialogs carry).

   PHASE 1 — Chat registration        → telegram.polling._register_chats
   PHASE 2 — Message polling (per chat) → telegram.polling._poll_all_chats
   PHASE 3 — Finalization              → telegram.polling._finalize

Cross-cutting concerns live in sibling modules:
   * telegram.helpers     — stateless utilities (matching, sorting, shutdown)
   * telegram.cursor      — cursor-entry structure + safety guards
   * telegram.resolution  — Telethon entity/dialog resolution
"""

import logging

import aiohttp
from telethon import TelegramClient
from telethon.sessions import StringSession
from gcp_actions.firestore_box.json_manipulations import FirestoreMagic
from telegram.polling import (
    _PollStats,
    _StopPolling,
    _finalize,
    _poll_all_chats,
    _register_chats,
    _setup_deadline,
)
from telegram.resolution import _build_dialog_cache
from telegram.helpers import _should_stop
from project_env.config import session_string, API_ID, API_HASH

logger = logging.getLogger(__name__)


async def poll_telegram(KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids,
                        shutdown_event=None, max_runtime_seconds=None):
    """Poll Telegram chats for keyword matches, delivering alerts and advancing cursors.

    Args:
        KEYWORDS_LIST: Normalized keywords to match against message text.
        TARGET_CHATS_LIST: Chat refs to monitor ("@username" or numeric ID).
        previous_checked_ids: cursor_base state ({chat_id: cursor_entry}).
            Mutated in place and persisted back to Firestore.
        known_usernames_to_ids: {ref: chat_id} lookup built at config load.
        shutdown_event: Optional threading.Event set on SIGINT/SIGTERM.
        max_runtime_seconds: Optional hard deadline for the whole poll cycle.
    """
    # =====================================================================
    # PHASE 0 — Setup & validation
    # =====================================================================
    stats = _PollStats()

    # --- Guard: validate inputs before entering the loop ---
    if not TARGET_CHATS_LIST:
        logger.critical("FATAL: TARGET_CHATS_LIST is empty. Nothing to scan.")
        return
    if not KEYWORDS_LIST:
        logger.critical("FATAL: KEYWORDS_LIST is empty. Nothing to match.")
        return

    # --- Initialize Firestore client for cursor_base ---
    fs = FirestoreMagic("telegram", "cursor_base")

    # --- Time limit setup ---
    deadline = _setup_deadline(max_runtime_seconds)

    async with TelegramClient(StringSession(session_string), API_ID, API_HASH, flood_sleep_threshold=60) as client:
        await client.start()

        # --- Create shared HTTP session for this poll cycle -------------------
        # One session reused for all Bot API calls — avoids per-call connection
        # setup and provides a uniform timeout.
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            # --- Build dialog cache for numeric-ID fallback resolution ---
            dialog_cache = await _build_dialog_cache(client)

            # =============================================================
            # PHASE 1 — Chat registration
            # =============================================================
            await _register_chats(
                client, dialog_cache, TARGET_CHATS_LIST, known_usernames_to_ids,
                previous_checked_ids, http_session, shutdown_event, deadline, fs, stats,
            )

            # =============================================================
            # PHASE 2 — Message polling (per chat)
            # =============================================================
            cursors_to_write = await _poll_all_chats(
                client, dialog_cache, previous_checked_ids, KEYWORDS_LIST,
                http_session, fs, shutdown_event, deadline, stats,
            )

            # =============================================================
            # PHASE 3 — Finalization
            # =============================================================
            await _finalize(cursors_to_write, previous_checked_ids, http_session, fs, stats)
        except _StopPolling:
            # Early exit (shutdown signal / flood wait) — partial state was
            # already persisted by the phase that raised.
            pass

        # --- Stats summary ---
        date_range = ""
        if stats.first_msg_date and stats.last_msg_date:
            date_range = f" | Range: {stats.first_msg_date.strftime('%d.%m.%Y')} → {stats.last_msg_date.strftime('%d.%m.%Y')}"
        logging.info(
            f"--- Stats: Processed= {stats.processed} | Matches= {stats.matches} | "
            f"Alerts= {stats.alerts}{date_range} ---"
        )

        # --- Final summary ---
        should_stop, stop_reason = _should_stop(shutdown_event, deadline)
        if should_stop:
            logger.warning("🛑 Shutdown (%s) — final state saved.", stop_reason)
        else:
            logger.info("✅ Polling completed normally — all chats processed.")

        await http_session.close()
