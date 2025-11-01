import json
import os
import asyncio
import aiohttp
import csv
import logging
from telethon import TelegramClient
from gcp.get_secret import get_secret
from gcp.clostorage import load_last_checked_ids, save_last_checked_ids

logging.basicConfig(level=logging.INFO)

API_ID = get_secret('telegram_api_id', ncoding="yes")
API_HASH = get_secret('telegram_api_hash', ncoding="yes")
SESSION_NAME = os.getenv('TELETHON_SESSION_PATH', '/tmp/user_session.session')
NOTIFICATION_CHAT = int(os.getenv('NOTIFICATION_CHAT'))
BOT_TOKEN = get_secret('telegram_bot_token', ncoding="yes")

def opencsv(file):
    """
        Reads a CSV file, treating each column as a keyword or a phrase.
        Phrases must be enclosed in double quotes in the CSV file (e.g., "phrase one").
    """
    keywords = []
    try:
        with open(file, encoding='utf-8', newline='') as f:
            # Use csv.reader to handle quoted phrases correctly
            reader = csv.reader(f)
            for row in reader:
                # The reader returns a list of items (words or phrases) from the line
                for item in row:
                    if item.strip():
                        # Add the cleaned and lowercased item (which might be a phrase)
                        keywords.append(item.strip().lower())
        return keywords
    except Exception as e:
        logging.error(f"Error reading keywords file {file}: {e}")
        return []

KEYWORDS = opencsv('keywords.csv')
TARGET_CHATS = opencsv('chats.csv')

def load_session_file():
    session_data = get_secret('telegram_session_file', ncoding="no")
    with open('/tmp/user_session.session', 'wb') as f:
        f.write(session_data)


async def send_bot_notification(text_message):
    """
    Sends a message using the Bot's HTTP API.
    This will appear as an "unread" message.
    """
    bot_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': NOTIFICATION_CHAT,
        'text': text_message,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': True
    }

    logging.info(f"Sending notification via Bot API to chat_id: {NOTIFICATION_CHAT}...")
    async with aiohttp.ClientSession() as session:
        async with session.post(bot_url, json=payload) as resp:
            if resp.status != 200:
                response_text = await resp.text()
                logging.error(f"❌ ERROR sending bot notification: {resp.status} - {response_text}")
            else:
                logging.info("✅ Bot notification sent successfully.")

async def send_alert(message, keywords_found):
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
    await send_bot_notification(alert_message)

async def poll_telegram():
    load_session_file() # authorization via previous manual input phone, password and code form SMS
    async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
        logging.debug(f"DEBUG: Notification target ID is set to: {NOTIFICATION_CHAT} (Type: {type(NOTIFICATION_CHAT)})")
        logging.debug("Connecting and logging in...")
        await client.start()
        logging.debug("Login successful.")
        # --- THIS IS THE CORRECTED CACHING BLOCK ---
        logging.debug("Caching all dialogs to find access hashes...")
        try:
            # This loop will run and populate the session's entity cache.
            # This is the only part we need.
            found_group = False
            async for dialog in client.iter_dialogs():
                if dialog.id == NOTIFICATION_CHAT:
                    logging.debug(f"✅ Successfully found and cached notification group: {dialog.title}")
                    found_group = True

            if not found_group:
                logging.warning(
                    "WARNING: Caching finished but did not find the target notification group. The session might be stale.")
                # We will let it continue, but it might fail at the 'send_alert' step

        except Exception as e:
            # This should no longer fail with "Could not find input entity"
            logging.error(f"❌ FATAL ERROR during dialog caching: {e}")
            await client.disconnect()
            return  # Stop the function
        # --- END OF CORRECTED BLOCK ---


        # This will load keys as STRINGS (e.g., '3240356500')
        last_checked_ids = load_last_checked_ids()
        logging.debug("Loaded last_checked_ids:", last_checked_ids)

        resolved_entities = []
        for chat_ref in TARGET_CHATS:
            try:
                entity = await client.get_entity(chat_ref)
                #Show last messages from all chats fot testing
                # try:
                #     entity = await client.get_entity(chat_ref)
                #     last_msg = await client.get_messages(entity, limit=1)
                #     if last_msg:
                #         print(f"Chat: {entity.title} | Message ID: {last_msg[0].id}")
                #         print(f"Text: {repr(last_msg[0].text)}\n")
                #     else:
                #         print(f"Chat: {entity.title} | No messages found.\n")
                # except Exception as e:
                #     print(f"Error fetching from {chat_ref}: {e}")
                resolved_entities.append(entity)

                # --- FIX 1: Convert entity.id to string for all key operations ---
                chat_id_str = str(entity.id)

                if chat_id_str not in last_checked_ids:
                    # Initialize last_checked_id by fetching the latest message ID
                    last_msg = await client.get_messages(entity, limit=1)

                    last_checked_ids[chat_id_str] = last_msg[0].id if last_msg and last_msg[0].id else 0
                    logging.debug(f"Initialized '{entity.title}' at message ID: {last_checked_ids[chat_id_str]}")
            except Exception as e:
                logging.error(f"Could not resolve {chat_ref}: {e}")

        # Process messages
        for entity in resolved_entities:

            # --- FIX 2: Use string for all key operations ---
            chat_id_str = str(entity.id)

            # Use the string key to get the last ID
            current_last_id = last_checked_ids.get(chat_id_str, 0)
            logging.debug(f"\n▶️ Scanning '{entity.title}' (ID: {chat_id_str}) after ID {current_last_id}")

            await asyncio.sleep(2)  # Delay for stability

            messages = await client.get_messages(entity, min_id=current_last_id)

            if not messages:
                logging.debug("No new messages found.")
                continue

            logging.info(f"Fetched {len(messages)} new messages.")

            max_id = current_last_id

            for message in reversed(messages):
                message_text = message.text

                if message_text:
                    normalized_text = message_text.lower()
                    found_keywords = [kw for kw in KEYWORDS if kw in normalized_text]

                    logging.info(f"Message ID {message.id}: {repr(message_text[:50])}...")
                    logging.info(f"Matched keywords: {found_keywords}")

                    if found_keywords:
                        try:
                            await send_alert(message, found_keywords)
                            logging.info(f"Alarm sent successfully for message ID: {message.id}")
                        except Exception as alert_e:
                            # Log the specific error when sending the alert
                            logging.error(f"❌ ERROR sending alert for Message ID {message.id}: {alert_e}")
                            # Re-raise if necessary to stop the function, but logging is crucial
                            # raise alert_e
                else:
                    logging.debug(f"Message ID {message.id}: (Non-text message)")

                max_id = max(max_id, message.id)

            # --- FIX 3: Use string for all key operations ---
            # Update the last checked ID using the string key
            last_checked_ids[chat_id_str] = max_id
            logging.debug(f"Updated last checked ID to: {max_id}")

        save_last_checked_ids(last_checked_ids)
        logging.debug("✅ Saved last_checked_ids:", json.dumps(last_checked_ids, indent=2))
        await client.disconnect()


def main(request):
    asyncio.run(poll_telegram())
    return "Polling complete", 200

# --- Local Test Entry Point ---
# This block is 100% ignored by Google Cloud Function
if __name__ == "__main__":
    # --- Move all test-only code INSIDE this block ---
    from flask import Request
    class DummyRequest(Request):
        def __init__(self):
            super().__init__(environ={})
    logging.info("--- Running in local test mode ---")
    dummy = DummyRequest()
    response = main(dummy)
    logging.info(response)
    logging.info("--- Local test complete ---")

