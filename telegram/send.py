import asyncio
import logging
import random
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
# Upper bound on a single 429 Retry-After wait so an aggressive value can't
# eat the whole poll budget. Anything above this is treated as permanent.
_MAX_RETRY_AFTER = 60


def _is_transient(status: int) -> bool:
    """Return True for status codes that are safe to retry."""
    return status in (429, 500, 502, 503, 504)


class _TransientError(RuntimeError):
    """A retryable Bot API failure (429 / 5xx)."""


class _MarkdownParseError(RuntimeError):
    """The message was rejected because Markdown could not be parsed."""


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
                    await _post_once(s, bot_url, payload)
            else:
                await _post_once(session, bot_url, payload)
            return  # success
        except _MarkdownParseError:
            # Recoverable: the same text failed Markdown parsing. Resend once
            # as plain text, then give up (no point retrying Markdown).
            logging.warning("⚠️  Message failed Markdown parsing; resending as plain text.")
            return await _send_plain_text(session, bot_url, payload)
        except _TransientError as exc:
            last_error = exc
            wait_s = getattr(exc, "retry_after", None)
            if wait_s:
                logging.warning(
                    "⏳ 429 Retry-After %d s (attempt %d/%d).", wait_s, attempt, _MAX_RETRIES
                )
            else:
                wait_s = 2 ** attempt + random.uniform(0, 1)
                logging.warning(
                    "🌐 Transient Bot API error (attempt %d/%d): %s",
                    attempt, _MAX_RETRIES, exc,
                )
            await asyncio.sleep(wait_s)
        except asyncio.TimeoutError:
            last_error = RuntimeError("Bot API request timed out")
            logging.warning(
                "⏱️  Bot API timeout (attempt %d/%d).", attempt, _MAX_RETRIES
            )
            await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
        except aiohttp.ClientError as exc:
            last_error = exc
            logging.warning(
                "🌐 Bot API network error (attempt %d/%d): %s",
                attempt, _MAX_RETRIES, exc,
            )
            await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
        except RuntimeError:
            # Permanent error raised by _post_once — do not retry.
            raise

    raise RuntimeError(f"Bot API delivery failed after {_MAX_RETRIES} attempts") from last_error


async def _send_plain_text(
    session: aiohttp.ClientSession | None,
    bot_url: str,
    payload: dict,
) -> None:
    """Resend *payload* without ``parse_mode`` (FR-ALERT-4 fallback)."""
    plain_payload = dict(payload)
    plain_payload.pop("parse_mode", None)
    if session is None:
        async with aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT) as s:
            await _post_once(s, bot_url, plain_payload)
    else:
        await _post_once(session, bot_url, plain_payload)


async def _post_once(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
) -> None:
    """Perform a single HTTP POST.  Returns on success; raises on failure."""
    async with session.post(url, json=payload) as resp:
        if resp.status == 200:
            logging.info("✅ Bot notification sent successfully.")
            return

        response_text = await resp.text()
        if resp.status == 429:
            # Respect Retry-After header if present (bounded so an aggressive
            # value can't consume the poll budget); otherwise let the caller
            # apply exponential backoff.
            retry_after = resp.headers.get("Retry-After")
            wait_s = None
            if retry_after is not None:
                try:
                    wait_s = min(int(retry_after), _MAX_RETRY_AFTER)
                except ValueError:
                    wait_s = None
            error_msg = f"Bot API returned 429: {response_text}"
            err = _TransientError(error_msg)
            err.retry_after = wait_s
            raise err

        error_msg = f"Bot API returned {resp.status}: {response_text}"
        if _is_transient(resp.status):
            logging.warning("⚠️  Transient error: %s", error_msg)
            raise _TransientError(error_msg)
        elif "can't parse entities" in response_text:
            logging.warning("⚠️  Markdown parse error: %s", error_msg)
            raise _MarkdownParseError(error_msg)

        logging.critical("❌ Permanent error: %s", error_msg)
        # Don't retry permanent errors — raise immediately
        raise RuntimeError(error_msg)


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
