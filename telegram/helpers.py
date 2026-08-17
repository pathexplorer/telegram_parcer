"""Stateless helper utilities for the Telegram polling pipeline.

These functions are shared across the polling, cursor-management, and
resolution modules.  None of them depend on Telethon or Firestore — they are
pure (or env-only) and safe to unit-test in isolation.
"""

import logging
import os
import time
import unicodedata

logger = logging.getLogger(__name__)


def _get_priority_chat_refs() -> set[str]:
    """Read the comma-separated PRIORITY_CHAT_REFS env var (case-insensitive).

    Chats matching these refs (username, e.g. ``@greenfield9000``, or numeric
    ID) are polled FIRST so e.g. an e2e test chat does not wait behind all
    other channels.  Returns a lowercased set of refs.
    """
    raw = os.getenv("PRIORITY_CHAT_REFS", "")
    return {r.strip().casefold() for r in raw.split(",") if r.strip()}


def _chat_sort_key(item, priority_refs: set[str]) -> tuple:
    """Sort key: priority chats first (by numeric ID), then the remaining chats.

    The default order is numeric chat ID ascending, which means a newly-added
    (often large-ID) test chat lands at the end of the queue.  Marking it as
    priority moves it to the front without changing the rest of the order.
    """
    name, values = item
    ref = ""
    if isinstance(values, dict):
        ref = str(values.get("ref", ""))
    is_priority = name.casefold() in priority_refs or ref.casefold() in priority_refs
    return (0 if is_priority else 1, int(name))


def _should_stop(shutdown_event, deadline):
    """Check if polling should stop due to signal or time limit.

    Args:
        shutdown_event: threading.Event or None — set by SIGINT/SIGTERM handler.
        deadline: float or None — time.monotonic() timestamp after which to stop.

    Returns:
        (should_stop: bool, reason: str or None)
    """
    if shutdown_event and shutdown_event.is_set():
        return True, "signal"
    if deadline is not None and time.monotonic() >= deadline:
        return True, "timeout"
    return False, None


def _find_matching_keywords(text: str, keywords: list[str]) -> list[str]:
    """Return the subset of *keywords* present in *text* (substring match).

    Matching is case-insensitive and Unicode-aware: both sides are
    NFKC-normalized and casefolded so fullwidth Latin, composed/decomposed
    forms and case variants all match (e.g. "café" matches "CAFÉ" and
    "cafe\u0301"). Substring semantics — a keyword matches when it appears
    anywhere in the text, with no word-boundary or regex logic.

    Args:
        text: Raw message text (may be empty/None).
        keywords: Normalized keyword list (see starter_conf).

    Returns:
        The keywords found in *text*, in the order they appear in *keywords*.
    """
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    return [kw for kw in keywords if kw in normalized_text]


def _safe_title(entity):
    """Return a human-readable title for any Telethon entity type.

    Channel / Chat / megagroup → .title
    User                       → .first_name (+ .last_name if present)
    Fallback                   → str(entity.id)
    """
    title = getattr(entity, 'title', None)
    if title:
        return title
    first = getattr(entity, 'first_name', None)
    if first:
        last = getattr(entity, 'last_name', None)
        return f"{first} {last}".strip() if last else first
    return str(getattr(entity, 'id', 'Unknown'))
