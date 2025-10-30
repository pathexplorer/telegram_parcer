import json
from gcp.client import get_bucket

bucket = get_bucket()

def load_last_checked_ids(blob_path='telegram_state/last_checked_ids.json'):
    blob = bucket.blob(blob_path)
    if not blob.exists():
        return {}
    content = blob.download_as_text()
    return json.loads(content)

def save_last_checked_ids(data, blob_path='telegram_state/last_checked_ids.json'):
    blob = bucket.blob(blob_path)
    blob.upload_from_string(json.dumps(data), content_type='application/json')
