from google.cloud import storage
from functools import lru_cache
from project_env import config


@lru_cache(maxsize=1)
def get_client():
    return storage.Client()

@lru_cache(maxsize=1)
def get_bucket():
    return get_client().bucket(config.GCS_BUCKET_NAME)


