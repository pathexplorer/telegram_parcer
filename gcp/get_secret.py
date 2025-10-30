from google.cloud import secretmanager
from google.api_core.exceptions import AlreadyExists
from project_env.config import GCP_PROJECT_ID

secret_client = secretmanager.SecretManagerServiceClient()

def get_secret(secret_id: str, version_id="latest", ncoding: str = 'yes'):

    name = f"projects/{GCP_PROJECT_ID}/secrets/{secret_id}/versions/{version_id}"
    response = secret_client.access_secret_version(request={"name": name})
    data = response.payload.data
    if ncoding == "yes":
        try:
            return data.decode("utf-8").strip()
        except UnicodeDecodeError as e:
            raise ValueError(f"Secret '{secret_id}' is not valid UTF-8: {e}")
    elif ncoding == "no":
        return data  # Return raw bytes
    else:
        raise ValueError(f"Invalid 'ncoding' value: {ncoding}")


def create_secret(secret_id: str):
    short_parent = f"projects/{GCP_PROJECT_ID}"
    try:
        secret_client.create_secret(
            request={
                "parent": short_parent,
                "secret_id": secret_id,
                "secret": {
                    "replication": {"automatic": {}}
                },
            }
        )
    except AlreadyExists:
        print(f"Secret '{secret_id}' already exists. Skipping creation.")

def update_secret(secret_id: str, new_value):
    parent = f"projects/{GCP_PROJECT_ID}/secrets/{secret_id}"
    secret_client.add_secret_version(
        request={
            "parent": parent,
            "payload": {"data": new_value.encode("UTF-8")}
        }
    )