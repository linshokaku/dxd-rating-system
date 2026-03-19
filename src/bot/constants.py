from datetime import timedelta

OUTBOX_NOTIFY_CHANNEL = "outbox_events"
DUMMY_DISCORD_USER_ID_MIN = 1
DUMMY_DISCORD_USER_ID_MAX = 1000

MATCH_QUEUE_TTL = timedelta(minutes=5)
PRESENCE_REMINDER_LEAD_TIME = timedelta(minutes=1)
# MATCH_PARENT_SELECTION_WINDOW = timedelta(minutes=5)
# MATCH_REPORT_OPEN_DELAY = timedelta(minutes=7)
# MATCH_REPORT_DEADLINE_DELAY = timedelta(minutes=27)
# MATCH_APPROVAL_WINDOW = timedelta(minutes=5)
MATCH_PARENT_SELECTION_WINDOW = timedelta(minutes=1)
MATCH_REPORT_OPEN_DELAY = timedelta(minutes=0)
MATCH_REPORT_DEADLINE_DELAY = timedelta(minutes=4)
MATCH_APPROVAL_WINDOW = timedelta(minutes=1)


def is_dummy_discord_user_id(discord_user_id: int) -> bool:
    return DUMMY_DISCORD_USER_ID_MIN <= discord_user_id <= DUMMY_DISCORD_USER_ID_MAX


def format_discord_user_mention(discord_user_id: int) -> str:
    if is_dummy_discord_user_id(discord_user_id):
        return f"<dummy_{discord_user_id}>"
    return f"<@{discord_user_id}>"
