import asyncio
import logging
import re

import aiohttp

from project_env.config import BOT_TOKEN, NOTIFICATION_CHAT

logger = logging.getLogger(__name__)

# Escape characters that have meaning in Telegram Markdown so untrusted raw
# message text / identifiers can't break the surrounding message formatting.
_MARKDOWN_CHARS = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")
_MARKDOWN_ESCAPE = r"\\\1"

# ── HTTP client defaults ──────────────────────────────────────────────────
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MAX_RETRIES = 3


def _is_transient(status: int) -> bool:
    """Return True for status codes that are safe to retry."""
    return status in (429, 500, 502, 503, 504)


def _md_escape(text: str) -> str:
    """Escape Telegram Markdown metacharacters in an untrusted string.

    Backslashes are escaped first so any pre-existing escapes/paths in the
    source text stay inert, then every Markdown metacharacter is escaped.
    """
    escaped = text.replace("\\", "\\\\")
    return _MARKDOWN_CHARS.sub(_MARKDOWN_ESCAPE, escaped)


async def send_bot_notification(
    text_message: str,
    *,
    session: aiohttp.ClientSession | None = None,
) -> None:
    """Send a message via the Bot HTTP API with bounded retries.

    Args:
        text_message: Markdown-formatted alert text.
        session: Optional shared ``aiohttp.ClientSession``.  When provided
            the caller owns session lifecycle (one session per poll cycle).
            When *None* a temporary session is created (backward-compatible).

    Raises:
        RuntimeError: After all retries are exhausted.
    """
    bot_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": NOTIFICATION_CHAT,
        "text": text_message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    logging.debug(
        "Sending notification via Bot API to chat_id: %d...", NOTIFICATION_CHAT
    )

    last_error: Exception | None = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            if session is None:
                async with aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT) as s:
                    last_error = await _post_once(s, bot_url, payload)
            else:
                last_error = await _post_once(session, bot_url, payload)
        except asyncio.TimeoutError:
            last_error = RuntimeError("Bot API request timed out")
            logging.warning(
                "⏱️  Bot API timeout (attempt %d/%d).", attempt, _MAX_RETRIES
            )
        except aiohttp.ClientError as exc:
            last_error = exc
            logging.warning(
                "🌐 Bot API network error (attempt %d/%d): %s",
                attempt, _MAX_RETRIES, exc,
            )
        except RuntimeError as exc:
            # Permanent error raised by _post_once. Only a malformed-Markdown
            # error is recoverable (resend as plain text); anything else raises
            # immediately exactly as before.
            if "can't parse entities" not in str(exc):
                raise
            last_error = exc

        if last_error is None:
            return  # success

        # fallthrough above guaranteed a Markdown parse error:
        logging.warning("⚠️  Message failed Markdown parsing; resending as plain text.")
        plain_payload = dict(payload)
        plain_payload.pop("parse_mode", None)
        try:
            if session is None:
                async with aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT) as s:
                    last_error = await _post_once(s, bot_url, plain_payload)
            else:
                last_error = await _post_once(session, bot_url, plain_payload)
        except RuntimeError as exc:
            last_error = exc
        if last_error is None:
            return  # plain text delivered successfully
        raise RuntimeError(f"Bot API delivery failed after {_MAX_RETRIES} attempts") from last_error


async def _post_once(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
) -> Exception | None:
    """Perform a single HTTP POST.  Returns *None* on success, the exception on failure."""
    async with session.post(url, json=payload) as resp:
        if resp.status == 200:
            logging.info("✅ Bot notification sent successfully.")
            return None

        response_text = await resp.text()
        if resp.status == 429:
            # Respect Retry-After header if present
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    wait_s = int(retry_after)
                    logging.warning(
                        "⏳ Telegram 429 — Retry-After %d s. Waiting…", wait_s
                    )
                    await asyncio.sleep(wait_s)
                except ValueError:
                    pass

        error_msg = f"Bot API returned {resp.status}: {response_text}"
        if _is_transient(resp.status):
            logging.warning("⚠️  Transient error: %s", error_msg)
        else:
            logging.critical("❌ Permanent error: %s", error_msg)
            # Don't retry permanent errors — raise immediately
            raise RuntimeError(error_msg)

        return RuntimeError(error_msg)


async def send_alert(message, keywords_found, *, session: aiohttp.ClientSession | None = None):
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
        f"**Group:** `{_md_escape(chat_identifier)}`\n"
        f"**Message:** {_md_escape(message.text[:300].strip())}...\n"

        f"[Go to message]({message_link})"
    )
    await send_bot_notification(alert_message, session=session)


async def send_health_alert(title: str, body: str, level: str = "error", *, session: aiohttp.ClientSession | None = None) -> None:
    """Send a pipeline health/status alert via the bot (non-keyword, operational).

    Args:
        title: Short alert title (e.g. "Chat resolution failed").
        body: Markdown-formatted detail message.
        level: Severity — "error" or "warning" (affects emoji prefix).
        session: Optional shared ``aiohttp.ClientSession``.
    """
    emoji = "❌" if level == "error" else "⚠️"
    health_message = (
        f"{emoji} **{title}** {emoji}\n\n"
        f"{body}"
    )
    await send_bot_notification(health_message, session=session)
