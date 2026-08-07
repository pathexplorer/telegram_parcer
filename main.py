"""Telegram Keyword Monitor — Google Cloud Function (Gen 1) entry point.

Deployed as an HTTP-triggered Cloud Function, invoked by Cloud Scheduler
with an OIDC token.  Performs incremental polling of public Telegram channels,
keyword matching, and Bot API alert delivery.

Query parameters:
    ``?check=1``   — Validate configuration only (secrets + Firestore),
                     skip the full polling loop.
    ``?health=1``  — Full health check: validates config AND checks that
                     the last successful poll heartbeat is recent.
                     Returns 200 if healthy, 500 if unhealthy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from gcp_actions.common_utils.handle_logs import run_handle_logs
from gcp_actions.common_utils.init_config import InjectConfig
from gcp_actions.firestore_box.json_manipulations import FirestoreMagic
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

# Max age of the last-success heartbeat before the function is considered
# unhealthy.  Should be > Cloud Scheduler interval (e.g. 2× the interval).
# Default: 7200 s = 2 hours (generous, adjust to your scheduler cadence).
_DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 7200

# Firestore collection / document where the heartbeat is stored.
_HEARTBEAT_COLLECTION = "telegram"
_HEARTBEAT_DOCUMENT = "heartbeat"

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


def _get_heartbeat_max_age() -> int:
    """Read HEARTBEAT_MAX_AGE_SECONDS from env, falling back to the default."""
    raw = os.getenv("HEARTBEAT_MAX_AGE_SECONDS")
    if raw is None:
        return _DEFAULT_HEARTBEAT_MAX_AGE_SECONDS
    try:
        val = int(raw)
    except ValueError:
        logger.warning("HEARTBEAT_MAX_AGE_SECONDS=%r invalid; using default %d.",
                       raw, _DEFAULT_HEARTBEAT_MAX_AGE_SECONDS)
        return _DEFAULT_HEARTBEAT_MAX_AGE_SECONDS
    if val <= 0:
        return _DEFAULT_HEARTBEAT_MAX_AGE_SECONDS
    return val


# ---------------------------------------------------------------------------
# Heartbeat — independent "dead man's switch" for external monitoring
# ---------------------------------------------------------------------------

def _write_heartbeat(success: bool, *, detail: str = "") -> None:
    """Write a heartbeat document to Firestore.

    This is an **independent** health signal.  Even when the Bot API is
    unreachable (so Telegram alerts cannot be sent), an external monitor
    can read this Firestore document to detect failures.

    Args:
        success: *True* for a successful poll, *False* for a failure.
        detail: Optional error description (only meaningful when
                *success* is *False*).
    """
    try:
        fs = FirestoreMagic(_HEARTBEAT_COLLECTION, _HEARTBEAT_DOCUMENT)
        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        heartbeat: dict[str, Any] = {
            "code_version": os.getenv("CODE_VERSION", "unknown"),
            "function_name": os.getenv("K_SERVICE", "local"),
        }
        if success:
            heartbeat["last_success_ts"] = now
            heartbeat["last_success_date"] = now_iso
        else:
            heartbeat["last_failure_ts"] = now
            heartbeat["last_failure_date"] = now_iso
            heartbeat["last_failure_detail"] = detail
        fs.set_firejson(heartbeat, merge=True)
        if success:
            logger.info("💓 Heartbeat written: success at %s", now_iso)
        else:
            logger.warning("💔 Failure heartbeat written: %s", detail)
    except Exception as exc:
        # Heartbeat is best-effort — never let it crash the function.
        logger.error("Failed to write heartbeat to Firestore: %s", exc)


def _read_heartbeat() -> dict[str, Any] | None:
    """Read the current heartbeat document from Firestore.

    Returns:
        The heartbeat dict, or *None* if the document does not exist
        or Firestore is unreachable.
    """
    try:
        fs = FirestoreMagic(_HEARTBEAT_COLLECTION, _HEARTBEAT_DOCUMENT)
        return fs.load_firejson()
    except Exception as exc:
        logger.warning("Could not read heartbeat from Firestore: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Authentication helper
# ---------------------------------------------------------------------------

def _check_auth(request: Any) -> bool:
    """Verify the request is from an authorised caller.

    When deployed without ``--allow-unauthenticated``, Cloud Functions
    validates the OIDC token at the platform level **before** the request
    reaches our handler.  The ``Authorization`` header is not forwarded to
    Flask, so we cannot re-check it here.

    This function therefore trusts the platform: if we are running in a
    Cloud Functions environment (``K_SERVICE`` is set), we assume the
    request was already validated.  In local mode (no ``K_SERVICE``),
    we accept any request — the developer is responsible for security.

    Returns:
        Always *True* in normal operation.
    """
    if request is None:
        return True  # local test mode

    # When K_SERVICE is set we are inside Cloud Functions / Cloud Run.
    # The platform already validated the OIDC token upstream.
    if os.environ.get("K_SERVICE"):
        logger.debug("Request authenticated by platform (K_SERVICE detected).")
        return True

    # Local development — no platform auth available.
    logger.debug("Local request — auth bypassed.")
    return True


# ---------------------------------------------------------------------------
# Cloud Function entry point
# ---------------------------------------------------------------------------

def main(request: Any = None) -> tuple[str, int]:
    """Cloud Function HTTP entry point.

    Args:
        request: Flask ``Request`` object injected by the GCF runtime.

    Query parameters:
        ``?check=1``   — Validate configuration only (secrets + Firestore),
                        skip the full polling loop.  Returns 200 on success,
                        500 on failure.  Used by the post-deploy smoke test.
        ``?health=1``  — Full health check: validates config AND checks that
                        the last successful poll heartbeat is recent (within
                        HEARTBEAT_MAX_AGE_SECONDS).  Returns 200 if healthy,
                        500 if unhealthy.  Use with Cloud Monitoring uptime
                        checks or any external monitoring service.

    Returns:
        (response_body, http_status_code)
    """
    # --- Auth gate ---
    if not _check_auth(request):
        return "Unauthorized", 403

    # --- Detect health-check / config-check mode ---
    is_check = False
    is_health = False
    if request is not None:
        try:
            is_check = request.args.get("check", "") == "1"
            is_health = request.args.get("health", "") == "1"
        except Exception:
            pass  # local dummy request has no .args

    # --- Load secrets FIRST (env vars needed by downstream imports) --------
    try:
        _inject_secrets()
    except RuntimeError as exc:
        logger.critical(
            "STARTUP_FAILURE: Secret injection failed — "
            "function cannot start. Check Secret Manager permissions "
            "and secret name '%s' in project %s.",
            os.getenv("TELEGRAM_SECRETS", "telegram-secrets"),
            os.getenv("GCP_PROJECT_ID", "unknown"),
        )
        logger.exception("❌ Secret injection failed.")
        _write_heartbeat(False, detail=f"Secret injection: {exc}")
        return f"Configuration error: {exc}", 500

    # --- Now safe to import — env vars from secrets are available ----------
    try:
        from telegram.listener import poll_telegram  # noqa: E402
    except Exception as exc:
        logger.critical(
            "STARTUP_FAILURE: Import failed — missing dependency or "
            "broken environment. Check requirements.txt and deployed "
            "package. Error: %s", exc
        )
        logger.exception("❌ Import failed (missing dependency or env var?).")
        _write_heartbeat(False, detail=f"Import error: {exc}")
        return f"Import error: {exc}", 500

    # --- Load Firestore configuration ---
    try:
        (
            KEYWORDS_LIST,
            TARGET_CHATS_LIST,
            previous_checked_ids,
            known_usernames_to_ids,
        ) = _load_firestore_config()
    except RuntimeError as exc:
        logger.critical(
            "STARTUP_FAILURE: Firestore configuration load failed. "
            "Check Firestore document 'config/local/settings/data' "
            "and service account permissions."
        )
        logger.exception("❌ Firestore configuration failure.")
        _write_heartbeat(False, detail=f"Firestore config: {exc}")
        return f"Configuration error: {exc}", 500

    # --- Health-check mode: full health = config + heartbeat age ---
    if is_health:
        heartbeat = _read_heartbeat()
        max_age_s = _get_heartbeat_max_age()

        if heartbeat is None or "last_success_ts" not in heartbeat:
            logger.critical(
                "HEALTH_CHECK_FAILED: No heartbeat found. "
                "The function may never have completed a successful poll, "
                "or Firestore is unreachable."
            )
            return (
                "UNHEALTHY: No heartbeat found — function may never have "
                "completed a successful poll.",
                500,
            )

        last_success_ts = heartbeat["last_success_ts"]
        age_s = time.time() - last_success_ts
        last_success_date = heartbeat.get("last_success_date", "unknown")

        if age_s > max_age_s:
            logger.critical(
                "HEALTH_CHECK_FAILED: Last successful poll was %.0f s ago "
                "(max allowed: %d s). Last success: %s. "
                "The function has been failing silently — check Cloud Logging "
                "for CRITICAL errors.",
                age_s, max_age_s, last_success_date,
            )
            return (
                f"UNHEALTHY: Last successful poll was {age_s:.0f} s ago "
                f"(max: {max_age_s} s). Last success: {last_success_date}.",
                500,
            )

        logger.info(
            "🩺 Health check PASSED — last success %.0f s ago (%s).",
            age_s, last_success_date,
        )
        return (
            f"OK — last success {age_s:.0f} s ago ({last_success_date})",
            200,
        )

    # --- Health-check mode: config loaded → done ---
    if is_check:
        logger.info(
            "🩺 Health check passed — %d keywords, %d chats, %d cursors.",
            len(KEYWORDS_LIST),
            len(TARGET_CHATS_LIST),
            len(previous_checked_ids),
        )
        return ("OK", 200)

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
        logger.critical(
            "RUNTIME_FAILURE: Unhandled exception in poll_telegram. "
            "The polling loop crashed — check the traceback above for "
            "the root cause (e.g. Telegram API error, network issue, "
            "or Firestore write failure)."
        )
        logger.exception("❌ Unhandled exception in poll_telegram")
        _write_heartbeat(False, detail="poll_telegram crashed — see logs")
        return "Internal error", 500

    # --- Success: write heartbeat so external monitors know we're alive ---
    _write_heartbeat(True)
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
