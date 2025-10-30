import os
from dotenv import load_dotenv

# Cloud Run assets K_SERVICE. If it is not present, is locally keys.env
IS_LOCAL = os.environ.get("K_SERVICE") is None

if IS_LOCAL: # then load .keys.env file
    dotenv_path = os.path.join(os.path.dirname(__file__), "../project_env/keys.env")
    load_dotenv(dotenv_path=dotenv_path, override=False)

# -------------- Configuration --------------
# GCP credentials
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
GCS_CLOUD_PROJECT = os.environ.get("GCS_CLOUD_PROJECT")
#----- Telegram
TELEGRAM_API_TOKEN = "telegram_api_id"
TELEGRAM_HASH = "telegram_hash"

KEYWORDS = '../keywords.csv.csv'