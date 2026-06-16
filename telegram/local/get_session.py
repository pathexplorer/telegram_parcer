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
API_ID = os.environ.get("API_ID")
API_HASH_STR = os.environ.get("API_HASH")
assert API_ID is not None, "API_ID environment variable is not set after loading secrets"
assert API_HASH_STR is not None, "API_HASH environment variable is not set after loading secrets"

API_HASH: str = API_HASH_STR

# --- CONFIGURE THESE ---

# This is the chat ID you want your function to message
NOTIFICATION_CHAT_STR = os.getenv('NOTIFICATION_CHAT')
assert NOTIFICATION_CHAT_STR is not None, "NOTIFICATION_CHAT environment variable is not set"
NOTIFICATION_CHAT = int(NOTIFICATION_CHAT_STR)

async def main():
    print("Starting session generation...")
    print("We will log in and then cache all your chats.")

    # Use realistic device info to avoid Telegram blocking the login code
    client = TelegramClient(
        StringSession(),
        int(API_ID),
        API_HASH,
        device_model="Desktop Linux",
        system_version="Ubuntu 24.04",
        app_version="5.11.0 x64",
    )

    phone = input("Please enter your phone (or bot token): ").strip()
    if not phone:
        print("ERROR: No phone number entered. Exiting.")
        return

    await client.connect()

    try:
        # Explicitly send code request — more reliable than auto-login
        sent = await client.send_code_request(phone)
        print(f"✅ Verification code sent via {sent.type}. Check your Telegram app.")
        print(f"   (Also check SMS inbox and the 'Telegram' service chat on all devices.)")

        code = input("Enter the code you received: ").strip()
        if not code:
            print("ERROR: No code entered. Exiting.")
            return

        try:
            await client.sign_in(phone, code)
        except Exception as sign_in_error:
            error_str = str(sign_in_error)
            if "password" in error_str.lower() or "2fa" in error_str.lower():
                password = input("2FA password required: ").strip()
                await client.sign_in(password=password)
            else:
                raise

        me = await client.get_me()
        print(f"✅ Logged in as: {me.first_name} (@{me.username or 'no username'})")

        print("\nCaching all dialogs to find the target chat...")

        found_target_group = False
        target_id = NOTIFICATION_CHAT

        try:
            async for dialog in client.iter_dialogs():
                print(f"  Caching: '{dialog.title}' (ID: {dialog.id})")
                if dialog.id == target_id:
                    print(f"\n  *** SUCCESS! Found and cached target group: {dialog.title} ***\n")
                    found_target_group = True

            if not found_target_group:
                print(f"\n  --- WARNING: Target ID {target_id} not found in your dialogs ---")
            else:
                print("  ✅ Target chat was found and cached.")

        except Exception as e:
            print(f"An error occurred while caching dialogs: {e}")

        finally:
            print("\n--- Your New, Complete Session String ---")
            print("Copy this entire string (it's very long) and update it in Secret Manager:")
            print(client.session.save())

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())