from dataclasses import dataclass
from datetime import timedelta

OUTBOX_NOTIFY_CHANNEL = "outbox_events"
DUMMY_DISCORD_USER_ID_MIN = 1
DUMMY_DISCORD_USER_ID_MAX = 1000


@dataclass(frozen=True, slots=True)
class MatchQueueClassDefinition:
    queue_class_id: str
    queue_name: str
    description: str
    target_rating: float | None = None


# Add new queue classes by inserting them in low -> high order.
# Once target_rating-based restrictions are enabled, keep these strictly increasing.
MATCH_QUEUE_CLASS_DEFINITIONS = (
    MatchQueueClassDefinition(
        queue_class_id="open_low",
        queue_name="low",
        description="レート下限無制限キュー",
        target_rating=None,
    ),
    MatchQueueClassDefinition(
        queue_class_id="open_high",
        queue_name="high",
        description="レート上限無制限キュー",
        target_rating=None,
    ),
)
MATCH_QUEUE_NAME_CHOICES = tuple(
    definition.queue_name for definition in MATCH_QUEUE_CLASS_DEFINITIONS
)

_MATCH_QUEUE_CLASS_DEFINITIONS_BY_ID = {
    definition.queue_class_id: definition for definition in MATCH_QUEUE_CLASS_DEFINITIONS
}
_MATCH_QUEUE_CLASS_DEFINITIONS_BY_NORMALIZED_NAME = {
    definition.queue_name.casefold(): definition for definition in MATCH_QUEUE_CLASS_DEFINITIONS
}

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


def get_match_queue_class_definitions() -> tuple[MatchQueueClassDefinition, ...]:
    return MATCH_QUEUE_CLASS_DEFINITIONS


def get_match_queue_class_definition_by_id(
    queue_class_id: str,
) -> MatchQueueClassDefinition | None:
    return _MATCH_QUEUE_CLASS_DEFINITIONS_BY_ID.get(queue_class_id)


def get_match_queue_class_definition_by_name(
    queue_name: str,
) -> MatchQueueClassDefinition | None:
    return _MATCH_QUEUE_CLASS_DEFINITIONS_BY_NORMALIZED_NAME.get(queue_name.strip().casefold())
