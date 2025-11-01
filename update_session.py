import asyncio
import os
from telethon import TelegramClient
from gcp.get_secret import get_secret  # Assuming this works locally

# --- CONFIGURE THESE ---
API_ID = get_secret('telegram_api_id', ncoding="yes")
API_HASH = get_secret('telegram_api_hash', ncoding="yes")
SESSION_NAME = os.getenv('TELETHON_SESSION_PATH', '/tmp/user_session.session')
NOTIFICATION_CHAT = int(os.getenv('NOTIFICATION_CHAT'))


async def main():
    print(f"Logging in with session: {SESSION_NAME}")
    print("If this is a new session, you will be asked to log in.")

    # Use a 'with' block to ensure it saves the session
    async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
        print("Login successful. Now iterating all dialogs to cache them...")

        found_target_group = False
        target_id = NOTIFICATION_CHAT

        try:
            # This loop forces the session to cache every chat
            async for dialog in client.iter_dialogs():
                print(f"Found: '{dialog.title}' (ID: {dialog.id})")
                if dialog.id == target_id:
                    print(f"\n*** SUCCESS! Found and cached target group: {dialog.title} ***\n")
                    found_target_group = True

            if not found_target_group:
                print("\n--- WARNING ---")
                print(f"Finished iterating all dialogs but did not find the target ID {target_id}.")
                print("Please ensure you are a member of this group and the ID is correct.")
            else:
                print("Session file has been successfully updated with the group's access hash.")

        except Exception as e:
            print(f"An error occurred: {e}")


if __name__ == "__main__":
    # If your session file is in a non-standard location, delete it first
    # Example: os.remove(SESSION_NAME)
    print("--- Running session updater ---")
    asyncio.run(main())