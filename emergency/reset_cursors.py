"""
Emergency cursor-reset tool.
Scans every tracked chat, records the latest message ID in each,
shows a diff against the current stored cursor, and — if confirmed —
updates Firestore so future polling starts "from now".

Usage (local):
    python -m emergency.reset_cursors          # interactive (asks y/n)
    python -m emergency.reset_cursors --yes    # non-interactive (auto-confirm)
    python -m emergency.reset_cursors --dry-run  # show diff only, never write
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from gcp_actions.common_utils.handle_logs import run_handle_logs
from gcp_actions.common_utils.init_config import InjectConfig
from gcp_actions.firestore_box.json_manipulations import FirestoreMagic

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
run_handle_logs()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_gap(old: int, new: int) -> str:
    """Human-readable gap string."""
    if old == 0 and new == 0:
        return "— (empty chat)"
    if old == 0:
        return f"⛳ NEW  (0 → {new})"
    diff = new - old
    if diff < 0:
        return f"⚠️  STALE (cursor ahead by {abs(diff)})"
    if diff == 0:
        return "✅ up-to-date"
    return f"📩 +{diff} new"


def _safe_title(entity) -> str:
    """Return a human-readable title for any Telethon entity type."""
    title = getattr(entity, 'title', None)
    if title:
        return title
    first = getattr(entity, 'first_name', None)
    if first:
        last = getattr(entity, 'last_name', None)
        return f"{first} {last}".strip() if last else first
    return str(getattr(entity, 'id', 'Unknown'))


async def _build_dialog_cache(client: TelegramClient) -> dict:
    """Iterate all dialogs and return {dialog_id: Dialog}."""
    cache: dict = {}
    try:
        async for dialog in client.iter_dialogs():
            cache[dialog.id] = dialog
        logger.info("📇 Cached %d dialogs.", len(cache))
    except Exception as exc:
        logger.warning("⚠️  Could not build dialog cache: %s", exc)
    return cache


def _lookup_dialog(numeric_id_str: str, cache: dict):
    """Look up a Dialog in the cache by public numeric ID.

    Telethon entity.id is the positive public ID (e.g. 1511100059),
    dialog.id uses internal peer IDs (e.g. -1001511100059 for supergroups).
    We try all common formats.
    """
    nid = int(numeric_id_str)
    for candidate in (nid, -nid, int(f"-100{numeric_id_str}")):
        dlg = cache.get(candidate)
        if dlg is not None:
            return dlg
    return None


async def _resolve_entity(
    client: TelegramClient,
    chat_ref: str,
    dialog_cache: dict,
    known_usernames_to_ids: dict,
) -> tuple:
    """Resolve a chat entity using the best available method.

    Strategy (in order):
      1. Numeric ref → dialog cache (has access_hash, works for private groups)
      2. Username ref → get_entity, but verify it's a channel/group, not a User
      3. Username ref fails or resolves to User → look up in known_usernames_to_ids
         to find the numeric ID, then try dialog cache
      4. Last resort → get_entity(int(chat_ref))

    Returns (entity, chat_id_str).
    """
    is_numeric = chat_ref.isdigit()

    # --- Path A: numeric ref → dialog cache first ---
    if is_numeric:
        dlg = _lookup_dialog(chat_ref, dialog_cache)
        if dlg is not None:
            entity = dlg.entity
            logger.info("Resolved numeric ID %s via dialog cache → '%s'.",
                        chat_ref, _safe_title(entity))
            return entity, str(entity.id)
        # Fallback: try get_entity (may lack access_hash but worth a shot)
        logger.warning("Numeric ID %s not in dialog cache, trying get_entity…", chat_ref)
        try:
            entity = await client.get_entity(int(chat_ref))
            return entity, str(entity.id)
        except Exception as e:
            raise ValueError(
                f"Cannot resolve numeric ID '{chat_ref}' via dialog cache or get_entity: {e}"
            ) from e

    # --- Path B: username ref ---
    try:
        entity = await client.get_entity(chat_ref)
    except ValueError:
        # Username not found at all — try to find numeric ID from known_usernames_to_ids
        logger.warning("Username '%s' not found, looking up in known_usernames_to_ids…", chat_ref)
        numeric_id = known_usernames_to_ids.get(chat_ref)
        if numeric_id:
            dlg = _lookup_dialog(numeric_id, dialog_cache)
            if dlg is not None:
                entity = dlg.entity
                logger.info("Resolved '%s' → numeric ID %s via dialog cache → '%s'.",
                            chat_ref, numeric_id, _safe_title(entity))
                return entity, str(entity.id)
        raise ValueError(
            f"Username '{chat_ref}' not found and no numeric ID in cursor. "
            f"Account may have lost access."
        )

    # If get_entity succeeded, verify it's a channel/group, not a User that
    # grabbed the old handle after the chat went private.
    if not hasattr(entity, 'title'):
        logger.warning(
            "Username '%s' resolved to a %s (id=%s) instead of a channel/group. "
            "The handle may have been reassigned. Looking up by known numeric ID…",
            chat_ref, type(entity).__name__, entity.id)
        numeric_id = known_usernames_to_ids.get(chat_ref)
        if numeric_id:
            dlg = _lookup_dialog(numeric_id, dialog_cache)
            if dlg is not None:
                entity = dlg.entity
                logger.info("Resolved '%s' → numeric ID %s via dialog cache → '%s'.",
                            chat_ref, numeric_id, _safe_title(entity))
                return entity, str(entity.id)
        raise ValueError(
            f"Username '{chat_ref}' resolved to User (id={entity.id}), "
            f"and no matching cursor entry found for numeric fallback."
        )

    return entity, str(entity.id)


async def _get_latest_message_id(client: TelegramClient, entity: object) -> int:
    """Return the latest message ID for an entity, or 0 if empty.

    Uses get_input_entity(entity) to obtain a proper InputPeer with access_hash,
    which is essential for private groups/channels.
    """
    try:
        msg_peer = await client.get_input_entity(entity)
        msgs = await client.get_messages(msg_peer, limit=1)
    except FloodWaitError as e:
        logger.warning("FloodWait %ds for %s — skipping.",
                       e.seconds, _safe_title(entity))
        return -1  # sentinel: couldn't fetch
    except Exception as e:
        logger.warning("Could not fetch messages for '%s': %s",
                       _safe_title(entity), e)
        return -1
    return msgs[0].id if msgs else 0


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

async def scan_and_optionally_reset(
    *,
    dry_run: bool = False,
    auto_yes: bool = False,
) -> None:
    """Main entry point for the emergency cursor-reset tool."""

    # --- 1. Load secrets & Firestore config ---------------------------------
    try:
        list_of_secret_env_vars = ["TELEGRAM_SECRETS"]
        list_of_sa_env_vars = [None]
        InjectConfig(list_of_secret_env_vars, list_of_sa_env_vars, False).load_and_inject_config()
    except Exception as e:
        logger.critical("FATAL: Could not load configuration. %s", e)
        sys.exit(1)

    REQUIRED_ENV_VARS = {
        "API_ID": "Telegram API ID",
        "API_HASH": "Telegram API Hash",
        "session_string": "Telegram session string",
    }
    missing = {k: v for k, v in REQUIRED_ENV_VARS.items() if not os.environ.get(k)}
    if missing:
        logger.critical("FATAL: Missing env vars: %s", list(missing.keys()))
        sys.exit(1)

    # --- 2. Now that secrets are in env, safe to import Telegram deps ----
    from project_env.config import API_ID, API_HASH, session_string  # noqa: E402
    from telegram.starter_conf import forming_configuration  # noqa: E402

    # --- 3. Load keywords/chats & existing cursors -------------------------
    try:
        _keywords, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids = forming_configuration()
    except Exception as e:
        logger.critical("FATAL: Could not load config from Firestore. %s", e)
        sys.exit(1)

    if not TARGET_CHATS_LIST:
        logger.critical("FATAL: No target chats configured.")
        sys.exit(1)

    fs_cursor = FirestoreMagic("telegram", "cursor_base")

    # --- 3. Connect to Telegram --------------------------------------------
    async with TelegramClient(
        StringSession(session_string), API_ID, API_HASH, flood_sleep_threshold=60
    ) as client:
        await client.start()
        logger.info("✅ Connected to Telegram.")

        # --- 3b. Build dialog cache (essential for private-group resolution) ---
        dialog_cache = await _build_dialog_cache(client)

        # --- 4. Scan every chat --------------------------------------------
        results: List[Dict] = []  # list of per-chat rows

        # Track which chats we've already scanned (by numeric ID) to avoid
        # duplicates when a chat appears both in TARGET_CHATS_LIST (as @username)
        # and in previous_checked_ids (as numeric ID).
        scanned_ids: set = set()

        # --- 4a. Scan cursor entries first (authoritative numeric IDs) -------
        for chat_id_str, values in previous_checked_ids.items():
            if not isinstance(values, (list, tuple)) or len(values) < 2:
                logger.warning("Corrupt cursor entry '%s': %s — skipping.", chat_id_str, values)
                continue
            stored_ref = values[0]
            old_cursor = values[1]

            logger.info("Scanning chat (cursor): ID=%s ref='%s'", chat_id_str, stored_ref)

            # Resolve by numeric ID via dialog cache
            dlg = _lookup_dialog(chat_id_str, dialog_cache)
            if dlg is None:
                # Not in dialog cache — don't mark as scanned so Phase 4b
                # can retry via @username (get_entity) from TARGET_CHATS_LIST.
                logger.warning("Chat ID %s not in dialog cache — will retry via TARGET_CHATS_LIST.", chat_id_str)
                continue

            entity = dlg.entity
            scanned_ids.add(chat_id_str)  # mark successful resolution
            title = _safe_title(entity)

            # Latest message on Telegram (uses get_input_entity for proper access_hash)
            latest_id = await _get_latest_message_id(client, entity)

            if latest_id == -1:
                results.append({
                    "ref": stored_ref,
                    "id": chat_id_str,
                    "title": title,
                    "old_cursor": old_cursor,
                    "latest": "Error/FloodWait",
                    "gap": "⏳ skipped (error or rate-limit)",
                })
                continue

            gap = _fmt_gap(old_cursor, latest_id)
            results.append({
                "ref": stored_ref,
                "id": chat_id_str,
                "title": title,
                "old_cursor": old_cursor,
                "latest": latest_id,
                "gap": gap,
                "_chat_id_str": chat_id_str,
                "_chat_ref": stored_ref,
                "_latest_id": latest_id,
            })
            await asyncio.sleep(1.5)

        # --- 4b. Scan TARGET_CHATS_LIST for NEW chats (not yet in cursor) ---
        for chat_ref in TARGET_CHATS_LIST:
            # Try to resolve and see if it's a new chat we haven't scanned
            try:
                entity, chat_id_str = await _resolve_entity(
                    client, chat_ref, dialog_cache, known_usernames_to_ids
                )
            except Exception as e:
                logger.error("❌ Could not resolve '%s': %s", chat_ref, e)
                # Only report if not already scanned via cursor
                if chat_ref.isdigit() and chat_ref in scanned_ids:
                    continue  # already handled above
                results.append({
                    "ref": chat_ref,
                    "id": "?",
                    "title": "?",
                    "old_cursor": "?",
                    "latest": "?",
                    "gap": f"❌ resolve error: {e}",
                })
                continue

            if chat_id_str in scanned_ids:
                continue  # already handled in cursor scan
            scanned_ids.add(chat_id_str)

            title = _safe_title(entity)
            old_entry = previous_checked_ids.get(chat_id_str)
            old_cursor = old_entry[1] if old_entry else 0

            logger.info("Scanning chat (new): ref='%s' id=%s title='%s'", chat_ref, chat_id_str, title)

            latest_id = await _get_latest_message_id(client, entity)

            if latest_id == -1:
                results.append({
                    "ref": chat_ref,
                    "id": chat_id_str,
                    "title": title,
                    "old_cursor": old_cursor,
                    "latest": "Error/FloodWait",
                    "gap": "⏳ skipped (error or rate-limit)",
                })
                continue

            gap = _fmt_gap(old_cursor, latest_id)
            results.append({
                "ref": chat_ref,
                "id": chat_id_str,
                "title": title,
                "old_cursor": old_cursor,
                "latest": latest_id,
                "gap": gap,
                "_chat_id_str": chat_id_str,
                "_chat_ref": chat_ref,
                "_latest_id": latest_id,
            })
            await asyncio.sleep(1.5)

        # --- 5. Print summary ----------------------------------------------
        print()
        print("=" * 80)
        print("  🔍  EMERGENCY CURSOR SCAN RESULTS")
        print(f"  Scanned at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 80)
        print(f"{'Chat':<30} {'ID':<15} {'Old cursor':>10} {'Latest':>10}  Status")
        print("-" * 80)
        for r in results:
            print(
                f"{r['title'][:28]:<30} "
                f"{r['id']:<15} "
                f"{str(r['old_cursor']):>10} "
                f"{str(r['latest']):>10}  "
                f"{r['gap']}"
            )
        print("-" * 80)

        # Stats
        new_chats = sum(1 for r in results if isinstance(r.get('old_cursor'), int) and r['old_cursor'] == 0 and isinstance(r.get('latest'), int) and r['latest'] > 0)
        behind = sum(1 for r in results if isinstance(r.get('old_cursor'), int) and isinstance(r.get('latest'), int) and r['latest'] > r['old_cursor'])
        up_to_date = sum(1 for r in results if isinstance(r.get('old_cursor'), int) and isinstance(r.get('latest'), int) and r['latest'] == r['old_cursor'])
        errors = len(results) - new_chats - behind - up_to_date

        print(f"  New chats (no cursor): {new_chats}")
        print(f"  Behind (will advance):  {behind}")
        print(f"  Already up-to-date:     {up_to_date}")
        if errors:
            print(f"  Errors/skipped:         {errors}")
        print("=" * 80)

        # --- 6. Decide action ----------------------------------------------
        if dry_run:
            print("\n🏁  DRY-RUN mode — no changes written to Firestore.")
            return

        if auto_yes:
            choice = "y"
            print("\n⚡  --yes flag set — auto-confirming.")
        else:
            print()
            choice = input("❓ Set ALL cursors to latest message IDs? [y/N]: ").strip().lower()

        if choice not in ("y", "yes"):
            print("👋  Aborted. No changes made.")
            return

        # --- 7. Build updated cursor map -----------------------------------
        updated: Dict[str, list] = {}
        for r in results:
            cid = r.get("_chat_id_str")
            if cid is None or cid == "?":
                continue
            latest = r.get("_latest_id")
            if not isinstance(latest, int) or latest <= 0:
                continue
            # Preserve existing entries, update cursor
            old_entry = previous_checked_ids.get(cid)
            ref = r["_chat_ref"]
            updated[cid] = [ref, latest]

        if not updated:
            print("⚠️   Nothing to update (no valid chat data).")
            return

        # --- 8. Write to Firestore -----------------------------------------
        try:
            fs_cursor.set_firejson(updated, merge=True)
            print(f"✅  Firestore 'cursor_base' updated — {len(updated)} chat cursors set to latest.")
        except Exception as e:
            logger.critical("❌  Failed to write to Firestore: %s", e)
            sys.exit(1)

        await client.disconnect()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Emergency cursor reset — catch up all chats to 'now'.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and print results only; do NOT write to Firestore.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt — immediately update cursors.",
    )
    args = parser.parse_args()

    asyncio.run(scan_and_optionally_reset(
        dry_run=args.dry_run,
        auto_yes=args.yes,
    ))


if __name__ == "__main__":
    main()
