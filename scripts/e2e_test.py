#!/usr/bin/env python3
"""End-to-end smoke test for the Telegram parser pipeline.

Sends a keyword-bearing test message to a target chat, forces the deployed
poller to run (Cloud Scheduler job or the function directly), then watches the
notification chat until the alert arrives — validating the full pipeline:

    Telegram message  →  Firestore config (keyword match)
                     →  archive to ``matched_messages``
                     →  Bot API alert to the notification chat

The test message embeds a unique marker (no Markdown metacharacters) so the
alert can be matched reliably:

    "This my specialtestphrase E2E20260814T081530Z"

Usage:
    python3 -m scripts.e2e_test                       # defaults below
    python3 -m scripts.e2e_test --chat @greenfield9000
    python3 -m scripts.e2e_test --trigger function     # call the CF directly
    python3 -m scripts.e2e_test --timeout 240
    python3 -m scripts.e2e_test --keyword some_phrase  # must already be in config
    python3 -m scripts.e2e_test --dry-run              # send only, no trigger
    python3 -m scripts.e2e_test --keep-message         # don't delete test msg

Requires gcloud auth (ADC) and keys.env with GCP_PROJECT_ID + NOTIFICATION_CHAT.
Exit code 0 = PASS (alert received), 1 = FAIL (timeout/error).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import subprocess
import sys
import time
from typing import Any

from gcp_actions.common_utils.handle_logs import run_handle_logs
from gcp_actions.common_utils.init_config import InjectConfig
from gcp_actions.firestore_box.json_manipulations import FirestoreMagic

run_handle_logs()
logger = logging.getLogger(__name__)

DEFAULT_CHAT = "@greenfield9000"
DEFAULT_KEYWORD = "specialtestphrase"
DEFAULT_TIMEOUT_S = 180
POLL_INTERVAL_S = 5

# Deployment identifiers (mirrors start.yaml / run_local.sh).
DEFAULT_REGION = "us-central1"
DEFAULT_FUNCTION = "telegramPoller"
DEFAULT_SCHEDULER_JOB = "telegram-poll-job"

ALERT_MARKER = "KEYWORD ALERT"


# ---------------------------------------------------------------------------
# Config / secrets
# ---------------------------------------------------------------------------

def _inject_secrets() -> None:
    required = ["API_ID", "API_HASH", "session_string", "NOTIFICATION_CHAT"]
    if all(os.environ.get(k) for k in required):
        return
    try:
        InjectConfig(["TELEGRAM_SECRETS"], [None], False).load_and_inject_config()
    except Exception as exc:
        raise SystemExit(f"❌ Could not inject secrets: {exc}") from exc
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"❌ Missing env vars after injection: {missing}")


def _ensure_keyword_configured(keyword: str) -> None:
    """Fail fast if the keyword is not in the Firestore 'word' array."""
    doc = FirestoreMagic("telegram", "keywords").load_firejson() or {}
    words = doc.get("word", [])
    if isinstance(words, str):
        words = [w.strip() for w in words.split(",") if w.strip()]
    if keyword.casefold() not in {w.casefold() for w in words}:
        raise SystemExit(
            f"❌ Keyword '{keyword}' is not in the config. Add it first:\n"
            f"   ./scripts/manage_config.sh add-keywords \"{keyword}\""
        )


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

async def _send_test_message(client: Any, chat_ref: str, text: str) -> int:
    entity = await client.get_entity(chat_ref)
    sent = await client.send_message(entity, text)
    logger.info("📤 Sent test message to '%s' (id=%s): %s",
                chat_ref, sent.id, text)
    return int(sent.id)


async def _find_alert(client: Any, marker: str) -> bool:
    """Return True if an alert containing the marker is in the notification chat."""
    # Telethon resolves a numeric chat ID only as an int (a str of digits is
    # treated as a username).
    notif_chat = int(os.environ["NOTIFICATION_CHAT"])
    msgs = await client.get_messages(notif_chat, limit=20)
    for m in msgs:
        text = m.text or ""
        if ALERT_MARKER in text and marker in text:
            logger.info("🎯 Alert found (msg id=%s): %s",
                        m.id, text[:120].replace("\n", " "))
            return True
    return False


def _archive_verified(chat_id: str, sent_msg_id: int, marker: str) -> bool:
    """Check the matched message was archived to Firestore."""
    try:
        doc = FirestoreMagic("matched_messages", f"{chat_id}_{sent_msg_id}").load_firejson()
    except Exception as exc:
        logger.warning("Archive check failed (non-fatal): %s", exc)
        return False
    if not doc:
        return False
    text = doc.get("message", "") or ""
    return marker in text


def _run_trigger(trigger: str) -> subprocess.Popen:
    """Start the poller invocation. Returns the (possibly running) process."""
    project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("PROJECT_ID") or ""
    if not project:
        raise SystemExit("❌ GCP_PROJECT_ID not set (source keys.env).")

    if trigger == "scheduler":
        cmd = [
            "gcloud", "scheduler", "jobs", "run", DEFAULT_SCHEDULER_JOB,
            f"--location={DEFAULT_REGION}", f"--project={project}", "--quiet",
        ]
        log = f"scheduler job '{DEFAULT_SCHEDULER_JOB}'"
    else:
        cmd = [
            "gcloud", "functions", "call", DEFAULT_FUNCTION,
            f"--region={DEFAULT_REGION}", f"--project={project}",
            "--gen2", "--quiet",
        ]
        log = f"function '{DEFAULT_FUNCTION}'"

    logger.info("⏱️  Triggering %s...", log)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="e2e_test",
        description="Send a keyword test message and verify the alert arrives.",
    )
    parser.add_argument("--chat", default=DEFAULT_CHAT,
                        help=f"Chat to post the test message to (default: {DEFAULT_CHAT}).")
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD,
                        help=f"Keyword to use (must be configured; default: {DEFAULT_KEYWORD}).")
    parser.add_argument("--trigger", choices=["scheduler", "function"], default="scheduler",
                        help="How to force the poller (default: scheduler).")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                        help=f"Max seconds to wait for the alert (default: {DEFAULT_TIMEOUT_S}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Send the test message only; do NOT trigger the poller.")
    parser.add_argument("--keep-message", action="store_true",
                        help="Do not delete the test message afterwards.")
    args = parser.parse_args()

    _inject_secrets()
    _ensure_keyword_configured(args.keyword)

    marker = f"E2E{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}Z"
    test_text = f"This my {args.keyword} {marker}"
    print(f"🧪 E2E test marker: {marker}")
    print(f"   Keyword: {args.keyword} | Chat: {args.chat} | Trigger: {args.trigger}")

    async def _run() -> int:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from project_env.config import API_HASH, API_ID, session_string

        async with TelegramClient(
            StringSession(session_string), API_ID, API_HASH, flood_sleep_threshold=60
        ) as client:
            await client.start()

            # --- 1. Send the test message -------------------------------
            sent_id = await _send_test_message(client, args.chat, test_text)
            chat_entity = await client.get_entity(args.chat)
            chat_id = str(chat_entity.id)

            if args.dry_run:
                print("🏁  DRY-RUN: message sent, poller NOT triggered.")
                return 0

            # --- 2. Force the poller to run -----------------------------
            proc = _run_trigger(args.trigger)

            # --- 3. Wait for the alert -----------------------------------
            deadline = time.monotonic() + args.timeout
            archived = False
            found = False
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL_INTERVAL_S)
                try:
                    if await _find_alert(client, marker):
                        found = True
                        archived = _archive_verified(chat_id, sent_id, marker)
                        break
                except Exception as exc:
                    logger.warning("Alert poll error (retrying): %s", exc)
                elapsed = int(time.monotonic() - (deadline - args.timeout))
                print(f"   ⏳ {elapsed}s elapsed, still waiting for the alert...")

            # --- 4. Report -----------------------------------------------
            proc.poll()
            if found:
                print("=" * 70)
                print("  ✅ E2E TEST PASSED — alert received in the notification chat.")
                print(f"     Archive to Firestore 'matched_messages': "
                      f"{'✅ verified' if archived else '⚠️ not found (check logs)'}")
                print("=" * 70)
                rc = 0
            else:
                print("=" * 70)
                print(f"  ❌ E2E TEST FAILED — no alert after {args.timeout}s.")
                print("     Check the poller logs:")
                print(f"       gcloud functions logs read {DEFAULT_FUNCTION} "
                      f"--region={DEFAULT_REGION} --limit=30")
                print("=" * 70)
                rc = 1

            # --- 5. Cleanup (best-effort) ---------------------------------
            if not args.keep_message:
                try:
                    await client.delete_messages(chat_entity, [sent_id], revoke=True)
                    print("   🧹 Test message deleted.")
                except Exception as exc:
                    logger.warning("Could not delete test message: %s", exc)
            return rc

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
