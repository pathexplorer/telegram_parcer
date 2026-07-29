from gcp_actions.firestore_box.json_manipulations import FirestoreMagic
import logging
logger = logging.getLogger(__name__)

def forming_configuration():
    """
    Loads keywords, target chats, and cursor state from Firestore.
    Validates all loaded data — raises RuntimeError if critical data is missing.
    """

    # --- 1. Load keywords/chats document ---
    fs = FirestoreMagic("telegram", "keywords")
    load_firejson = fs.load_firejson()

    if not load_firejson:
        logger.critical("FATAL: 'keywords' document is empty or missing in Firestore!")
        raise RuntimeError("Cannot continue without keywords/chats configuration.")

    fire_keywords = fs.unpack_array_to_csv_string(load_firejson, "word")
    fire_chats = fs.unpack_array_to_csv_string(load_firejson, "chats")

    if not fire_keywords:
        logger.critical("FATAL: No keywords found in Firestore 'keywords' document (field 'word').")
        raise RuntimeError("Keywords list is empty.")
    if not fire_chats:
        logger.critical("FATAL: No target chats found in Firestore 'keywords' document (field 'chats').")
        raise RuntimeError("Target chats list is empty.")

    # --- 2. Load cursor_base document ---
    fs1 = FirestoreMagic("telegram", "cursor_base")
    previous_checked_ids = fs1.load_firejson()

    if previous_checked_ids is None:
        logger.warning("No 'cursor_base' document found. Starting with empty state.")
        previous_checked_ids = {}
    elif not isinstance(previous_checked_ids, dict):
        logger.critical("FATAL: 'cursor_base' is not a dict. Got type: %s", type(previous_checked_ids))
        raise RuntimeError("cursor_base document is corrupt.")

    # --- 3. Build username→ID lookup ---
    known_usernames_to_ids = {}
    if previous_checked_ids:
        try:
            known_usernames_to_ids = {
                values[0]: key
                for key, values in previous_checked_ids.items()
            }
        except (IndexError, TypeError) as e:
            logger.error("Database cursor_base is corrupt: %s. Rebuilding from scratch.", e)
            known_usernames_to_ids = {}
            previous_checked_ids = {}

    logger.info("Loaded %d known chats from database.", len(known_usernames_to_ids))

    # --- 3b. Load-time cursor field validation ---
    suspicious_entries: list[tuple[str, str]] = []
    for key, values in previous_checked_ids.items():
        if not isinstance(values, (list, tuple)) or len(values) < 2:
            suspicious_entries.append(
                (str(key), f"malformed structure: {type(values).__name__} (len={len(values) if hasattr(values, '__len__') else '?'})")
            )
            continue
        cursor_val = values[1]
        if not isinstance(cursor_val, int):
            suspicious_entries.append(
                (str(key), f"non-integer cursor: {type(cursor_val).__name__} = {cursor_val!r}")
            )
        elif cursor_val < 0:
            suspicious_entries.append((str(key), f"negative cursor: {cursor_val}"))
        elif cursor_val > 2_147_483_647:  # max Telegram message ID (2³¹ − 1)
            suspicious_entries.append((str(key), f"suspiciously large cursor: {cursor_val}"))

    if suspicious_entries:
        logger.warning(
            "Load-time validation found %d suspicious cursor entr%s:",
            len(suspicious_entries),
            "y" if len(suspicious_entries) == 1 else "ies"
        )
        for chat_id, reason in suspicious_entries:
            logger.warning("  Chat %s: %s", chat_id, reason)

    # --- 4. Convert CSV strings to lists ---
    TARGET_CHATS_LIST = [
        chat.strip()
        for chat in fire_chats.split(',')
        if chat.strip()
    ]

    KEYWORDS_LIST = [
        kw.strip()
        for kw in fire_keywords.split(',')
        if kw.strip()
    ]

    logger.info("Configuration ready: %d keywords, %d target chats.",
                len(KEYWORDS_LIST), len(TARGET_CHATS_LIST))

    return KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids