import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from gcp_actions.secret_manager import SecretManagerClient
from gcp_actions.client import get_env_and_cashed_it, logger

TELEGRAM_SECRETS = os.environ.get("TELEGRAM_SECRETS")
assert TELEGRAM_SECRETS is not None, "TELEGRAM_SECRETS environment variable is not set"


def get_telegram_secrets():
    sm = SecretManagerClient(get_env_and_cashed_it("GCP_PROJECT_ID"))

    current_secret_telegram_data = sm.get_secret_json(TELEGRAM_SECRETS)  # type: ignore[arg-type]

    # Inject the config into the environment
    for key, value in current_secret_telegram_data.items():
        os.environ[key] = str(value)
    logger.info(f"✅ Injected {len(current_secret_telegram_data)} configuration keys  into environment.")
    
get_telegram_secrets()
API_ID_STR = os.environ.get("API_ID")
API_HASH_STR = os.environ.get("API_HASH")
assert API_ID_STR is not None, "API_ID environment variable is not set after loading secrets"
assert API_HASH_STR is not None, "API_HASH environment variable is not set after loading secrets"

API_ID = int(API_ID_STR)
API_HASH: str = API_HASH_STR

# --- CONFIGURE THESE ---

# This is the chat ID you want your function to message
NOTIFICATION_CHAT_STR = os.getenv('NOTIFICATION_CHAT')
assert NOTIFICATION_CHAT_STR is not None, "NOTIFICATION_CHAT environment variable is not set"
NOTIFICATION_CHAT = int(NOTIFICATION_CHAT_STR)

async def main():
    print("Starting session generation...")
    print("We will log in and then cache all your chats.")

    # Start with a new, in-memory StringSession
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        print("\nPlease log in if prompted...")

        # Check if already logged in (will be fast if you run this twice)
        me = await client.get_me()
        print(f"Logged in as: {me.first_name}")

        print("\nCaching all dialogs to find the target chat...")

        found_target_group = False
        target_id = NOTIFICATION_CHAT

        try:
            # This loop forces the session to cache every chat's access hash
            async for dialog in client.iter_dialogs():
                print(f"Caching: '{dialog.title}' (ID: {dialog.id})")
                if dialog.id == target_id:
                    print(f"\n*** SUCCESS! Found and cached target group: {dialog.title} ***\n")
                    found_target_group = True

            if not found_target_group:
                print("\n--- WARNING ---")
                print(f"Finished all dialogs but did not find the target ID {target_id}.")
                print("The session will work, but your function might fail.")
            else:
                print("Target chat was found and cached.")

        except Exception as e:
            print(f"An error occurred: {e}")

        finally:
            # THIS IS THE MOST IMPORTANT PART
            print("\n--- Your New, Complete Session String ---")
            print("Copy this entire string (it's very long):")
            # This exports the session, including the auth key AND all the cached chat hashes
            print(client.session.save())


if __name__ == "__main__":
    asyncio.run(main())