import json
import os
import asyncio
from telethon import TelegramClient
from gcp.get_secret import get_secret
from gcp.clostorage import load_last_checked_ids, save_last_checked_ids

API_ID = get_secret('telegram_api_id', ncoding="yes")
API_HASH = get_secret('telegram_api_hash', ncoding="yes")
SESSION_NAME = os.getenv('TELETHON_SESSION_PATH', '/tmp/user_session.session')
NOTIFICATION_CHAT = 'me'

def opencsv(file):
    with open(file, encoding='utf-8') as f:
        return [v.strip().lower() for v in f.readline().strip().split(',') if v.strip()]

KEYWORDS = opencsv('keywords.csv')
TARGET_CHATS = opencsv('chats.csv')

def load_session_file():
    session_data = get_secret('telegram_session_file', ncoding="no")
    with open('/tmp/user_session.session', 'wb') as f:
        f.write(session_data)

async def test_latest_messages():
    load_session_file()
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    print("🔍 Testing latest messages from target chats...")

    for chat_ref in TARGET_CHATS:
        try:
            entity = await client.get_entity(chat_ref)
            last_msg = await client.get_messages(entity, limit=1)
            if last_msg:
                print(f"Chat: {entity.title} | Message ID: {last_msg[0].id}")
                print(f"Text: {repr(last_msg[0].text)}\n")
            else:
                print(f"Chat: {entity.title} | No messages found.\n")
        except Exception as e:
            print(f"Error fetching from {chat_ref}: {e}")

    await client.disconnect()


async def send_alert(client, message, keywords_found):
    chat_entity = await message.get_chat()
    # Safely get a chat identifier (username is preferred, then title, then a string ID)
    chat_identifier = chat_entity.username or chat_entity.title or str(chat_entity.id)
    message_link = f"https://t.me/c/{chat_entity.id}/{message.id}"
    alert_message = (
        f"🚨 **KEYWORD ALERT!** 🚨\n"
        f"**Keywords:** {', '.join(keywords_found)}\n"
        f"**Group:** `{chat_identifier}`\n"
        f"**Message:** {message.text[:150].strip()}...\n"  # Added .strip() for clean excerpt
        f"[Go to message]({message_link})"
    )
    await client.send_message(NOTIFICATION_CHAT, alert_message, parse_mode='md')


async def poll_telegram():
    load_session_file()
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    print("Connecting and logging in...")
    await client.start()
    print("Login successful.")

    # This will load keys as STRINGS (e.g., '3240356500')
    last_checked_ids = load_last_checked_ids()
    print("Loaded last_checked_ids:", last_checked_ids)

    resolved_entities = []
    for chat_ref in TARGET_CHATS:
        try:
            entity = await client.get_entity(chat_ref)
            resolved_entities.append(entity)

            # --- FIX 1: Convert entity.id to string for all key operations ---
            chat_id_str = str(entity.id)

            if chat_id_str not in last_checked_ids:
                # Initialize last_checked_id by fetching the latest message ID
                last_msg = await client.get_messages(entity, limit=1)
                last_checked_ids[chat_id_str] = last_msg[0].id if last_msg and last_msg[0].id else 0
                print(f"Initialized '{entity.title}' at message ID: {last_checked_ids[chat_id_str]}")
        except Exception as e:
            print(f"Could not resolve {chat_ref}: {e}")

    # Process messages
    for entity in resolved_entities:

        # --- FIX 2: Use string for all key operations ---
        chat_id_str = str(entity.id)

        # Use the string key to get the last ID
        current_last_id = last_checked_ids.get(chat_id_str, 0)
        print(f"\n▶️ Scanning '{entity.title}' (ID: {chat_id_str}) after ID {current_last_id}")

        await asyncio.sleep(2)  # Delay for stability

        messages = await client.get_messages(entity, min_id=current_last_id)

        if not messages:
            print("No new messages found.")
            continue

        print(f"Fetched {len(messages)} new messages.")

        max_id = current_last_id

        for message in reversed(messages):
            message_text = message.text

            if message_text:
                normalized_text = message_text.lower()
                found_keywords = [kw for kw in KEYWORDS if kw in normalized_text]

                print(f"Message ID {message.id}: {repr(message_text[:50])}...")
                print(f"Matched keywords: {found_keywords}")

                if found_keywords:
                    print(f"✅ Found keywords: {', '.join(found_keywords)}")
                    await send_alert(client, message, found_keywords)
            else:
                print(f"Message ID {message.id}: (Non-text message)")

            max_id = max(max_id, message.id)

        # --- FIX 3: Use string for all key operations ---
        # Update the last checked ID using the string key
        last_checked_ids[chat_id_str] = max_id + 1
        print(f"Updated last checked ID to: {max_id + 1}")

    save_last_checked_ids(last_checked_ids)
    print("✅ Saved last_checked_ids:", json.dumps(last_checked_ids, indent=2))
    await client.disconnect()


def main(request):
    asyncio.run(poll_telegram())
    return "Polling complete", 200

# # Enable additon to def main for test and get latest messages
# if __name__ == "__main__":
#     asyncio.run(test_latest_messages())

# # Local test entry point
# from flask import Request
# class DummyRequest(Request):
#     def __init__(self):
#         super().__init__(environ={})
#
# if __name__ == "__main__":
#     dummy = DummyRequest()
#     response = main(dummy)
#     print(response)
