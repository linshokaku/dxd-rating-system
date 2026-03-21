from dataclasses import dataclass
from datetime import timedelta

from bot.models import MatchFormat

OUTBOX_NOTIFY_CHANNEL = "outbox_events"
DUMMY_DISCORD_USER_ID_MIN = 1
DUMMY_DISCORD_USER_ID_MAX = 1000


@dataclass(frozen=True, slots=True)
class MatchFormatDefinition:
    match_format: MatchFormat
    description: str
    team_size: int
    batch_size: int

    @property
    def players_per_batch(self) -> int:
        return self.team_size * 2 * self.batch_size


@dataclass(frozen=True, slots=True)
class MatchQueueClassDefinition:
    match_format: MatchFormat
    queue_class_id: str
    queue_name: str
    description: str
    target_rating: float | None = None


MATCH_FORMAT_DEFINITIONS = (
    MatchFormatDefinition(
        match_format=MatchFormat.ONE_VS_ONE,
        description="1v1",
        team_size=1,
        batch_size=2,
    ),
    MatchFormatDefinition(
        match_format=MatchFormat.TWO_VS_TWO,
        description="2v2",
        team_size=2,
        batch_size=1,
    ),
    MatchFormatDefinition(
        match_format=MatchFormat.THREE_VS_THREE,
        description="3v3",
        team_size=3,
        batch_size=1,
    ),
)
MATCH_FORMAT_CHOICES = tuple(
    definition.match_format.value for definition in MATCH_FORMAT_DEFINITIONS
)

# Add new queue classes by inserting them in low -> high order.
# Once target_rating-based restrictions are enabled, keep these strictly increasing.
MATCH_QUEUE_CLASS_DEFINITIONS = (
    MatchQueueClassDefinition(
        match_format=MatchFormat.ONE_VS_ONE,
        queue_class_id="1v1_open_low",
        queue_name="low",
        description="1v1 レート下限無制限キュー",
        target_rating=None,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.ONE_VS_ONE,
        queue_class_id="1v1_open_high",
        queue_name="high",
        description="1v1 レート上限無制限キュー",
        target_rating=None,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.TWO_VS_TWO,
        queue_class_id="2v2_open_low",
        queue_name="low",
        description="2v2 レート下限無制限キュー",
        target_rating=None,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.TWO_VS_TWO,
        queue_class_id="2v2_open_high",
        queue_name="high",
        description="2v2 レート上限無制限キュー",
        target_rating=None,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.THREE_VS_THREE,
        queue_class_id="3v3_open_low",
        queue_name="low",
        description="3v3 レート下限無制限キュー",
        target_rating=None,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.THREE_VS_THREE,
        queue_class_id="3v3_open_high",
        queue_name="high",
        description="3v3 レート上限無制限キュー",
        target_rating=None,
    ),
)
MATCH_QUEUE_NAME_CHOICES = tuple(
    sorted({definition.queue_name for definition in MATCH_QUEUE_CLASS_DEFINITIONS})
)
_MATCH_FORMAT_DEFINITIONS_BY_FORMAT = {
    definition.match_format: definition for definition in MATCH_FORMAT_DEFINITIONS
}

_MATCH_QUEUE_CLASS_DEFINITIONS_BY_ID = {
    definition.queue_class_id: definition for definition in MATCH_QUEUE_CLASS_DEFINITIONS
}
_MATCH_QUEUE_CLASS_DEFINITIONS_BY_NORMALIZED_KEY = {
    (definition.match_format, definition.queue_name.casefold()): definition
    for definition in MATCH_QUEUE_CLASS_DEFINITIONS
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


def get_match_format_definitions() -> tuple[MatchFormatDefinition, ...]:
    return MATCH_FORMAT_DEFINITIONS


def get_match_format_definition(match_format: MatchFormat) -> MatchFormatDefinition | None:
    return _MATCH_FORMAT_DEFINITIONS_BY_FORMAT.get(match_format)


def get_match_queue_class_definition_by_id(
    queue_class_id: str,
) -> MatchQueueClassDefinition | None:
    return _MATCH_QUEUE_CLASS_DEFINITIONS_BY_ID.get(queue_class_id)


def get_match_queue_class_definition_by_name(
    match_format: MatchFormat,
    queue_name: str,
) -> MatchQueueClassDefinition | None:
    return _MATCH_QUEUE_CLASS_DEFINITIONS_BY_NORMALIZED_KEY.get(
        (match_format, queue_name.strip().casefold())
    )
