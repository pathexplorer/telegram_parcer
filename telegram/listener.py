import logging
import asyncio
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from gcp_actions.firestore_box.json_manipulations import FirestoreMagic
from telegram.send import send_alert
from project_env.config import session_string, API_ID, API_HASH

logger = logging.getLogger(__name__)

COUNT_PROCESSED_MESSAGES = 0
COUNT_KEYWORD_MATCHES = 0
COUNT_ALERTS_SENT = 0

async def poll_telegram(KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids):

    # --- Initialize Firestore client for cursor_base ---
    fs = FirestoreMagic("telegram", "cursor_base")

    # --- Guard: validate inputs before entering the loop ---
    if not TARGET_CHATS_LIST:
        logger.critical("FATAL: TARGET_CHATS_LIST is empty. Nothing to scan.")
        return
    if not KEYWORDS_LIST:
        logger.critical("FATAL: KEYWORDS_LIST is empty. Nothing to match.")
        return
    first_msg_date = None
    last_msg_date = None

    async with TelegramClient(StringSession(session_string), API_ID, API_HASH,flood_sleep_threshold=60) as client:
        await client.start()

        # previous_checked_ids = fs.load_firejson("cursor_base")
        # """ Return: nested dict { '12345' : [ '@name' , 11 ], '67890' : [ '@name' , 22 ] } """

        # 1. Build a fast lookup map of usernames we already know.
        # Result: {"@name1": "12345", "@name2": "67890"}
        # try:
        #     known_usernames_to_ids = {
        #         values[0]: key for key, values in previous_checked_ids.items()
        #     }
        # except IndexError:
        #     logging.error("Database is corrupt. Rebuilding.")
        #     known_usernames_to_ids = {}
        #     # You might to clear previous_checked_ids here
        #
        # logging.info(f"Loaded {len(known_usernames_to_ids)} known chats from database.")

        db_was_updated = False

        # TARGET_CHATS_LIST = [
        #     chat.strip()
        #     for chat in TARGET_CHATS.split(',')
        #     if chat.strip()  # This ignores empty strings that result from trailing commas
        # ]
        # """ Convert string to list """
        #
        # KEYWORDS_LIST = [
        #     chat.strip()
        #     for chat in KEYWORDS.split(',')
        #     if chat.strip()  # This ignores empty strings that result from trailing commas
        # ]
        # """ Convert string to list """

        for chat_ref in TARGET_CHATS_LIST:  # TARGET_CHATS: @name1, @name2..
            try:
                # Check our FAST local map first. This is instant and uses no network.
                if chat_ref not in known_usernames_to_ids:
                    logging.info(f"New or changed username: '{chat_ref}', use the network to get its ID")
                    entity = await client.get_entity(chat_ref)  # >> Channel(id=12345, title='abcdefg', .... )
                    chat_id_str = str(entity.id)
                    # await show_last_messages(entity, client, chat_ref) # only for test

                    # This logic handles two cases:
                    # A) A brand-new chat ID.
                    # B) A chat ID we know, but with a new username (a rename).
                    if chat_id_str not in previous_checked_ids:
                        # --- Case A: Brand new chat ---
                        logging.info(f"Adding new chat '{entity.title}' ({chat_id_str}) to database.")
                        try:
                            last_msg = await client.get_messages(entity, limit=1)
                        except FloodWaitError as e:
                            # This code runs, but Telethon is *also* sleeping
                            logging.critical(f"We hit a flood wait for {e.seconds} seconds. My bot is too fast!")
                            # No need to add 'await asyncio.sleep(e.seconds)',
                            # Telethon is already doing it.
                        except Exception as e:
                            logging.error(f"A different error: {e}")
                        last_id = last_msg[0].id if last_msg else 0
                        previous_checked_ids[chat_id_str] = [chat_ref, last_id]
                    else:
                        # --- Case B: Renamed chat ---
                        old_ref = previous_checked_ids[chat_id_str][0]
                        logging.warning(
                            f"Username for {chat_id_str} changed from '{old_ref}' to '{chat_ref}'. Updating.")
                        previous_checked_ids[chat_id_str][0] = chat_ref  # Update the username
                    db_was_updated = True
                else:
                    logging.debug(f"Chat '{chat_ref}' already in database. Skipping.")
            except Exception as e:
                # This will catch errors from get_entity (e.g., username not found)
                logging.critical(f"Could not resolve or process {chat_ref}: {e}")

        # 4. Upload to Firebase *ONCE* at the end, only if needed.
        if db_was_updated:
            logging.info("Database was updated, uploading to Firebase...")
            fs.set_firejson(previous_checked_ids, merge=True)
        else:
            logging.debug("No database changes detected.")
        # 5.
        for name, values in previous_checked_ids.items():
            # --- Guard: validate cursor entry structure ---
            if not isinstance(values, (list, tuple)) or len(values) < 2:
                logger.error("Corrupt entry for chat '%s': %s. Skipping.", name, values)
                continue
            value0 = values[0]
            value1 = values[1]

            # ----- Get the lastest message ID in a channel
            # A. Load previous state
            chat_id_str = str(name)
            current_last_message_id = value1 # >> as sample, 34
            logging.debug(f"Scanning '{value0}' (ID: {chat_id_str}) after ID {current_last_message_id}")

            await asyncio.sleep(2)  # Delay for stability

            # B. Resolve entity — try username first, fall back to numeric ID.
            #    This handles groups that changed their @username or went private.
            try:
                entity = await client.get_entity(value0)
            except ValueError:
                logging.warning(
                    f"Username '{value0}' not found for chat {chat_id_str} "
                    f"(group may have lost its public username or gone private). "
                    f"Trying by numeric ID..."
                )
                try:
                    entity = await client.get_entity(int(chat_id_str))
                except Exception as e2:
                    logging.error(
                        f"Cannot resolve chat {chat_id_str} by numeric ID either: {e2}. Skipping."
                    )
                    continue
                # Update stored reference: new username if available, else use numeric ID
                new_username = getattr(entity, 'username', None)
                if new_username:
                    new_ref = f"@{new_username}"
                    logging.info(
                        f"Chat {chat_id_str} renamed from '{value0}' to '{new_ref}'. Updating cursor."
                    )
                    values[0] = new_ref
                else:
                    logging.info(
                        f"Chat {chat_id_str} ('{entity.title}') has no public username. "
                        f"Will resolve by numeric ID from now on."
                    )
                    values[0] = str(chat_id_str)
                db_was_updated = True

            # C. Get actual messages
            try:
                messages = await client.get_messages(entity, min_id=current_last_message_id, limit=None)
                """ Result: 
                        1. empty space if no new
                        1.1 Or metadata of new mess: Message(id=68, peer_id=PeerChannel(channel_id=12345), date=datetime.datetime(2025, 11, 4, 12, 7, 33, tzinfo=datetime.timezone.utc), message='{content_of_message}', out=False,...)
                        2. total=29 is real quantity of messages + create channel message  
                    min_id means: only message.id > current_last_message_id will get            
                    """
                if not messages:
                    logging.debug("No new messages found.")
                    continue
                logging.debug(f"Fetched {len(messages)} new messages.")
            except FloodWaitError as e:
                logging.critical(f"We hit a flood wait for {e.seconds} seconds. My bot is too fast!")
            except Exception as e:
                logging.error(f"Failed to fetch messages for '{value0}' (chat {chat_id_str}): {e}")
                continue  # skip this chat on error (e.g., bot account can't use GetHistoryRequest)
            db_was_updated = True # need to save our state

            newest_message_id = messages[0].id

            last_acked_id = current_last_message_id  # fallback: don't advance cursor if no messages processed

            # Track overall date range across all chats
            if messages:
                if first_msg_date is None or messages[-1].date < first_msg_date:
                    first_msg_date = messages[-1].date  # oldest
                if last_msg_date is None or messages[0].date > last_msg_date:
                    last_msg_date = messages[0].date      # newest

            for message in reversed(messages):
                global COUNT_PROCESSED_MESSAGES, COUNT_KEYWORD_MATCHES, COUNT_ALERTS_SENT
                COUNT_PROCESSED_MESSAGES += 1
                message_text = message.text

                if message_text:
                    normalized_text = message_text.lower()
                    found_keywords = [kw for kw in KEYWORDS_LIST if kw in normalized_text]

                    logging.debug(f"Message ID {message.id}: {repr(message_text[:50])}...")
                    logging.debug(f"Matched keywords: {found_keywords}")

                    if found_keywords:
                        COUNT_KEYWORD_MATCHES += 1
                        try:
                            await send_alert(message, found_keywords)
                            logging.info(f"Alarm sent successfully for message ID: {message.id}")
                            COUNT_ALERTS_SENT += 1
                            last_acked_id = message.id  # only advance cursor on success
                        except Exception as alert_e:
                            logging.error(f"❌ ERROR sending alert for Message ID {message.id}: {alert_e}. "
                                          f"Cursor will NOT advance past this message — will retry on next run.")
                else:
                    logging.debug(f"Message ID {message.id}: (Non-text message)")

                # For non-keyword messages, advance cursor normally
                if not message_text or not found_keywords:
                    last_acked_id = message.id

            previous_checked_ids[name][1] = last_acked_id
        date_range = ""
        if first_msg_date and last_msg_date:
            date_range = f" | Range: {first_msg_date.strftime('%d.%m.%Y')} → {last_msg_date.strftime('%d.%m.%Y')}"
        logging.info(f"--- Stats: Processed= {COUNT_PROCESSED_MESSAGES} | Matches= {COUNT_KEYWORD_MATCHES} | Alerts= {COUNT_ALERTS_SENT}{date_range} ---")

        # Save to Firebase *only* if there were changes
        if db_was_updated:
            fs.set_firejson(previous_checked_ids, merge=True)
            logging.debug(f"Saved previous_checked_ids to Firebase...: {previous_checked_ids}")
        else:
            logging.info("No new messages found in any chat. No DB update.")
        await client.disconnect()
