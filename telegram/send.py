import logging
import aiohttp
from project_env.config import BOT_TOKEN, NOTIFICATION_CHAT

logger = logging.getLogger(__name__)


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
    logging.debug(f"Sending notification via Bot API to chat_id: {NOTIFICATION_CHAT}...")

    async with aiohttp.ClientSession() as session:
        async with session.post(bot_url, json=payload) as resp:
            if resp.status != 200:
                response_text = await resp.text()
                error_msg = f"Bot API returned {resp.status}: {response_text}"
                logging.critical(f"❌ {error_msg}")
                # Raise so the caller can retry / not advance the cursor
                raise RuntimeError(error_msg)
            else:
                logging.info("✅ Bot notification sent successfully.")


async def send_alert(message, keywords_found):
    chat_entity = await message.get_chat()
    # Safely get a chat identifier (username is preferred, then title, then a string ID)
    chat_identifier = (
        chat_entity.username
        or getattr(chat_entity, 'title', None)
        or getattr(chat_entity, 'first_name', None)
        or str(chat_entity.id)
    )
    message_link = f"https://t.me/c/{chat_entity.id}/{message.id}"
    alert_message = (
        f"🚨 **KEYWORD ALERT!** 🚨\n"
        f"**Keywords:** {', '.join(keywords_found)}\n"
        f"**Group:** `{chat_identifier}`\n"
        f"**Message:** {message.text[:300].strip()}...\n"  # Added .strip() for clean excerpt

        f"[Go to message]({message_link})"
    )
    await send_bot_notification(alert_message)


async def send_health_alert(title: str, body: str, level: str = "error") -> None:
    """Send a pipeline health/status alert via the bot (non-keyword, operational).

    Args:
        title: Short alert title (e.g. "Chat resolution failed").
        body: Markdown-formatted detail message.
        level: Severity — "error" or "warning" (affects emoji prefix).
    """
    emoji = "❌" if level == "error" else "⚠️"
    health_message = (
        f"{emoji} **{title}** {emoji}\n\n"
        f"{body}"
    )
    await send_bot_notification(health_message)


# async def show_last_messages(entity, client, chat_ref):
#     try:
#         last_msg = await client.get_messages(entity, limit=1)
#         if last_msg:
#             print(f"Chat: {entity.title} | Message ID: {last_msg[0].id}")
#             print(f"Text: {repr(last_msg[0].text)}\n")
#         else:
#             print(f"Chat: {entity.title} | No messages found.\n")
#     except Exception as e:
#         print(f"Error fetching from {chat_ref}: {e}")
