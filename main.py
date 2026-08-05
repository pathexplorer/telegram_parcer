"""Telegram Keyword Monitor — Google Cloud Function (Gen 1) entry point.

Deployed as an HTTP-triggered Cloud Function, invoked by Cloud Scheduler
with an OIDC token.  Performs incremental polling of public Telegram channels,
keyword matching, and Bot API alert delivery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from gcp_actions.common_utils.handle_logs import run_handle_logs
from gcp_actions.common_utils.init_config import InjectConfig
from telegram.starter_conf import forming_configuration

run_handle_logs()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Max invocation duration for Gen 1 HTTP Cloud Function is 540 s.
# We budget 450 s for polling and leave ~90 s for shutdown / cursor flush.
_DEFAULT_MAX_POLL_SECONDS = 450

# How long to reuse Firestore configuration before refreshing (seconds).
_CONFIG_TTL_SECONDS = 60

# Minimal set of env vars that MUST be present for the function to work.
_REQUIRED_ENV_VARS: dict[str, str] = {
    "API_ID": "Telegram API ID",
    "API_HASH": "Telegram API Hash",
    "session_string": "Telegram session string",
    "NOTIFICATION_CHAT": "Target chat ID for alerts",
}

# ---------------------------------------------------------------------------
# Per-process config cache (survives warm instances)
# ---------------------------------------------------------------------------
_config_cache: dict[str, Any] | None = None
_config_loaded_at: float = 0.0


def _secrets_loaded() -> bool:
    """Return *True* when all required env vars are present."""
    return all(os.environ.get(k) for k in _REQUIRED_ENV_VARS)


def _inject_secrets() -> None:
    """Load Telegram secrets from Secret Manager into the environment.

    This is called at most once per cold start.  Secrets change very rarely
    (when a session string is rotated), so we keep them cached for the
    lifetime of the warm instance.
    """
    if _secrets_loaded():
        return

    logger.info("🔐 Injecting secrets from Secret Manager…")
    try:
        InjectConfig(
            ["TELEGRAM_SECRETS"], [None], False
        ).load_and_inject_config()
    except Exception as exc:
        logger.critical("FATAL: Could not load secrets from Secret Manager: %s", exc)
        raise RuntimeError("Secret Manager injection failed") from exc

    missing = {k: v for k, v in _REQUIRED_ENV_VARS.items() if not os.environ.get(k)}
    if missing:
        logger.critical(
            "FATAL: Missing required env vars after secret injection: %s",
            {k: v for k, v in missing.items()},
        )
        raise RuntimeError("Required environment variables are missing")

    logger.info("✅ All required secret/env variables loaded.")


def _load_firestore_config() -> tuple[
    list[str], list[str], dict[str, list[Any]], dict[str, str],
]:
    """Load (or refresh) keywords, chats, and cursor state from Firestore.

    Returns:
        (KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids)
    """
    global _config_cache, _config_loaded_at

    now = time.monotonic()
    if _config_cache is not None and (now - _config_loaded_at) < _CONFIG_TTL_SECONDS:
        logger.debug("📦 Using cached Firestore config (age=%.0f s).", now - _config_loaded_at)
        return _config_cache  # type: ignore[return-value]

    logger.info("📡 Loading Firestore configuration…")
    try:
        (
            KEYWORDS_LIST,
            TARGET_CHATS_LIST,
            previous_checked_ids,
            known_usernames_to_ids,
        ) = forming_configuration()
    except Exception as exc:
        logger.critical("FATAL: Could not load Firestore configuration: %s", exc)
        raise RuntimeError("Firestore configuration load failed") from exc

    logger.info(
        "✅ Firestore config loaded: %d keywords, %d chats, %d known IDs.",
        len(KEYWORDS_LIST), len(TARGET_CHATS_LIST), len(known_usernames_to_ids),
    )

    _config_cache = (
        KEYWORDS_LIST,
        TARGET_CHATS_LIST,
        previous_checked_ids,
        known_usernames_to_ids,
    )
    _config_loaded_at = now
    return _config_cache


def _get_max_poll_seconds() -> int:
    """Read MAX_POLL_SECONDS from env, falling back to the default."""
    raw = os.getenv("MAX_POLL_SECONDS")
    if raw is None:
        return _DEFAULT_MAX_POLL_SECONDS
    try:
        val = int(raw)
    except ValueError:
        logger.warning("MAX_POLL_SECONDS=%r is not an integer; using default %d.",
                       raw, _DEFAULT_MAX_POLL_SECONDS)
        return _DEFAULT_MAX_POLL_SECONDS
    if val <= 0:
        logger.warning("MAX_POLL_SECONDS=%d ≤ 0; using default %d.",
                       val, _DEFAULT_MAX_POLL_SECONDS)
        return _DEFAULT_MAX_POLL_SECONDS
    return val


# ---------------------------------------------------------------------------
# Authentication helper
# ---------------------------------------------------------------------------

def _check_auth(request: Any) -> bool:
    """Verify the request is from an authorised caller.

    When the function is deployed **without** ``--allow-unauthenticated``,
    the Cloud Functions / Cloud Run platform validates the OIDC token before
    the request reaches our handler.  This check is a defence-in-depth layer
    that logs and rejects requests missing an ``Authorization`` header.

    Returns:
        *True* when the request appears authenticated (or in local ``__main__``
        mode where the request is a dummy), *False* otherwise.
    """
    if request is None:
        # Local test mode — no platform auth available.
        return True

    # In a properly-configured authenticated deployment the platform injects
    # an Authorization header.  If it's absent something is misconfigured.
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        logger.debug("Request authenticated (Authorization header present).")
        return True

    logger.warning(
        "⚠️  Request missing Authorization header — rejecting. "
        "Ensure the function is NOT deployed with --allow-unauthenticated."
    )
    return False


# ---------------------------------------------------------------------------
# Cloud Function entry point
# ---------------------------------------------------------------------------

def main(request: Any = None) -> tuple[str, int]:
    """Cloud Function HTTP entry point.

    Args:
        request: Flask ``Request`` object injected by the GCF runtime.

    Returns:
        (response_body, http_status_code)
    """
    from telegram.listener import poll_telegram

    # --- Auth gate ---
    if not _check_auth(request):
        return "Unauthorized", 403

    # --- Load / refresh configuration ---
    try:
        _inject_secrets()
        (
            KEYWORDS_LIST,
            TARGET_CHATS_LIST,
            previous_checked_ids,
            known_usernames_to_ids,
        ) = _load_firestore_config()
    except RuntimeError as exc:
        logger.exception("❌ Configuration failure.")
        return f"Configuration error: {exc}", 500

    # --- Determine runtime budget ---
    max_poll_s = _get_max_poll_seconds()
    logger.info("⏱️  Poll budget: %d seconds.", max_poll_s)

    # --- Run polling ---
    try:
        asyncio.run(
            poll_telegram(
                KEYWORDS_LIST,
                TARGET_CHATS_LIST,
                previous_checked_ids,
                known_usernames_to_ids,
                max_runtime_seconds=max_poll_s,
            )
        )
    except Exception:
        logger.exception("❌ Unhandled exception in poll_telegram")
        return "Internal error", 500

    return "Polling complete", 200


# ---------------------------------------------------------------------------
# Local test harness
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from flask import Request

    class _DummyRequest(Request):
        def __init__(self) -> None:
            super().__init__(environ={})

    logging.info("--- Running in local test mode ---")
    dummy = _DummyRequest()
    status, code = main(dummy)
    logging.info("Response (%d): %s", code, status)
    logging.info("--- Local test complete ---")
