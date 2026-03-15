OUTBOX_NOTIFY_CHANNEL = "outbox_events"
DUMMY_DISCORD_USER_ID_MIN = 1
DUMMY_DISCORD_USER_ID_MAX = 1000


def is_dummy_discord_user_id(discord_user_id: int) -> bool:
    return DUMMY_DISCORD_USER_ID_MIN <= discord_user_id <= DUMMY_DISCORD_USER_ID_MAX
