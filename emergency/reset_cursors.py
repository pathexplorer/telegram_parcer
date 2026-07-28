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


async def _resolve_entity(client: TelegramClient, chat_ref: str) -> Tuple[object, str]:
    """
    Resolve a chat entity.
    Returns (entity, chat_id_str).
    Falls back to numeric ID if username lookup fails.
    """
    try:
        entity = await client.get_entity(chat_ref)
    except ValueError:
        logger.warning("Username '%s' not found, trying numeric ID.", chat_ref)
        try:
            entity = await client.get_entity(int(chat_ref))
        except Exception as e2:
            raise ValueError(f"Cannot resolve '{chat_ref}' by any method: {e2}") from e2
    return entity, str(entity.id)


async def _get_latest_message_id(client: TelegramClient, entity: object) -> int:
    """Return the latest message ID for an entity, or 0 if empty."""
    try:
        msgs = await client.get_messages(entity, limit=1)
    except FloodWaitError as e:
        logger.warning("FloodWait %ds for %s — skipping.", e.seconds, getattr(entity, 'title', entity))
        return -1  # sentinel: couldn't fetch
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

        # --- 4. Scan every chat --------------------------------------------
        results: List[Dict] = []  # list of per-chat rows

        for chat_ref in TARGET_CHATS_LIST:
            logger.info("Scanning chat: %s", chat_ref)

            # Resolve entity
            try:
                entity, chat_id_str = await _resolve_entity(client, chat_ref)
            except Exception as e:
                logger.error("❌ Could not resolve '%s': %s", chat_ref, e)
                results.append({
                    "ref": chat_ref,
                    "id": "?",
                    "title": "?",
                    "old_cursor": "?",
                    "latest": "?",
                    "gap": f"❌ resolve error: {e}",
                })
                continue

            title = getattr(entity, 'title', chat_ref)
            # Current stored cursor (may not exist yet)
            old_entry = previous_checked_ids.get(chat_id_str)
            old_cursor = old_entry[1] if old_entry else 0
            old_ref = old_entry[0] if old_entry else "—"

            # Latest message on Telegram
            latest_id = await _get_latest_message_id(client, entity)

            if latest_id == -1:
                results.append({
                    "ref": chat_ref,
                    "id": chat_id_str,
                    "title": title,
                    "old_cursor": old_cursor,
                    "latest": "FloodWait",
                    "gap": "⏳ skipped (rate-limit)",
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
                # payload for later update
                "_chat_id_str": chat_id_str,
                "_chat_ref": chat_ref,
                "_latest_id": latest_id,
            })

            # Small delay between chats to be gentle on the API
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
