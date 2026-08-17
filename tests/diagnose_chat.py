#!/usr/bin/env python3
"""
Diagnostic tool: test Telegram chat accessibility.

Purpose:
    When a Telegram chat moves from public to private, the main listener may lose
    the ability to parse messages from it.  This script tries every resolution
    strategy in isolation and reports exactly what succeeds and what fails, so
    the root cause can be identified and fixed.

Usage:
    # By username
    uv run python tests/diagnose_chat.py @mobilization_law

    # By numeric ID
    uv run python tests/diagnose_chat.py 1511100059

    # Via environment variable
    TEST_CHAT_REF=@mobilization_law uv run python tests/diagnose_chat.py

Output:
    A step-by-step diagnostic report showing:
      - Session validity
      - Username resolution result
      - Numeric-ID / dialog-cache resolution result
      - Last N messages (if reachable)
      - Summary table with actionable recommendations
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 0.  Logger setup (minimal – no dependency on gcp_actions.handle_logs)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-5s %(message)s",
)
logger = logging.getLogger("diagnose")


# ---------------------------------------------------------------------------
# 1.  Load secrets & config (same path as main.py)
# ---------------------------------------------------------------------------
def _load_config() -> None:
    """Populate os.environ with Telegram secrets from Secret Manager."""
    # --- 0. Auto-load keys.env if present (convenience for local dev) ---
    _keys_env = Path(__file__).resolve().parent.parent / "keys.env"
    if _keys_env.is_file() and not os.environ.get("GCP_PROJECT_ID"):
        from dotenv import load_dotenv
        load_dotenv(_keys_env)
        logger.info("📄 Loaded %s", _keys_env.name)

    from gcp_actions.common_utils.init_config import InjectConfig  # type: ignore[import-untyped]

    try:
        InjectConfig(
            list_of_secret_env_vars=["TELEGRAM_SECRETS"],
            list_of_sa_env_vars=[None],
            from_firestore=False,
        ).load_and_inject_config()
        logger.info("✅ Configuration loaded from Secret Manager.")
    except Exception as exc:
        logger.critical("FATAL: cannot load config: %s", exc)
        sys.exit(1)

    # --- Validate critical env vars ---
    required = {
        "API_ID": "Telegram API ID",
        "API_HASH": "Telegram API Hash",
        "session_string": "Telegram session string",
    }
    missing = {k: v for k, v in required.items() if not os.environ.get(k)}
    if missing:
        logger.critical("FATAL: missing env vars: %s", missing)
        sys.exit(1)


# ---------------------------------------------------------------------------
# 2.  Resolution helpers (mirror listener.py logic, self-contained)
# ---------------------------------------------------------------------------
async def _build_dialog_cache(client) -> dict[int, object]:
    """Iterate all dialogs and return {dialog_id: Dialog}."""
    cache: dict[int, object] = {}
    try:
        async for dialog in client.iter_dialogs():
            cache[dialog.id] = dialog
        logger.info("📇 Cached %d dialogs.", len(cache))
    except Exception as exc:
        logger.warning("⚠️  Could not build dialog cache: %s", exc)
    return cache


def _lookup_dialog(numeric_id_str: str, cache: dict[int, object]) -> object | None:
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


# ---------------------------------------------------------------------------
# 3.  Diagnostic steps
# ---------------------------------------------------------------------------
async def _step_session_check(client) -> dict:
    """Verify the user session is alive and report its identity."""
    try:
        me = await client.get_me()
        return {
            "ok": True,
            "phone": getattr(me, "phone", "?"),
            "first_name": getattr(me, "first_name", "?"),
            "username": getattr(me, "username", None),
            "user_id": me.id,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _step_username_resolution(client, chat_ref: str) -> dict:
    """Try resolving via @username (Telethon get_entity)."""
    if chat_ref.isdigit():
        return {"ok": False, "skipped": True, "reason": "chat_ref is numeric, not a username"}
    try:
        entity = await client.get_entity(chat_ref)
        return {
            "ok": True,
            "entity_id": entity.id,
            "title": getattr(entity, "title", "?"),
            "username": getattr(entity, "username", None),
            "entity_type": type(entity).__name__,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _step_numeric_resolution(
    client, chat_ref: str, cache: dict[int, object]
) -> dict:
    """Try resolving via numeric ID + dialog cache."""
    numeric_id = chat_ref if chat_ref.isdigit() else None

    if numeric_id is None:
        return {"ok": False, "skipped": True, "reason": "chat_ref is not numeric"}

    # 3a — is the chat in our dialog list at all?
    dlg = _lookup_dialog(numeric_id, cache)
    if dlg is None:
        return {
            "ok": False,
            "in_dialog_cache": False,
            "error": (
                f"Chat ID {numeric_id} not found in {len(cache)} dialogs. "
                "Account may not be a member or the ID is wrong."
            ),
        }

    entity = dlg.entity
    base = {
        "in_dialog_cache": True,
        "entity_id": entity.id,
        "title": getattr(entity, "title", "?"),
        "username": getattr(entity, "username", None),
    }

    # 3b — can we get a valid InputPeer (access_hash required)?
    try:
        msg_peer = await client.get_input_entity(entity)
        base["ok"] = True
        base["input_peer_type"] = type(msg_peer).__name__
        base["input_peer"] = msg_peer  # keep for message fetch
        return base
    except Exception as exc:
        base["ok"] = False
        base["error"] = f"get_input_entity failed: {exc}"
        return base


async def _step_fetch_messages(client, msg_peer, limit: int = 10) -> dict:
    """Fetch the last *limit* messages using the resolved InputPeer."""
    try:
        messages = await client.get_messages(msg_peer, limit=limit)
        if not messages:
            return {"ok": True, "count": 0, "note": "no messages returned (empty chat?)"}
        total = getattr(messages, "total", len(messages))
        msg_list = []
        for m in messages:
            msg_list.append({
                "id": m.id,
                "date": m.date.isoformat() if m.date else "?",
                "text": (m.text or "(non-text)")[:120],
            })
        return {"ok": True, "count": len(messages), "total": total, "messages": msg_list}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 4.  Pretty-print helpers
# ---------------------------------------------------------------------------
def _header(text: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def _ok(flag: bool) -> str:
    return "✅" if flag else "❌"


# ---------------------------------------------------------------------------
# 5.  Main entrypoint
# ---------------------------------------------------------------------------
async def diagnose(chat_ref: str) -> int:
    """Run the full diagnostic chain for a single chat reference.

    Returns 0 on success (chat reachable), 1 on failure.
    """
    # Lazy imports so the script starts fast and fails gracefully
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    session_string = os.environ["session_string"]
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]

    # --- Print header ---
    _header(f"Telegram Chat Diagnostic: {chat_ref}")
    print(f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # --- Connect ---
    print("\n⏳ Connecting to Telegram …")
    client = TelegramClient(
        StringSession(session_string), api_id, api_hash, flood_sleep_threshold=30
    )
    try:
        await client.start()
        print("✅ Connected.")
    except Exception as exc:
        print(f"❌ Connection failed: {exc}")
        return 1

    reachable = False  # will flip to True if any resolution + fetch works

    try:
        # ---- Step A: Session check ----
        _header("A. Session Check")
        s = await _step_session_check(client)
        if s["ok"]:
            print(f"  {_ok(True)} Logged in as {s['first_name']} "
                  f"(@{s.get('username') or 'no username'}, +{s.get('phone','?')})")
        else:
            print(f"  {_ok(False)} Session invalid: {s['error']}")
            return 1

        # ---- Step B: Build dialog cache ----
        _header("B. Dialog Cache")
        cache = await _build_dialog_cache(client)
        print(f"  Dialogs available: {len(cache)}")

        # ---- Step C: Username resolution ----
        _header("C. Username Resolution")
        u = await _step_username_resolution(client, chat_ref)
        if u.get("skipped"):
            print(f"  ⏭️  Skipped — '{chat_ref}' is numeric, not a username.")
        elif u["ok"]:
            print(f"  {_ok(True)} Resolved via username.")
            print(f"     Title   : {u['title']}")
            print(f"     @handle : {u.get('username') or '(none — private)'}")
            print(f"     ID      : {u['entity_id']}")
            print(f"     Type    : {u['entity_type']}")
        else:
            print(f"  {_ok(False)} Username resolution FAILED: {u['error']}")

        # ---- Step D: Numeric ID / dialog-cache resolution ----
        _header("D. Numeric-ID Resolution (Dialog Cache)")
        msg_peer = None  # will be set by whichever resolution succeeds
        n = await _step_numeric_resolution(client, chat_ref, cache)
        if n.get("skipped"):
            print(f"  ⏭️  Skipped — '{chat_ref}' is not numeric.")
        elif n["ok"]:
            print(f"  {_ok(True)} Resolved via dialog cache.")
            print(f"     Title   : {n['title']}")
            print(f"     @handle : {n.get('username') or '(none — private)'}")
            print(f"     ID      : {n['entity_id']}")
            print(f"     Peer    : {n['input_peer_type']}")
            msg_peer = n.get("input_peer")
        else:
            print(f"  {_ok(False)} Numeric resolution FAILED.")
            print(f"     In cache: {n.get('in_dialog_cache', '?')}")
            print(f"     Error   : {n.get('error', '?')}")

        # ---- Step E: Fetch last messages ----
        _header("E. Message Fetch (last 10)")
        # Prefer numeric resolution peer; fall back to username entity
        if msg_peer is None and u.get("ok"):
            try:
                # resolve the entity again to get a fresh InputPeer
                entity = await client.get_entity(chat_ref)
                msg_peer = await client.get_input_entity(entity)
            except Exception as exc:
                print(f"  {_ok(False)} Could not get InputPeer from username entity: {exc}")
                msg_peer = None

        if msg_peer is None:
            print(f"  {_ok(False)} No valid peer to fetch messages from.")
        else:
            m = await _step_fetch_messages(client, msg_peer, limit=10)
            if m["ok"]:
                reachable = True
                print(f"  {_ok(True)} Fetched {m['count']} message(s) "
                      f"(total in chat: {m.get('total', '?')}).")
                for msg in m.get("messages", []):
                    print(f"     #{msg['id']:>6}  {msg['date'][:19]}  {msg['text']}")
            else:
                print(f"  {_ok(False)} Message fetch FAILED: {m['error']}")

        # ---- Summary ----
        _header("SUMMARY")
        if reachable:
            print(f"  {_ok(True)} Chat **IS** reachable — the session can read messages.")
            print("  If the main listener fails, the issue may be transient")
            print("  (flood wait, network, stale cursor) rather than a permanent")
            print("  access loss.")
        else:
            print(f"  {_ok(False)} Chat is **NOT** reachable with current session.")
            print("  Possible causes:")
            print("    • User account left or was removed from the chat")
            print("    • Chat was deleted / banned")
            print("    • Session expired (re-run telegram/local/get_session.py)")
            print("    • Chat ID changed (verify the correct numeric ID)")

        return 0 if reachable else 1

    finally:
        await client.disconnect()
        print("\n🔌 Disconnected.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    _load_config()

    chat_ref = (
        sys.argv[1].strip()
        if len(sys.argv) > 1
        else os.environ.get("TEST_CHAT_REF", "").strip()
    )
    if not chat_ref:
        print("Usage: uv run diagnose <@username|numeric_id>", file=sys.stderr)
        print("   or: TEST_CHAT_REF=<ref> uv run diagnose", file=sys.stderr)
        sys.exit(2)

    exit_code = asyncio.run(diagnose(chat_ref))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
