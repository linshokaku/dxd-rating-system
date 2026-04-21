from dataclasses import dataclass
from datetime import timedelta

from dxd_rating.platform.db.models import MatchFormat

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
    minimum_rating: float | None = None
    maximum_rating: float | None = None


@dataclass(frozen=True, slots=True)
class MatchTimingWindows:
    parent_selection_window: timedelta
    report_open_delay: timedelta
    report_deadline_delay: timedelta
    approval_window: timedelta


MATCH_FORMAT_DEFINITIONS = (
    MatchFormatDefinition(
        match_format=MatchFormat.ONE_VS_ONE,
        description="1v1",
        team_size=1,
        batch_size=1,
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
REGULAR_QUEUE_BASELINE_RATING = 1600.0
MATCH_QUEUE_NAME_ALIASES = {
    "begginer": "beginner",
}


def normalize_match_queue_name(queue_name: str) -> str:
    normalized_queue_name = queue_name.strip().casefold()
    return MATCH_QUEUE_NAME_ALIASES.get(normalized_queue_name, normalized_queue_name)


# Add new queue classes in the intended UI display order for each format.
MATCH_QUEUE_CLASS_DEFINITIONS = (
    MatchQueueClassDefinition(
        match_format=MatchFormat.ONE_VS_ONE,
        queue_class_id="1v1_open_beginner",
        queue_name="beginner",
        description="1v1 レート 1600 未満向けキュー",
        maximum_rating=REGULAR_QUEUE_BASELINE_RATING,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.ONE_VS_ONE,
        queue_class_id="1v1_open_regular",
        queue_name="regular",
        description="1v1 全レート参加可能キュー",
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.ONE_VS_ONE,
        queue_class_id="1v1_open_master",
        queue_name="master",
        description="1v1 レート 1600 以上向けキュー",
        minimum_rating=REGULAR_QUEUE_BASELINE_RATING,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.TWO_VS_TWO,
        queue_class_id="2v2_open_beginner",
        queue_name="beginner",
        description="2v2 レート 1600 未満向けキュー",
        maximum_rating=REGULAR_QUEUE_BASELINE_RATING,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.TWO_VS_TWO,
        queue_class_id="2v2_open_regular",
        queue_name="regular",
        description="2v2 全レート参加可能キュー",
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.TWO_VS_TWO,
        queue_class_id="2v2_open_master",
        queue_name="master",
        description="2v2 レート 1600 以上向けキュー",
        minimum_rating=REGULAR_QUEUE_BASELINE_RATING,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.THREE_VS_THREE,
        queue_class_id="3v3_open_beginner",
        queue_name="beginner",
        description="3v3 レート 1600 未満向けキュー",
        maximum_rating=REGULAR_QUEUE_BASELINE_RATING,
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.THREE_VS_THREE,
        queue_class_id="3v3_open_regular",
        queue_name="regular",
        description="3v3 全レート参加可能キュー",
    ),
    MatchQueueClassDefinition(
        match_format=MatchFormat.THREE_VS_THREE,
        queue_class_id="3v3_open_master",
        queue_name="master",
        description="3v3 レート 1600 以上向けキュー",
        minimum_rating=REGULAR_QUEUE_BASELINE_RATING,
    ),
)


def _build_match_queue_name_choices() -> tuple[str, ...]:
    queue_name_choices: list[str] = []
    seen_queue_names: set[str] = set()
    for definition in MATCH_QUEUE_CLASS_DEFINITIONS:
        normalized_queue_name = normalize_match_queue_name(definition.queue_name)
        if normalized_queue_name in seen_queue_names:
            continue
        queue_name_choices.append(definition.queue_name)
        seen_queue_names.add(normalized_queue_name)
    return tuple(queue_name_choices)


MATCH_QUEUE_NAME_CHOICES = _build_match_queue_name_choices()
_MATCH_FORMAT_DEFINITIONS_BY_FORMAT = {
    definition.match_format: definition for definition in MATCH_FORMAT_DEFINITIONS
}

_MATCH_QUEUE_CLASS_DEFINITIONS_BY_ID = {
    definition.queue_class_id: definition for definition in MATCH_QUEUE_CLASS_DEFINITIONS
}
_MATCH_QUEUE_CLASS_DEFINITIONS_BY_NORMALIZED_KEY = {
    (definition.match_format, normalize_match_queue_name(definition.queue_name)): definition
    for definition in MATCH_QUEUE_CLASS_DEFINITIONS
}

MATCH_QUEUE_TTL = timedelta(minutes=30)
PRESENCE_REMINDER_LEAD_TIME = timedelta(minutes=5)
PRODUCTION_MATCH_TIMING_WINDOWS = MatchTimingWindows(
    parent_selection_window=timedelta(minutes=5),
    report_open_delay=timedelta(minutes=7),
    report_deadline_delay=timedelta(minutes=27),
    approval_window=timedelta(minutes=5),
)
DEVELOPMENT_MATCH_TIMING_WINDOWS = MatchTimingWindows(
    parent_selection_window=timedelta(minutes=1),
    report_open_delay=timedelta(minutes=0),
    report_deadline_delay=timedelta(minutes=4),
    approval_window=timedelta(minutes=1),
)


def resolve_match_timing_windows(development_mode: bool) -> MatchTimingWindows:
    if development_mode:
        return DEVELOPMENT_MATCH_TIMING_WINDOWS
    return PRODUCTION_MATCH_TIMING_WINDOWS


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
        (match_format, normalize_match_queue_name(queue_name))
    )
