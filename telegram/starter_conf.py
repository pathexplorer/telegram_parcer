from gcp_actions.firestore_box.json_manipulations import FirestoreMagic
import logging
logger = logging.getLogger(__name__)

def forming_configuration():

    fs = FirestoreMagic( "telegram","keywords")
    load_firejson = fs.load_firejson()

    fire_keywords = fs.unpack_array_to_csv_string(load_firejson, "word")
    fire_chats = fs.unpack_array_to_csv_string(load_firejson, "chats")

    fs1 = FirestoreMagic( "telegram","cursor_base")
    previous_checked_ids = fs1.load_firejson()
    """ Return: nested dict { '12345' : [ '@name' , 11 ], '67890' : [ '@name' , 22 ] } """

    try:
        known_usernames_to_ids = {
            values[0]: key for key, values in previous_checked_ids.items()
        }
    except IndexError:
        logging.error("Database is corrupt. Rebuilding.")
        known_usernames_to_ids = {}
        # You might to clear previous_checked_ids here

    logging.info(f"Loaded {len(known_usernames_to_ids)} known chats from database.")

    TARGET_CHATS_LIST = [
        chat.strip()
        for chat in fire_chats.split(',')
        if chat.strip()  # This ignores empty strings that result from trailing commas
    ]
    """ Convert string to list """

    KEYWORDS_LIST = [
        chat.strip()
        for chat in fire_keywords.split(',')
        if chat.strip()  # This ignores empty strings that result from trailing commas
    ]
    """ Convert string to list """

    return KEYWORDS_LIST, TARGET_CHATS_LIST, previous_checked_ids, known_usernames_to_ids