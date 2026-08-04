import asyncio
import os
import sys
import logging
from gcp_actions.common_utils.handle_logs import run_handle_logs
from gcp_actions.common_utils.init_config import InjectConfig
from telegram.starter_conf import forming_configuration

run_handle_logs()
logger = logging.getLogger(__name__)

# --- Step 1: Load secrets & Firestore config into environment ---
try:
    list_of_secret_env_vars = ["TELEGRAM_SECRETS"]
    list_of_sa_env_vars = [None]
    InjectConfig(list_of_secret_env_vars, list_of_sa_env_vars, False).load_and_inject_config()
    logger.debug("Configuration loaded successfully.")
except Exception as e:
    logger.critical(f"FATAL ERROR: Could not load configuration. {e}")
    sys.exit(1)

# --- Step 1b: Validate critical env vars are present ---
REQUIRED_ENV_VARS = {
    "API_ID": "Telegram API ID",
    "API_HASH": "Telegram API Hash",
    "session_string": "Telegram session string",
    "NOTIFICATION_CHAT": "Target chat ID for alerts",
}
missing = {k: v for k, v in REQUIRED_ENV_VARS.items() if not os.environ.get(k)}
if missing:
    logger.critical("FATAL: Missing required environment variables: %s",
                     {k: v for k, v in missing.items()})
    sys.exit(1)
logger.info("✅ All required secret/env variables loaded successfully.")

# --- Step 2: Load keywords and chats from Firestore ---
try:
    KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids = forming_configuration()
    logger.info("✅ Firestore configuration loaded: %d keywords, %d chats, %d known IDs.",
                len(KEYWORDS_LIST), len(TARGET_CHATS_LIST), len(known_usernames_to_ids))
except Exception as e:
    logger.critical(f"FATAL ERROR: Could not load keywords and chats from Firestore. {e}")
    sys.exit(1)

def main(request = None):
    from telegram.listener import poll_telegram

    try:
        # Inject previously loaded data to lister during it runs
        asyncio.run(poll_telegram(KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids))
    except Exception:
        logger.exception("❌ Unhandled exception in poll_telegram")
        return "Internal error", 500
    return "Polling complete", 200

if __name__ == "__main__":
    from flask import Request

    class DummyRequest(Request):
        def __init__(self):
            super().__init__(environ={})

    logging.info("--- Running in local test mode ---")
    dummy = DummyRequest()
    response = main(dummy)
    logging.info(response)
    logging.info("--- Local test complete ---")
