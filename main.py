import asyncio
import sys
import logging
from gcp_actions.common_utils.handle_logs import run_handle_logs
from gcp_actions.common_utils.init_config import InjectConfig
from telegram.starter_conf import forming_configuration

run_handle_logs()
logger = logging.getLogger(__name__)

try:
    list_of_secret_env_vars = ["TELEGRAM_SECRETS"]
    list_of_sa_env_vars = [None]
    InjectConfig(list_of_secret_env_vars, list_of_sa_env_vars, False).load_and_inject_config()
    logger.debug("Configuration loaded successfully.")
except Exception as e:
    logger.critical(f"FATAL ERROR: Could not load configuration. {e}")
    sys.exit(1)

# Loaded words and chats configuration at once
try:
    KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids = forming_configuration()
except Exception as e:
    logger.error(f"Could not load keywords and chats. {e}")

def main(request = None):
    from telegram.listener import poll_telegram

    # Inject previously loaded data to lister during it runs
    asyncio.run(poll_telegram(KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids))
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
