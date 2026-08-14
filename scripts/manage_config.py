#!/usr/bin/env python3
"""User-friendly CLI for managing the Telegram parser config in Firestore.

Adds / removes / lists the target chats (``chats`` array) and keywords
(``word`` array) on the ``telegram/keywords`` document, and optionally
pre-provisions the matching ``telegram/cursor_base`` entry so the new chat
is monitored on the very next poll (no 15-minute wait, no manual Firestore
editing mistakes).

Why this exists:
    Manually editing Firestore is error-prone: the service only reads the
    ``chats`` ARRAY (not the legacy ``channels`` string), chats are only
    auto-registered on the next scheduler run, and a username that now
    resolves to a *User* (not a channel/group) is silently skipped.

Usage (local, from the project root):
    python3 -m scripts.manage_config list
    python3 -m scripts.manage_config add-chat "@greenfield9000"
    python3 -m scripts.manage_config add-keywords "urgent, emergency"
    python3 -m scripts.manage_config remove-chat "@old_channel"
    python3 -m scripts.manage_config remove-keywords "obsolete"
    python3 -m scripts.manage_config            # interactive menu

Flags:
    --no-verify   Skip the Telegram channel/group check (faster, offline).
    --dry-run     Show what would change without writing anything.
    --yes         Skip confirmation prompts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from gcp_actions.common_utils.handle_logs import run_handle_logs
from gcp_actions.common_utils.init_config import InjectConfig
from gcp_actions.firestore_box.json_manipulations import FirestoreMagic
from telegram.starter_conf import CURSOR_SCHEMA_VERSION

run_handle_logs()
logger = logging.getLogger(__name__)

KEYWORDS_DOC = FirestoreMagic("telegram", "keywords")
CURSOR_DOC = FirestoreMagic("telegram", "cursor_base")

FIELDS = {
    "chats": "target chats",
    "word": "keywords",
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _normalize_chat(ref: str) -> str:
    """Strip whitespace and ensure a leading '@'."""
    ref = ref.strip()
    if not ref:
        return ""
    if not ref.startswith("@"):
        ref = "@" + ref
    return ref


def _split_phrases(raw: str) -> list[str]:
    """Split comma / whitespace separated phrases, dropping empties."""
    out: list[str] = []
    for chunk in raw.replace(",", " ").split():
        chunk = chunk.strip()
        if chunk and chunk not in out:
            out.append(chunk)
    return out


def _dedupe(items: list[str]) -> list[str]:
    """Dedupe case-insensitively, preserving order and original casing."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _field_list(doc: dict[str, Any], field: str) -> list[str]:
    """Read an array (or legacy comma-string) field as a list of strings."""
    raw = doc.get(field)
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


# ---------------------------------------------------------------------------
# Config loading / secrets
# ---------------------------------------------------------------------------

def _inject_secrets() -> None:
    """Inject Telegram secrets from Secret Manager (best effort, offline-safe)."""
    required = ["API_ID", "API_HASH", "session_string"]
    if all(os.environ.get(k) for k in required):
        return
    try:
        InjectConfig(["TELEGRAM_SECRETS"], [None], False).load_and_inject_config()
    except Exception as exc:
        logger.warning("Could not inject secrets from Secret Manager (%s). "
                       "Continuing without Telegram verification.", exc)


def _secrets_available() -> bool:
    return all(os.environ.get(k) for k in ["API_ID", "API_HASH", "session_string"])


# ---------------------------------------------------------------------------
# Telegram verification
# ---------------------------------------------------------------------------

def _safe_title(entity: Any) -> str:
    """Return a human-readable title for any Telethon entity type."""
    title = getattr(entity, "title", None)
    if title:
        return title
    first = getattr(entity, "first_name", None)
    if first:
        last = getattr(entity, "last_name", None)
        return f"{first} {last}".strip() if last else first
    return str(getattr(entity, "id", "Unknown"))


async def _verify_and_provision_chat(chat_ref: str, from_start: bool = False) -> dict[str, Any] | None:
    """Resolve the chat via Telethon; return cursor entry data or None.

    Returns a dict {"ref", "last_processed_id", ...} to write into
    cursor_base, or *None* if the chat could not be verified.  Raises
    ValueError if the username resolves to a *User* instead of a channel/
    group (the chat is not a valid target and must not be added).

    When *from_start* is True the cursor starts at 0 so the next poll
    scans the whole chat history (which may alert on older messages);
    otherwise it starts at the latest message, scanning only new posts.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from project_env.config import API_HASH, API_ID, session_string

    async with TelegramClient(
        StringSession(session_string), API_ID, API_HASH, flood_sleep_threshold=60
    ) as client:
        await client.start()
        entity = await client.get_entity(chat_ref)

        if not hasattr(entity, "title"):
            raise ValueError(
                f"'{chat_ref}' resolved to a {type(entity).__name__} (id={entity.id}), "
                f"not a channel/group. The handle may have been reassigned after the "
                f"original chat went private — use its numeric ID instead, or drop it."
            )

        chat_id = str(entity.id)
        if from_start:
            last_id = 0
            print(f"   ✅ Verified: '{_safe_title(entity)}' (id={chat_id}) — "
                  f"tracking from message 0 (full history).")
        else:
            last_msg = await client.get_messages(entity, limit=1)
            last_id = last_msg[0].id if last_msg else 0
            print(f"   ✅ Verified: '{_safe_title(entity)}' (id={chat_id}) — "
                  f"tracking from message {last_id}.")
        return {
            "ref": chat_ref,
            "last_processed_id": last_id,
            "alerted_keys": "",
            "schema_version": CURSOR_SCHEMA_VERSION,
        }


def _resolve_chat(chat_ref: str, verify: bool, from_start: bool = False) -> dict[str, Any] | None:
    """Run Telegram verification synchronously (offline-safe when disabled)."""
    if not verify or not _secrets_available():
        return None
    try:
        return asyncio.run(_verify_and_provision_chat(chat_ref, from_start))
    except ValueError as exc:
        raise
    except Exception as exc:
        print(f"   ⚠️  Could not verify '{chat_ref}' via Telegram: {exc}")
        print("   The chat will still be added; the poller will retry resolution next run.")
        return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


def _backup(fs: FirestoreMagic) -> None:
    try:
        fs.backup_document()
    except Exception as exc:
        logger.warning("Backup failed (non-fatal): %s", exc)


def list_config() -> int:
    """Print current chats and keywords."""
    doc = KEYWORDS_DOC.load_firejson() or {}
    chats = _field_list(doc, "chats")
    words = _field_list(doc, "word")

    print("=" * 70)
    print(f"  Target chats ({len(chats)}):")
    for i, c in enumerate(chats, 1):
        print(f"    {i:>3}. {c}")
    print(f"  Keywords ({len(words)}):")
    for i, w in enumerate(words, 1):
        print(f"    {i:>3}. {w}")
    print("=" * 70)

    cursors = CURSOR_DOC.load_firejson() or {}
    registered = {v.get("ref") for v in cursors.values() if isinstance(v, dict)}
    unregistered = [c for c in chats if c not in registered]
    if unregistered:
        print(f"  ⚠️  Not yet registered in cursor_base (added on next poll): "
              f"{', '.join(unregistered)}")
    return 0


def add_chats(refs: list[str], *, verify: bool, dry_run: bool, auto_yes: bool,
              from_start: bool = False) -> int:
    """Add one or more chats to the 'chats' array (and cursor_base)."""
    normalized = _dedupe([_normalize_chat(r) for r in refs])
    normalized = [r for r in normalized if r]
    if not normalized:
        print("❌ No valid chat reference provided.")
        return 1

    doc = KEYWORDS_DOC.load_firejson() or {}
    existing = _field_list(doc, "chats")
    existing_lower = {c.casefold() for c in existing}

    new_refs = [r for r in normalized if r.casefold() not in existing_lower]
    if not new_refs:
        print("ℹ️  All provided chats are already in the config.")
        return 0
    already = [r for r in normalized if r.casefold() in existing_lower]
    if already:
        print(f"ℹ️  Skipping (already present): {', '.join(already)}")

    cursor_entries: dict[str, dict[str, Any]] = {}
    for ref in new_refs:
        print(f"📡 Adding chat: {ref}")
        try:
            entry = _resolve_chat(ref, verify, from_start)
        except ValueError as exc:
            print(f"❌ {exc}")
            print("   Chat NOT added.")
            return 1
        if entry:
            cursor_entries[entry["ref"]] = entry

    if not _confirm(f"Add {len(new_refs)} chat(s) to config?", auto_yes):
        print("👋  Aborted. No changes made.")
        return 0
    if dry_run:
        print(f"🏁  DRY-RUN: would add {new_refs} (and cursor entries "
              f"{list(cursor_entries) or 'none'}).")
        return 0

    updated = existing + [r for r in new_refs]
    _backup(KEYWORDS_DOC)
    KEYWORDS_DOC.set_firejson({"chats": updated}, merge=True)
    if cursor_entries:
        _backup(CURSOR_DOC)
        CURSOR_DOC.set_firejson(cursor_entries, merge=True)
        print(f"✅ Added {len(cursor_entries)} chat(s) to config and provisioned cursor_base.")
    else:
        print("✅ Added to config. Cursor will be provisioned automatically on next poll.")
    return 0


def add_keywords(phrases: list[str], *, dry_run: bool, auto_yes: bool) -> int:
    """Add one or more phrases to the 'word' array."""
    flat = " ".join(phrases)
    incoming = _dedupe(_split_phrases(flat))
    if not incoming:
        print("❌ No valid keywords provided.")
        return 1

    doc = KEYWORDS_DOC.load_firejson() or {}
    existing = _field_list(doc, "word")
    existing_lower = {w.casefold() for w in existing}
    new_words = [w for w in incoming if w.casefold() not in existing_lower]

    if not new_words:
        print("ℹ️  All keywords are already in the config.")
        return 0

    if not _confirm(f"Add {len(new_words)} keyword(s): {', '.join(new_words)}?", auto_yes):
        print("👋  Aborted. No changes made.")
        return 0
    if dry_run:
        print(f"🏁  DRY-RUN: would add keywords {new_words}.")
        return 0

    updated = existing + new_words
    _backup(KEYWORDS_DOC)
    KEYWORDS_DOC.set_firejson({"word": updated}, merge=True)
    print(f"✅ Added {len(new_words)} keyword(s).")
    return 0


def remove_chats(refs: list[str], *, dry_run: bool, auto_yes: bool) -> int:
    """Remove chats from the 'chats' array and their cursor_base entries."""
    normalized = [_normalize_chat(r) for r in refs]
    doc = KEYWORDS_DOC.load_firejson() or {}
    existing = _field_list(doc, "chats")
    existing_lower = {c.casefold() for c in existing}

    to_remove = [r for r in normalized if r.casefold() in existing_lower]
    if not to_remove:
        print("ℹ️  None of the provided chats are in the config.")
        return 0

    print(f"🗑  Removing chats: {', '.join(to_remove)}")
    if not _confirm("Remove these chats (and their cursors)?", auto_yes):
        print("👋  Aborted. No changes made.")
        return 0
    if dry_run:
        print(f"🏁  DRY-RUN: would remove {to_remove} and their cursor entries.")
        return 0

    remove_lower = {r.casefold() for r in to_remove}
    updated = [c for c in existing if c.casefold() not in remove_lower]
    _backup(KEYWORDS_DOC)
    KEYWORDS_DOC.set_firejson({"chats": updated}, merge=True)

    cursors = CURSOR_DOC.load_firejson() or {}
    doomed = [
        cid for cid, v in cursors.items()
        if isinstance(v, dict) and str(v.get("ref", "")).casefold() in remove_lower
    ]
    if doomed:
        _backup(CURSOR_DOC)
        for cid in doomed:
            CURSOR_DOC.delete_field_firejson(cid)
        print(f"✅ Removed chats and {len(doomed)} cursor entry/ies: {', '.join(doomed)}.")
    else:
        print("✅ Removed chats (no cursor entries to delete).")
    return 0


def remove_keywords(phrases: list[str], *, dry_run: bool, auto_yes: bool) -> int:
    """Remove phrases from the 'word' array."""
    flat = " ".join(phrases)
    incoming = _dedupe(_split_phrases(flat))
    doc = KEYWORDS_DOC.load_firejson() or {}
    existing = _field_list(doc, "word")
    existing_lower = {w.casefold() for w in existing}

    to_remove = [w for w in incoming if w.casefold() in existing_lower]
    if not to_remove:
        print("ℹ️  None of the provided keywords are in the config.")
        return 0

    if not _confirm(f"Remove keywords: {', '.join(to_remove)}?", auto_yes):
        print("👋  Aborted. No changes made.")
        return 0
    if dry_run:
        print(f"🏁  DRY-RUN: would remove keywords {to_remove}.")
        return 0

    remove_lower = {w.casefold() for w in to_remove}
    updated = [w for w in existing if w.casefold() not in remove_lower]
    _backup(KEYWORDS_DOC)
    KEYWORDS_DOC.set_firejson({"word": updated}, merge=True)
    print(f"✅ Removed {len(to_remove)} keyword(s).")
    return 0


def reset_chat(ref: str, new_cursor: int = 0, *, dry_run: bool = False,
               auto_yes: bool = False) -> int:
    """Reset the cursor of a single chat so the next poll rescans its history.

    Used to re-scan messages that were skipped when a chat was first
    registered (e.g. a keyphrase posted before registration).
    """
    cursors = CURSOR_DOC.load_firejson() or {}
    target_id: str | None = None
    if ref.isdigit() and ref in cursors:
        target_id = ref
    else:
        norm = _normalize_chat(ref).casefold()
        for cid, v in cursors.items():
            if isinstance(v, dict) and str(v.get("ref", "")).casefold() == norm:
                target_id = cid
                break
    if target_id is None:
        print(f"❌ No cursor entry found for '{ref}'. Is the chat registered yet?")
        return 1

    entry = cursors[target_id]
    if not isinstance(entry, dict):
        print(f"❌ Cursor entry for '{ref}' is malformed: {entry}")
        return 1
    current = entry.get("last_processed_id")
    print(f"🔁 Chat '{entry.get('ref', target_id)}' (id={target_id}): "
          f"cursor {current} → {new_cursor}.")

    if not _confirm("Reset this chat's cursor?", auto_yes):
        print("👋  Aborted. No changes made.")
        return 0
    if dry_run:
        print(f"🏁  DRY-RUN: would set cursor for {target_id} to {new_cursor}.")
        return 0

    entry["last_processed_id"] = new_cursor
    _backup(CURSOR_DOC)
    CURSOR_DOC.set_firejson({target_id: entry}, merge=True)
    print(f"✅ Cursor for {target_id} reset to {new_cursor}. "
          f"Next poll will re-scan its history.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _interactive_menu() -> int:
    """Simple interactive menu for non-CLI users."""
    print("\n Telegram Parser — Config Manager")
    print("  1. List chats & keywords")
    print("  2. Add a chat (@username)")
    print("  3. Add keywords (comma-separated)")
    print("  4. Remove a chat")
    print("  5. Remove keywords")
    print("  6. Exit")
    choice = input("\nChoose an option: ").strip()

    if choice == "1":
        return list_config()
    if choice == "2":
        ref = input("Chat (e.g. @channel or numeric ID): ").strip()
        return add_chats([ref], verify=True, dry_run=False, auto_yes=False)
    if choice == "3":
        raw = input("Keywords (comma-separated): ").strip()
        return add_keywords([raw], dry_run=False, auto_yes=False)
    if choice == "4":
        ref = input("Chat to remove: ").strip()
        return remove_chats([ref], dry_run=False, auto_yes=False)
    if choice == "5":
        raw = input("Keywords to remove (comma-separated): ").strip()
        return remove_keywords([raw], dry_run=False, auto_yes=False)
    print("👋  Bye.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="manage_config",
        description="Manage Telegram parser chats and keywords in Firestore.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show changes, write nothing.")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts.")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip Telegram channel/group verification.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="Show current chats and keywords.")

    p_add_chat = sub.add_parser("add-chat", help="Add chat(s) to the config.")
    p_add_chat.add_argument("refs", nargs="+", help="Chat username(s) or numeric ID(s).")
    p_add_chat.add_argument("--from-start", action="store_true",
                            help="Scan the full chat history from message 0 (may alert "
                                 "on older messages) instead of only new posts.")

    p_add_kw = sub.add_parser("add-keywords", help="Add keyword phrase(s).")
    p_add_kw.add_argument("phrases", nargs="+", help="Phrase(s), comma-separated or space.")

    p_rm_chat = sub.add_parser("remove-chat", help="Remove chat(s) from the config.")
    p_rm_chat.add_argument("refs", nargs="+", help="Chat username(s) or numeric ID(s).")

    p_rm_kw = sub.add_parser("remove-keywords", help="Remove keyword phrase(s).")
    p_rm_kw.add_argument("phrases", nargs="+", help="Phrase(s) to remove.")

    p_reset = sub.add_parser("reset-chat", help="Reset a chat's cursor (re-scan history).")
    p_reset.add_argument("ref", help="Chat username or numeric ID.")
    p_reset.add_argument("--cursor", type=int, default=0,
                         help="New cursor value (default: 0 = from the start).")

    args = parser.parse_args()

    if args.command is None:
        return _interactive_menu()

    _inject_secrets()

    if args.command == "list":
        return list_config()
    if args.command == "add-chat":
        return add_chats(args.refs, verify=not args.no_verify,
                         dry_run=args.dry_run, auto_yes=args.yes,
                         from_start=getattr(args, "from_start", False))
    if args.command == "add-keywords":
        return add_keywords(args.phrases, dry_run=args.dry_run, auto_yes=args.yes)
    if args.command == "remove-chat":
        return remove_chats(args.refs, dry_run=args.dry_run, auto_yes=args.yes)
    if args.command == "remove-keywords":
        return remove_keywords(args.phrases, dry_run=args.dry_run, auto_yes=args.yes)
    if args.command == "reset-chat":
        return reset_chat(args.ref, args.cursor,
                          dry_run=args.dry_run, auto_yes=args.yes)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
