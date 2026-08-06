import logging
import unicodedata

from gcp_actions.firestore_box.json_manipulations import FirestoreMagic

logger = logging.getLogger(__name__)

# Current cursor schema version.  Bump when the cursor format changes.
# Written into each cursor entry so future code can identify the format.
CURSOR_SCHEMA_VERSION = 1


def _migrate_cursor_to_typed(previous_checked_ids: dict) -> int:
    """Convert legacy positional-list cursors to typed dicts in place.

    Legacy format:  [ref_str, last_message_id_int, alerted_csv_str]
    New format:     {"ref": str, "last_processed_id": int,
                     "alerted_keys": str, "schema_version": 1}

    Returns the number of entries migrated.
    """
    migrated = 0
    for key, values in previous_checked_ids.items():
        if isinstance(values, dict):
            # Already typed — ensure schema_version is set
            if "schema_version" not in values:
                values["schema_version"] = CURSOR_SCHEMA_VERSION
            continue
        if not isinstance(values, (list, tuple)):
            continue  # corrupt entry, leave for validation

        ref = str(values[0]) if len(values) >= 1 else ""
        last_id = int(values[1]) if len(values) >= 2 and isinstance(values[1], int) else 0
        alerted = ""
        if len(values) >= 3:
            alerted = str(values[2]) if isinstance(values[2], str) else ",".join(
                str(k) for k in values[2] if k
            ) if isinstance(values[2], list) else ""

        previous_checked_ids[key] = {
            "ref": ref,
            "last_processed_id": last_id,
            "alerted_keys": alerted,
            "schema_version": CURSOR_SCHEMA_VERSION,
        }
        migrated += 1

    return migrated


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

    # --- 3. Migrate legacy formats BEFORE validation -----------------------
    # Step 3a: Migrate nested-array alerted entries to CSV string (legacy v1→v2)
    nested_migrated = 0
    for key, values in previous_checked_ids.items():
        if isinstance(values, list) and len(values) >= 3 and isinstance(values[2], list):
            values[2] = ",".join(str(k) for k in values[2] if k)
            nested_migrated += 1
    if nested_migrated:
        logger.warning("Migrated %d legacy nested-array alerted entries to CSV format.", nested_migrated)

    # Step 3b: Migrate positional-list cursors to typed dicts (legacy → v3)
    typed_migrated = _migrate_cursor_to_typed(previous_checked_ids)
    if typed_migrated:
        logger.warning(
            "Migrated %d legacy positional-list cursor(s) to typed-dict format (schema_version=%d).",
            typed_migrated, CURSOR_SCHEMA_VERSION,
        )

    # --- 4. Build username→ID lookup (typed-dict access) ------------------
    known_usernames_to_ids = {}
    if previous_checked_ids:
        try:
            known_usernames_to_ids = {
                values["ref"]: key
                for key, values in previous_checked_ids.items()
            }
        except (KeyError, TypeError) as e:
            logger.error("Database cursor_base is corrupt: %s. Rebuilding from scratch.", e)
            known_usernames_to_ids = {}
            previous_checked_ids = {}

    logger.info("Loaded %d known chats from database.", len(known_usernames_to_ids))

    # --- 5. Load-time cursor field validation (typed-dict access) ----------
    suspicious_entries: list[tuple[str, str]] = []
    for key, values in previous_checked_ids.items():
        if not isinstance(values, dict):
            suspicious_entries.append(
                (str(key), f"malformed structure: {type(values).__name__} (expected dict)")
            )
            continue
        cursor_val = values.get("last_processed_id")
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

    # --- 6. Convert CSV strings to lists ----------------------------------
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

    # --- 6b. Normalize keywords for Unicode-aware matching -----------------
    # NFKC normalizes compatibility equivalents (fullwidth, ligatures) into
    # composed canonical forms.  casefold() provides locale-independent
    # case-insensitive comparison (e.g. "ß" → "ss", "İ" → "i̇").
    # Deduplicate after normalization to avoid redundant checks.
    seen: set[str] = set()
    normalized: list[str] = []
    for kw in KEYWORDS_LIST:
        nk = unicodedata.normalize("NFKC", kw).casefold()
        if nk and nk not in seen:
            seen.add(nk)
            normalized.append(nk)
    KEYWORDS_LIST = normalized

    logger.info("Configuration ready: %d keywords (normalized), %d target chats.",
                len(KEYWORDS_LIST), len(TARGET_CHATS_LIST))

    return KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids