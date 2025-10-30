import json
import os
import asyncio
from telethon import TelegramClient
from gcp.get_secret import get_secret
from gcp.clostorage import load_last_checked_ids, save_last_checked_ids


API_ID = get_secret('telegram_api_id', ncoding="yes")
API_HASH = get_secret('telegram_api_hash', ncoding="yes")
SESSION_NAME = os.getenv('TELETHON_SESSION_PATH', '/tmp/user_session.session')

def opencsv(file):
    with open(file) as f:
        line = f.readline().strip()
        values = line.split(',')
    return values

KEYWORDS = opencsv('keywords.csv')
TARGET_CHATS = opencsv('chats.csv')

NOTIFICATION_CHAT = 'me'

def load_session_file():
    session_data = get_secret('telegram_session_file', ncoding="no")
    with open('/tmp/user_session.session', 'wb') as f:
        f.write(session_data)


async def send_alert(client, message, keywords_found):
    chat_entity = await message.get_chat()
    chat_identifier = chat_entity.username or chat_entity.title
    message_link = f"https://t.me/c/{chat_entity.id}/{message.id}"
    alert_message = (
        f"🚨 **KEYWORD ALERT!** 🚨\n"
        f"**Keywords:** {', '.join(keywords_found)}\n"
        f"**Group:** `{chat_identifier}`\n"
        f"**Message:** {message.text[:150]}...\n"
        f"[Go to message]({message_link})"
    )
    await client.send_message(NOTIFICATION_CHAT, alert_message, parse_mode='md')

async def poll_telegram():
    load_session_file()
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    print("Connecting and logging in...")
    await client.start()
    print("Login successful.")
    last_checked_ids = load_last_checked_ids()
    print("Loaded last_checked_ids:", last_checked_ids)

    print("Initializing monitors... Fetching current message IDs for each chat.")
    resolved_entities = []

    for chat_ref in TARGET_CHATS:
        try:
            entity = await client.get_entity(chat_ref)
            resolved_entities.append(entity)
            if entity.id not in last_checked_ids:
                last_msg = await client.get_messages(entity, limit=1)
                if last_msg:
                    last_checked_ids[entity.id] = last_msg[0].id
                else:
                    last_checked_ids[entity.id] = 0
                print(f"Monitoring '{entity.title}'. Last message ID: {last_msg[0].id}")
        except Exception as e:
            print(f"Could not resolve {chat_ref}: {e}")

    for entity in resolved_entities:
        current_last_id = last_checked_ids.get(entity.id, 0)
        messages = await client.get_messages(entity, min_id=current_last_id)
        for message in reversed(messages):
            if message.text:
                found_keywords = [kw for kw in KEYWORDS if kw in message.text.lower()]
                if found_keywords:
                    print(f"Found keywords: {', '.join(found_keywords)}")
                    await send_alert(client, message, found_keywords)
            current_last_id = max(current_last_id, message.id)
        last_checked_ids[entity.id] = current_last_id + 1


    save_last_checked_ids(last_checked_ids)
    print("Saved last_checked_ids:", json.dumps(last_checked_ids, indent=2))


    await client.disconnect()

def main(request):
    asyncio.run(poll_telegram())
    return "Polling complete", 200

# from flask import Request
#
# class DummyRequest(Request):
#     def __init__(self):
#         super().__init__(environ={})
# if __name__ == "__main__":
#     dummy = DummyRequest()
#     response = main(dummy)
#     print(response)
