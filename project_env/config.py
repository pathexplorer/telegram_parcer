import os
import logging
logger = logging.getLogger(__name__)

# -------------- Configuration --------------
try:
    API_HASH = os.environ.get("API_HASH")
    API_ID = os.environ.get("API_ID")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
    GCS_CLOUD_PROJECT = os.environ.get("GCS_CLOUD_PROJECT")
    _notif_raw = os.getenv('NOTIFICATION_CHAT')
    if _notif_raw is None:
        raise EnvironmentError("NOTIFICATION_CHAT is not set in environment")
    NOTIFICATION_CHAT = int(_notif_raw)
    TELEGRAM_SECRETS = os.environ.get("TELEGRAM_SECRETS")
    session_string = os.environ.get("session_string")
    CODE_VERSION = os.environ.get('CODE_VERSION')
    #----- Telegram
    TELEGRAM_API_TOKEN = "telegram_api_id"
    TELEGRAM_HASH = "telegram_hash"


except KeyError as e:
    logger.critical(f"FATAL: Missing required environment variable: {e}")
    raise EnvironmentError(f"Configuration missing from environment: {e}")
