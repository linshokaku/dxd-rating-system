from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import discord
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.matches.application import MatchFlowService
from dxd_rating.contexts.matchmaking.application import (
    MatchingQueueNotificationContext,
    MatchingQueueService,
)
from dxd_rating.contexts.players.application import register_player
from dxd_rating.contexts.restrictions.application import (
    PlayerAccessRestrictionDuration,
    PlayerAccessRestrictionService,
)
from dxd_rating.contexts.seasons.application import ensure_active_and_upcoming_seasons
from dxd_rating.contexts.ui.application import (
    REGISTERED_PLAYER_ROLE_NAME,
    get_required_managed_ui_definitions,
)
from dxd_rating.platform.config.bot import BotSettings
from dxd_rating.platform.db.models import (
    ManagedUiChannel,
    ManagedUiType,
    MatchFormat,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchSpectator,
    PenaltyType,
    Player,
    PlayerAccessRestriction,
    PlayerAccessRestrictionType,
    PlayerFormatStats,
    PlayerPenalty,
    Season,
)
from dxd_rating.platform.discord.gateway.commands import BotCommandHandlers, register_app_commands
from dxd_rating.platform.discord.ui import (
    ADMIN_CONTACT_CHANNEL_MESSAGE,
    MATCHMAKING_CHANNEL_JOIN_HIGH_BUTTON_LABEL,
    MATCHMAKING_CHANNEL_JOIN_LOW_BUTTON_LABEL,
    MATCHMAKING_CHANNEL_LEAVE_BUTTON_LABEL,
    MATCHMAKING_CHANNEL_MESSAGE,
    MATCHMAKING_CHANNEL_PRESENT_BUTTON_LABEL,
    MATCHMAKING_NEWS_CHANNEL_MESSAGE,
    REGISTER_PANEL_BUTTON_LABEL,
    REGISTER_PANEL_MESSAGE,
    SYSTEM_ANNOUNCEMENTS_CHANNEL_MESSAGE,
)
from dxd_rating.platform.runtime import MatchRuntime

DEFAULT_MATCH_FORMAT = MatchFormat.THREE_VS_THREE
DEFAULT_QUEUE_NAME = "low"


@dataclass(frozen=True)
class FakeUser:
    id: int
    name: str | None = None
    global_name: str | None = None
    nick: str | None = None

    def __post_init__(self) -> None:
        if self.name is None:
            object.__setattr__(self, "name", f"user-{self.id}")


@dataclass
class FakeMember:
    id: int
    name: str | None = None
    global_name: str | None = None
    nick: str | None = None
    roles: list[FakeRole] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.name is None:
            self.name = f"user-{self.id}"

    async def add_roles(self, *roles: FakeRole, **_: Any) -> None:
        existing_role_ids = {role.id for role in self.roles}
        for role in roles:
            if role.id not in existing_role_ids:
                self.roles.append(role)
                existing_role_ids.add(role.id)


@dataclass
class FakeInteractionResponse:
    messages: list[str] = field(default_factory=list)
    ephemeral_flags: list[bool] = field(default_factory=list)
    deferred: bool = False
    defer_ephemeral: bool | None = None
    defer_thinking: bool | None = None

    async def send_message(self, content: str, *, ephemeral: bool = False, **_: Any) -> None:
        self.deferred = True
        self.messages.append(content)
        self.ephemeral_flags.append(ephemeral)

    async def defer(
        self,
        *,
        ephemeral: bool = False,
        thinking: bool = False,
        **_: Any,
    ) -> None:
        self.deferred = True
        self.defer_ephemeral = ephemeral
        self.defer_thinking = thinking

    def is_done(self) -> bool:
        return self.deferred


@dataclass
class FakeInteractionFollowup:
    response: FakeInteractionResponse

    async def send(self, content: str, *, ephemeral: bool = False, **_: Any) -> None:
        self.response.messages.append(content)
        self.response.ephemeral_flags.append(ephemeral)


@dataclass(frozen=True)
class FakeRole:
    id: int
    name: str = "@everyone"


@dataclass
class FakeGuildMember:
    id: int
    guild_permissions: discord.Permissions = field(
        default_factory=lambda: discord.Permissions(manage_channels=True, manage_roles=True)
    )

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class FakeHttpResponse:
    status: int
    reason: str
    text: str


def make_forbidden() -> discord.Forbidden:
    return discord.Forbidden(
        FakeHttpResponse(status=403, reason="Forbidden", text="Forbidden"),
        "Forbidden",
    )


def make_not_found() -> discord.NotFound:
    return discord.NotFound(
        FakeHttpResponse(status=404, reason="Not Found", text="Not Found"),
        "Not Found",
    )


@dataclass
class FakeMessage:
    id: int
    content: str
    view: discord.ui.View | None = None


@dataclass
class FakeTextChannel:
    id: int
    name: str
    guild: FakeGuild
    overwrites: dict[object, discord.PermissionOverwrite] = field(default_factory=dict)
    sent_messages: list[FakeMessage] = field(default_factory=list)
    fail_send_with: Exception | None = None
    fail_delete_with: Exception | None = None
    deleted: bool = False

    async def send(
        self,
        content: str | None = None,
        *,
        view: discord.ui.View | None = None,
        **_: Any,
    ) -> discord.Message:
        if self.fail_send_with is not None:
            raise self.fail_send_with

        message = FakeMessage(
            id=self.guild.next_message_id,
            content="" if content is None else content,
            view=view,
        )
        self.guild.next_message_id += 1
        self.sent_messages.append(message)
        return cast(discord.Message, message)

    async def delete(self, *_: Any, **__: Any) -> None:
        if self.fail_delete_with is not None:
            raise self.fail_delete_with

        self.deleted = True
        self.guild.channels = [
            existing_channel
            for existing_channel in self.guild.channels
            if existing_channel.id != self.id
        ]


@dataclass
class FakeGuild:
    id: int
    channels: list[FakeTextChannel] = field(default_factory=list)
    default_role: FakeRole = field(default_factory=lambda: FakeRole(id=0))
    roles: list[FakeRole] = field(default_factory=list)
    me: FakeGuildMember | None = None
    next_channel_id: int = 20_001
    next_message_id: int = 30_001
    next_role_id: int = 40_001
    create_channel_error: Exception | None = None
    create_role_error: Exception | None = None
    next_channel_fail_send_with: Exception | None = None
    next_channel_fail_delete_with: Exception | None = None

    def __post_init__(self) -> None:
        if self.me is None:
            self.me = FakeGuildMember(id=999_999)
        if not any(role.id == self.default_role.id for role in self.roles):
            self.roles.insert(0, self.default_role)

    async def create_text_channel(
        self,
        name: str,
        *,
        overwrites: dict[object, discord.PermissionOverwrite] | None = None,
        **_: Any,
    ) -> discord.TextChannel:
        if self.create_channel_error is not None:
            raise self.create_channel_error

        channel = FakeTextChannel(
            id=self.next_channel_id,
            name=name,
            guild=self,
            overwrites={} if overwrites is None else dict(overwrites),
            fail_send_with=self.next_channel_fail_send_with,
            fail_delete_with=self.next_channel_fail_delete_with,
        )
        self.next_channel_id += 1
        self.next_channel_fail_send_with = None
        self.next_channel_fail_delete_with = None
        self.channels.append(channel)
        return cast(discord.TextChannel, channel)

    async def create_role(self, name: str, **_: Any) -> discord.Role:
        if self.create_role_error is not None:
            raise self.create_role_error

        role = FakeRole(id=self.next_role_id, name=name)
        self.next_role_id += 1
        self.roles.append(role)
        return cast(discord.Role, role)

    def get_channel(self, channel_id: int) -> FakeTextChannel | None:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None


@dataclass
class FakeInteraction:
    user: FakeUser
    channel_id: int | None = 1_001
    guild_id: int | None = 2_001
    application_id: int | None = 3_001
    token: str = "interaction-token"
    guild: FakeGuild | None = None
    response: FakeInteractionResponse = field(default_factory=FakeInteractionResponse)
    followup: FakeInteractionFollowup = field(init=False)

    def __post_init__(self) -> None:
        self.followup = FakeInteractionFollowup(response=self.response)


def as_interaction(fake_interaction: FakeInteraction) -> discord.Interaction[discord.Client]:
    return cast(discord.Interaction[discord.Client], fake_interaction)


def assert_response(
    interaction: FakeInteraction,
    expected_messages: list[str],
    *,
    ephemeral: bool,
) -> None:
    assert interaction.response.messages == expected_messages
    assert interaction.response.ephemeral_flags == [ephemeral] * len(expected_messages)


@pytest.fixture(autouse=True)
def prepared_seasons(session: Session) -> None:
    ensure_active_and_upcoming_seasons(session)
    session.commit()


def create_settings(*, super_admin_user_ids: frozenset[int] = frozenset()) -> BotSettings:
    return BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        super_admin_user_ids=super_admin_user_ids,
    )


def create_handlers(
    session_factory: sessionmaker[Session],
    *,
    super_admin_user_ids: frozenset[int] = frozenset(),
    matching_queue_service: MatchingQueueService | MatchRuntime | None = None,
) -> BotCommandHandlers:
    resolved_matching_queue_service = matching_queue_service
    if isinstance(matching_queue_service, MatchingQueueService):
        resolved_matching_queue_service = MatchRuntime(
            service=matching_queue_service,
            match_service=MatchFlowService(session_factory),
        )

    return BotCommandHandlers(
        settings=create_settings(super_admin_user_ids=super_admin_user_ids),
        session_factory=session_factory,
        matching_queue_service=resolved_matching_queue_service,
    )


def create_player(session: Session, discord_user_id: int) -> Player:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player


def get_active_season_id(session: Session) -> int:
    return ensure_active_and_upcoming_seasons(session).active.id


def find_role_by_name(guild: FakeGuild, role_name: str) -> FakeRole | None:
    for role in guild.roles:
        if role.name == role_name:
            return role
    return None


def find_channel_by_name(guild: FakeGuild, channel_name: str) -> FakeTextChannel:
    for channel in guild.channels:
        if channel.name == channel_name:
            return channel
    raise AssertionError(f"Channel not found: {channel_name}")


def get_player_format_stats(
    session: Session,
    player_id: int,
    match_format: MatchFormat = DEFAULT_MATCH_FORMAT,
) -> PlayerFormatStats:
    season_id = get_active_season_id(session)
    format_stats = session.scalar(
        select(PlayerFormatStats).where(
            PlayerFormatStats.player_id == player_id,
            PlayerFormatStats.season_id == season_id,
            PlayerFormatStats.match_format == match_format,
        )
    )
    assert format_stats is not None
    return format_stats


def format_player_info_message(
    stats_by_format: dict[MatchFormat, tuple[float, int, int, int, int, datetime | None]],
    *,
    season_id: int | None = None,
    season_name: str | None = None,
) -> str:
    lines = ["プレイヤー情報"]
    if season_id is not None:
        lines.extend(
            [
                f"season_id: {season_id}",
                f"season_name: {season_name}",
            ]
        )
    for match_format in (
        MatchFormat.ONE_VS_ONE,
        MatchFormat.TWO_VS_TWO,
        MatchFormat.THREE_VS_THREE,
    ):
        rating, games_played, wins, losses, draws, last_played_at = stats_by_format[match_format]
        lines.extend(
            [
                match_format.value,
                f"rating: {rating:.2f}",
                f"games_played: {games_played}",
                f"wins: {wins}",
                f"losses: {losses}",
                f"draws: {draws}",
                f"last_played_at: {'-' if last_played_at is None else last_played_at.isoformat()}",
            ]
        )
    return "\n".join(lines)


def get_queue_entry(session: Session, player_id: int) -> MatchQueueEntry:
    session.expire_all()
    queue_entry = session.scalar(
        select(MatchQueueEntry).where(MatchQueueEntry.player_id == player_id)
    )
    assert queue_entry is not None
    return queue_entry


def create_match(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> tuple[int, list[Player]]:
    players = [create_player(session, start_discord_user_id + offset) for offset in range(6)]
    queue_service = MatchingQueueService(session_factory)
    for player in players:
        queue_service.join_queue(
            player.id,
            DEFAULT_MATCH_FORMAT,
            DEFAULT_QUEUE_NAME,
            notification_context=MatchingQueueNotificationContext(
                channel_id=channel_id,
                guild_id=guild_id,
                mention_discord_user_id=player.discord_user_id,
            ),
        )

    created_matches = queue_service.try_create_matches()

    assert len(created_matches) == 1
    return created_matches[0].match_id, players


def test_register_command_registers_requesting_user(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_678
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.register(as_interaction(interaction)))

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == interaction.user.id)
    )

    assert_response(interaction, ["登録が完了しました。"], ephemeral=True)
    assert persisted_player is not None
    assert persisted_player.display_name == f"user-{discord_user_id}"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_register_command_returns_duplicate_message_for_registered_user(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_679
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.register(as_interaction(interaction)))

    assert_response(interaction, ["すでに登録済みです。"], ephemeral=True)
    session.expire_all()
    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )
    assert persisted_player is not None
    assert persisted_player.display_name == f"user-{discord_user_id}"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_register_command_returns_internal_error_message_when_seasons_are_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    session.execute(delete(Season))
    session.commit()
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_679_1))

    asyncio.run(handlers.register(as_interaction(interaction)))

    assert_response(
        interaction,
        ["登録に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_join_command_joins_requesting_player_and_stores_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_680
    player = create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(
        user=FakeUser(
            id=discord_user_id,
            name="queue-user",
            global_name="queue-global",
            nick="queue-guild",
        ),
        channel_id=3_001,
        guild_id=4_001,
    )

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    queue_entry = get_queue_entry(session, player.id)
    session.expire_all()
    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )

    assert_response(
        interaction,
        ["キューに参加しました。5分間マッチングします。"],
        ephemeral=True,
    )
    assert queue_entry.notification_channel_id == 3_001
    assert queue_entry.notification_guild_id == 4_001
    assert queue_entry.notification_dm_discord_user_id is None
    assert queue_entry.notification_interaction_application_id is None
    assert queue_entry.notification_interaction_token is None
    assert queue_entry.notification_mention_discord_user_id == discord_user_id
    assert persisted_player is not None
    assert persisted_player.display_name == "queue-guild"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_join_command_requires_registered_player(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_681))

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


def test_join_command_returns_internal_error_message_when_seasons_are_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_2
    create_player(session, discord_user_id)
    session.execute(delete(PlayerFormatStats))
    session.execute(delete(Season))
    session.commit()
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    assert_response(
        interaction,
        ["キュー参加に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_join_command_returns_restricted_message_for_queue_join_restricted_player(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_1
    player = create_player(session, discord_user_id)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        player.id,
        PlayerAccessRestrictionType.QUEUE_JOIN,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=99_001,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    assert_response(interaction, ["現在キュー参加を制限されています。"], ephemeral=True)


def test_present_command_updates_waiting_entry_and_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_682
    player = create_player(session, discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(
        player.id,
        DEFAULT_MATCH_FORMAT,
        DEFAULT_QUEUE_NAME,
        notification_context=MatchingQueueNotificationContext(
            channel_id=5_001,
            guild_id=6_001,
            mention_discord_user_id=7_001,
        ),
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=matching_queue_service,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=8_001,
        guild_id=9_001,
    )

    asyncio.run(handlers.present(as_interaction(interaction)))

    queue_entry = get_queue_entry(session, player.id)

    assert_response(
        interaction,
        ["在席を更新しました。次の期限は5分後です。"],
        ephemeral=True,
    )
    assert queue_entry.notification_channel_id == 8_001
    assert queue_entry.notification_guild_id == 9_001
    assert queue_entry.notification_dm_discord_user_id is None
    assert queue_entry.notification_interaction_application_id is None
    assert queue_entry.notification_interaction_token is None
    assert queue_entry.notification_mention_discord_user_id == discord_user_id


def test_present_command_returns_not_joined_message_for_non_waiting_player(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_683
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.present(as_interaction(interaction)))

    assert_response(interaction, ["キューに参加していません。"], ephemeral=True)


def test_leave_command_is_idempotent_for_registered_player_without_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_684
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.leave(as_interaction(interaction)))

    assert_response(interaction, ["キューから退出しました。"], ephemeral=True)


def test_player_info_command_returns_requesting_player_stats(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_686
    player = create_player(session, discord_user_id)
    three_vs_three_stats = get_player_format_stats(session, player.id)
    three_vs_three_stats.rating = 1512.5
    three_vs_three_stats.games_played = 8
    three_vs_three_stats.wins = 5
    three_vs_three_stats.losses = 2
    three_vs_three_stats.draws = 1
    three_vs_three_stats.last_played_at = datetime(2026, 3, 20, 12, 34, 56, tzinfo=timezone.utc)
    session.commit()
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(
        interaction,
        [
            format_player_info_message(
                {
                    MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                    MatchFormat.TWO_VS_TWO: (1500.0, 0, 0, 0, 0, None),
                    MatchFormat.THREE_VS_THREE: (
                        1512.5,
                        8,
                        5,
                        2,
                        1,
                        datetime(2026, 3, 20, 12, 34, 56, tzinfo=timezone.utc),
                    ),
                }
            )
        ],
        ephemeral=True,
    )


def test_player_info_season_command_returns_requested_season_stats(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_686_1
    player = create_player(session, discord_user_id)
    season_pair = ensure_active_and_upcoming_seasons(session)
    season_pair.upcoming.name = "next-spring"
    upcoming_three_vs_three_stats = session.scalar(
        select(PlayerFormatStats).where(
            PlayerFormatStats.player_id == player.id,
            PlayerFormatStats.season_id == season_pair.upcoming.id,
            PlayerFormatStats.match_format == MatchFormat.THREE_VS_THREE,
        )
    )
    assert upcoming_three_vs_three_stats is not None
    upcoming_three_vs_three_stats.rating = 1601.0
    upcoming_three_vs_three_stats.games_played = 4
    upcoming_three_vs_three_stats.wins = 3
    upcoming_three_vs_three_stats.losses = 1
    session.commit()
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.player_info_season(as_interaction(interaction), season_pair.upcoming.id))

    assert_response(
        interaction,
        [
            format_player_info_message(
                {
                    MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                    MatchFormat.TWO_VS_TWO: (1500.0, 0, 0, 0, 0, None),
                    MatchFormat.THREE_VS_THREE: (1601.0, 4, 3, 1, 0, None),
                },
                season_id=season_pair.upcoming.id,
                season_name="next-spring",
            )
        ],
        ephemeral=True,
    )


def test_player_info_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_687))

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


def test_player_info_command_returns_internal_error_message_when_seasons_are_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_687_1
    create_player(session, discord_user_id)
    session.execute(delete(PlayerFormatStats))
    session.execute(delete(Season))
    session.commit()
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(
        interaction,
        ["プレイヤー情報の取得に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_match_spectate_command_registers_requesting_player_as_spectator(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_700,
        channel_id=13_001,
        guild_id=14_001,
    )
    spectator_discord_user_id = 123_456_789_012_345_706
    spectator = create_player(session, spectator_discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=spectator_discord_user_id))

    asyncio.run(handlers.match_spectate(as_interaction(interaction), match_id))

    persisted_spectator = session.scalar(
        select(MatchSpectator).where(
            MatchSpectator.match_id == match_id,
            MatchSpectator.player_id == spectator.id,
        )
    )

    assert interaction.response.messages == ["観戦応募を受け付けました。現在 1 / 6 人です。"]
    assert persisted_spectator is not None


def test_match_spectate_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_707))

    asyncio.run(handlers.match_spectate(as_interaction(interaction), 1))

    assert interaction.response.messages == [
        "プレイヤー登録が必要です。先に /register を実行してください。"
    ]


def test_match_spectate_command_returns_restricted_message_for_restricted_player(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_707_1,
        channel_id=13_002,
        guild_id=14_002,
    )
    spectator_discord_user_id = 123_456_789_012_345_713
    spectator = create_player(session, spectator_discord_user_id)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        spectator.id,
        PlayerAccessRestrictionType.SPECTATE,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=99_002,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=spectator_discord_user_id))

    asyncio.run(handlers.match_spectate(as_interaction(interaction), match_id))

    assert interaction.response.messages == ["現在観戦を制限されています。"]


def test_dev_match_spectate_registers_target_dummy_user_as_spectator(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_710,
        channel_id=13_010,
        guild_id=14_010,
    )
    spectator = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.dev_match_spectate(
            as_interaction(interaction),
            match_id,
            str(target_discord_user_id),
        )
    )

    persisted_spectator = session.scalar(
        select(MatchSpectator).where(
            MatchSpectator.match_id == match_id,
            MatchSpectator.player_id == spectator.id,
        )
    )

    assert interaction.response.messages == ["指定したユーザーの観戦応募を受け付けました。"]
    assert persisted_spectator is not None


def test_dev_match_spectate_returns_restricted_message_for_restricted_target(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 778
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_711,
        channel_id=13_011,
        guild_id=14_011,
    )
    spectator = create_player(session, target_discord_user_id)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        spectator.id,
        PlayerAccessRestrictionType.SPECTATE,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=10,
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.dev_match_spectate(
            as_interaction(interaction),
            match_id,
            str(target_discord_user_id),
        )
    )

    assert interaction.response.messages == ["指定したユーザーは現在観戦を制限されています。"]


def test_dev_register_requires_admin(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "123456789012345685"))

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == 123_456_789_012_345_685)
    )

    assert interaction.response.messages == ["このコマンドは管理者のみ実行できます。"]
    assert persisted_player is None


def test_dev_register_sets_fixed_dummy_display_name(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "777"))

    persisted_player = session.scalar(select(Player).where(Player.discord_user_id == 777))

    assert interaction.response.messages == ["ダミーユーザーを登録しました。"]
    assert persisted_player is not None
    assert persisted_player.display_name == "<dummy_777>"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_dev_register_validates_discord_user_id(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "not-a-number"))

    assert interaction.response.messages == ["discord_user_id が不正です。"]


def test_dev_register_rejects_non_dummy_discord_user_id(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "1001"))

    assert interaction.response.messages == ["discord_user_id が不正です。"]


def test_dev_join_targets_provided_user_and_uses_target_for_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=11_001,
        guild_id=12_001,
    )

    asyncio.run(
        handlers.dev_join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
            str(target_discord_user_id),
        )
    )

    queue_entry = get_queue_entry(session, player.id)

    assert_response(interaction, ["指定したユーザーをキューに参加させました。"], ephemeral=False)
    assert queue_entry.notification_channel_id == 11_001
    assert queue_entry.notification_guild_id == 12_001
    assert queue_entry.notification_dm_discord_user_id is None
    assert queue_entry.notification_interaction_application_id is None
    assert queue_entry.notification_interaction_token is None
    assert queue_entry.notification_mention_discord_user_id == target_discord_user_id


def test_dev_join_returns_restricted_message_for_restricted_target(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 779
    player = create_player(session, target_discord_user_id)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        player.id,
        PlayerAccessRestrictionType.QUEUE_JOIN,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=10,
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.dev_join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
            str(target_discord_user_id),
        )
    )

    assert interaction.response.messages == ["指定したユーザーは現在キュー参加を制限されています。"]


def test_dev_present_returns_expired_message_for_expired_target(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_687
    player = create_player(session, target_discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)
    queue_entry = get_queue_entry(session, player.id)
    queue_entry.expire_at = queue_entry.joined_at - timedelta(seconds=1)
    session.commit()

    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=matching_queue_service,
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(handlers.dev_present(as_interaction(interaction), str(target_discord_user_id)))

    assert interaction.response.messages == [
        "指定したユーザーは期限切れのためキューから外れました。"
    ]


def test_dev_leave_returns_target_not_registered_message(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_leave(as_interaction(interaction), "123456789012345688"))

    assert interaction.response.messages == ["指定したユーザーは未登録です。"]


def test_dev_player_info_requires_admin(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    create_player(session, 123_456_789_012_345_689)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "123456789012345689"))

    assert interaction.response.messages == ["このコマンドは管理者のみ実行できます。"]


def test_dev_player_info_validates_discord_user_id(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "not-a-number"))

    assert interaction.response.messages == ["discord_user_id が不正です。"]


def test_dev_player_info_returns_target_player_stats(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690
    player = create_player(session, target_discord_user_id)
    three_vs_three_stats = get_player_format_stats(session, player.id)
    three_vs_three_stats.rating = 1498.25
    three_vs_three_stats.games_played = 3
    three_vs_three_stats.wins = 1
    three_vs_three_stats.losses = 1
    three_vs_three_stats.draws = 1
    three_vs_three_stats.last_played_at = datetime(2026, 3, 20, 14, 0, 0, tzinfo=timezone.utc)
    session.commit()
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), str(target_discord_user_id)))

    assert interaction.response.messages == [
        format_player_info_message(
            {
                MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.TWO_VS_TWO: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.THREE_VS_THREE: (
                    1498.25,
                    3,
                    1,
                    1,
                    1,
                    datetime(2026, 3, 20, 14, 0, 0, tzinfo=timezone.utc),
                ),
            }
        )
    ]


def test_dev_player_info_season_returns_target_player_stats(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690_1
    player = create_player(session, target_discord_user_id)
    season_pair = ensure_active_and_upcoming_seasons(session)
    season_pair.upcoming.name = "next-summer"
    upcoming_three_vs_three_stats = session.scalar(
        select(PlayerFormatStats).where(
            PlayerFormatStats.player_id == player.id,
            PlayerFormatStats.season_id == season_pair.upcoming.id,
            PlayerFormatStats.match_format == MatchFormat.THREE_VS_THREE,
        )
    )
    assert upcoming_three_vs_three_stats is not None
    upcoming_three_vs_three_stats.rating = 1488.0
    session.commit()
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.dev_player_info_season(
            as_interaction(interaction),
            season_pair.upcoming.id,
            str(target_discord_user_id),
        )
    )

    assert interaction.response.messages == [
        format_player_info_message(
            {
                MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.TWO_VS_TWO: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.THREE_VS_THREE: (1488.0, 0, 0, 0, 0, None),
            },
            season_id=season_pair.upcoming.id,
            season_name="next-summer",
        )
    ]


def test_dev_player_info_returns_target_not_registered_message(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "123456789012345691"))

    assert interaction.response.messages == ["指定したユーザーは未登録です。"]


def test_dev_is_admin_returns_yes_or_no(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    admin_interaction = FakeInteraction(user=FakeUser(id=10))
    non_admin_interaction = FakeInteraction(user=FakeUser(id=20))

    asyncio.run(handlers.dev_is_admin(as_interaction(admin_interaction)))
    asyncio.run(handlers.dev_is_admin(as_interaction(non_admin_interaction)))

    assert admin_interaction.response.messages == ["はい"]
    assert non_admin_interaction.response.messages == ["いいえ"]


def test_admin_rename_season_updates_target_season_name(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    create_player(session, 123_456_789_012_345_695)
    season_pair = ensure_active_and_upcoming_seasons(session)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.admin_rename_season(
            as_interaction(interaction),
            season_pair.upcoming.id,
            "spring-cup",
        )
    )

    session.expire_all()
    refreshed_pair = ensure_active_and_upcoming_seasons(session)

    assert interaction.response.messages == ["シーズン名を変更しました。"]
    assert refreshed_pair.upcoming.id == season_pair.upcoming.id
    assert refreshed_pair.upcoming.name == "spring-cup"


def test_admin_setup_custom_ui_channel_creates_register_panel_and_button_registers_player(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_800
    guild = FakeGuild(id=2_100)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(interaction),
            ManagedUiType.REGISTER_PANEL.value,
            "レート戦はこちらから",
        )
    )

    session.expire_all()
    managed_ui_channel = session.scalar(select(ManagedUiChannel))
    persisted_channel = guild.channels[0]
    persisted_message = persisted_channel.sent_messages[0]

    assert interaction.response.messages == ["UI 設置チャンネルを作成しました。"]
    assert interaction.response.ephemeral_flags == [True]
    assert interaction.response.defer_ephemeral is True
    assert interaction.response.defer_thinking is True
    assert managed_ui_channel is not None
    assert managed_ui_channel.ui_type == ManagedUiType.REGISTER_PANEL
    assert managed_ui_channel.channel_id == persisted_channel.id
    assert managed_ui_channel.message_id == persisted_message.id
    assert managed_ui_channel.created_by_discord_user_id == executor_discord_user_id
    assert persisted_channel.overwrites[guild.default_role].view_channel is True
    assert persisted_channel.overwrites[guild.default_role].send_messages is False
    assert persisted_message.content == REGISTER_PANEL_MESSAGE
    assert persisted_message.view is not None
    button = cast(discord.ui.Button[Any], persisted_message.view.children[0])
    assert button.label == REGISTER_PANEL_BUTTON_LABEL

    button_interaction = FakeInteraction(
        user=FakeUser(id=target_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )
    asyncio.run(button.callback(as_interaction(button_interaction)))

    session.expire_all()
    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == target_discord_user_id)
    )

    assert button_interaction.response.messages == ["登録が完了しました。"]
    assert button_interaction.response.ephemeral_flags == [True]
    assert persisted_player is not None


def test_register_command_assigns_registered_role_when_present(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(id=2_109)
    registered_role = FakeRole(id=55_001, name=REGISTERED_PLAYER_ROLE_NAME)
    guild.roles.append(registered_role)
    member = FakeMember(id=123_456_789_012_345_801)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(
        user=cast(Any, member),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.register(as_interaction(interaction)))

    assert_response(interaction, ["登録が完了しました。"], ephemeral=True)
    assert {role.id for role in member.roles} == {registered_role.id}


def test_admin_setup_custom_ui_channel_rolls_back_created_channel_when_ui_send_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(id=2_101, next_channel_fail_send_with=RuntimeError("boom"))
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(interaction),
            ManagedUiType.REGISTER_PANEL.value,
            "レート戦はこちらから",
        )
    )

    session.expire_all()

    assert interaction.response.messages == [
        "UI 設置チャンネルの作成に失敗しました。管理者に確認してください。"
    ]
    assert interaction.response.ephemeral_flags == [True]
    assert guild.channels == []
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_setup_custom_ui_channel_reports_missing_permissions(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(
        id=2_101_5,
        me=FakeGuildMember(
            id=999_999,
            guild_permissions=discord.Permissions.none(),
        ),
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(interaction),
            ManagedUiType.MATCHMAKING_CHANNEL.value,
            "レート戦マッチング",
        )
    )

    session.expire_all()

    assert_response(
        interaction,
        ["Bot に必要な権限がありません。 不足している権限: チャンネルの管理, ロールの管理"],
        ephemeral=True,
    )
    assert guild.channels == []
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_setup_custom_ui_channel_reports_discord_forbidden_detail(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(id=2_101_6, create_channel_error=make_forbidden())
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(interaction),
            ManagedUiType.REGISTER_PANEL.value,
            "レート戦はこちらから",
        )
    )

    session.expire_all()

    assert_response(
        interaction,
        ["Bot に必要な権限がありません。 Discord API: 403 Forbidden (error code: 0): Forbidden"],
        ephemeral=True,
    )
    assert guild.channels == []
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_setup_custom_ui_channel_reports_discord_forbidden_detail_when_initial_send_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(id=2_101_7, next_channel_fail_send_with=make_forbidden())
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(interaction),
            ManagedUiType.REGISTER_PANEL.value,
            "レート戦はこちらから",
        )
    )

    session.expire_all()

    assert_response(
        interaction,
        ["Bot に必要な権限がありません。 Discord API: 403 Forbidden (error code: 0): Forbidden"],
        ephemeral=True,
    )
    assert guild.channels == []
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_setup_ui_channels_returns_already_created_when_required_ui_exists(
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(id=2_102)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    for offset, definition in enumerate(get_required_managed_ui_definitions()):
        handlers.managed_ui_service.create_managed_ui_channel(
            ui_type=definition.ui_type,
            channel_id=40_001 + offset,
            message_id=50_001 + offset,
            created_by_discord_user_id=10,
        )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(handlers.admin_setup_ui_channels(as_interaction(interaction)))

    assert interaction.response.messages == ["必要な UI 設置チャンネルはすでに作成済みです。"]
    assert interaction.response.ephemeral_flags == [True]


def test_admin_setup_ui_channels_creates_registered_channel_set(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    guild = FakeGuild(id=2_102_5)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.admin_setup_ui_channels(as_interaction(interaction)))

    session.expire_all()
    managed_ui_channels = session.scalars(
        select(ManagedUiChannel).order_by(ManagedUiChannel.id.asc())
    ).all()

    assert_response(interaction, ["必要な UI 設置チャンネルを作成しました。"], ephemeral=True)
    assert interaction.response.defer_ephemeral is True
    assert interaction.response.defer_thinking is True
    assert [definition.ui_type for definition in get_required_managed_ui_definitions()] == [
        managed_ui_channel.ui_type for managed_ui_channel in managed_ui_channels
    ]
    expected_channel_names = [
        definition.recommended_channel_name for definition in get_required_managed_ui_definitions()
    ]
    assert expected_channel_names == [channel.name for channel in guild.channels]

    registered_role = find_role_by_name(guild, REGISTERED_PLAYER_ROLE_NAME)
    assert registered_role is not None

    register_channel = find_channel_by_name(guild, "レート戦はこちらから")
    assert register_channel.overwrites[guild.default_role].view_channel is True
    assert register_channel.overwrites[guild.default_role].send_messages is False
    assert register_channel.sent_messages[0].content == REGISTER_PANEL_MESSAGE

    matchmaking_channel = find_channel_by_name(guild, "レート戦マッチング")
    assert matchmaking_channel.overwrites[guild.default_role].view_channel is False
    assert matchmaking_channel.overwrites[registered_role].view_channel is True
    assert matchmaking_channel.overwrites[registered_role].send_messages is False
    assert matchmaking_channel.sent_messages[0].content == MATCHMAKING_CHANNEL_MESSAGE
    assert matchmaking_channel.sent_messages[0].view is not None
    matchmaking_button_labels = [
        cast(discord.ui.Button[Any], child).label
        for child in matchmaking_channel.sent_messages[0].view.children
    ]
    assert matchmaking_button_labels == [
        MATCHMAKING_CHANNEL_JOIN_HIGH_BUTTON_LABEL,
        MATCHMAKING_CHANNEL_JOIN_LOW_BUTTON_LABEL,
        MATCHMAKING_CHANNEL_PRESENT_BUTTON_LABEL,
        MATCHMAKING_CHANNEL_LEAVE_BUTTON_LABEL,
    ]

    matchmaking_news_channel = find_channel_by_name(guild, "レート戦マッチ速報")
    assert matchmaking_news_channel.overwrites[guild.default_role].view_channel is False
    assert matchmaking_news_channel.overwrites[registered_role].view_channel is True
    assert matchmaking_news_channel.sent_messages[0].content == MATCHMAKING_NEWS_CHANNEL_MESSAGE
    assert matchmaking_news_channel.sent_messages[0].view is None

    system_announcements_channel = find_channel_by_name(guild, "レート戦アナウンス")
    assert system_announcements_channel.overwrites[guild.default_role].view_channel is False
    assert system_announcements_channel.overwrites[registered_role].view_channel is True
    assert (
        system_announcements_channel.sent_messages[0].content
        == SYSTEM_ANNOUNCEMENTS_CHANNEL_MESSAGE
    )

    admin_contact_channel = find_channel_by_name(guild, "運営連絡・フィードバック")
    assert admin_contact_channel.overwrites[guild.default_role].view_channel is True
    assert admin_contact_channel.overwrites[guild.default_role].send_messages is True
    assert admin_contact_channel.sent_messages[0].content == ADMIN_CONTACT_CHANNEL_MESSAGE


def test_admin_setup_ui_channels_reports_missing_manage_roles_permission(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(
        id=2_102_6,
        me=FakeGuildMember(
            id=999_999,
            guild_permissions=discord.Permissions(manage_channels=True),
        ),
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(handlers.admin_setup_ui_channels(as_interaction(interaction)))

    session.expire_all()

    assert_response(
        interaction,
        ["Bot に必要な権限がありません。 不足している権限: ロールの管理"],
        ephemeral=True,
    )
    assert guild.channels == []
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_teardown_ui_channels_deletes_managed_channels_and_records(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    guild = FakeGuild(id=2_103)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    setup_interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(setup_interaction),
            ManagedUiType.REGISTER_PANEL.value,
            "レート戦はこちらから",
        )
    )

    teardown_interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )
    asyncio.run(
        handlers.admin_teardown_ui_channels(
            as_interaction(teardown_interaction),
            "teardown",
        )
    )

    session.expire_all()

    assert teardown_interaction.response.messages == ["UI 設置チャンネルをすべて撤収しました。"]
    assert teardown_interaction.response.ephemeral_flags == [True]
    assert teardown_interaction.response.defer_ephemeral is True
    assert teardown_interaction.response.defer_thinking is True
    assert guild.channels == []
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_teardown_ui_channels_removes_record_when_channel_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.REGISTER_PANEL,
        channel_id=60_001,
        message_id=70_001,
        created_by_discord_user_id=10,
    )
    guild = FakeGuild(id=2_104)
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(handlers.admin_teardown_ui_channels(as_interaction(interaction), "teardown"))

    session.expire_all()

    assert interaction.response.messages == ["UI 設置チャンネルをすべて撤収しました。"]
    assert interaction.response.ephemeral_flags == [True]
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_teardown_ui_channels_reports_missing_manage_channels_permission(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(
        id=2_104_5,
        me=FakeGuildMember(
            id=999_999,
            guild_permissions=discord.Permissions.none(),
        ),
    )
    channel = FakeTextChannel(id=60_002, name="レート戦はこちらから", guild=guild)
    guild.channels.append(channel)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.REGISTER_PANEL,
        channel_id=channel.id,
        message_id=70_002,
        created_by_discord_user_id=10,
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(handlers.admin_teardown_ui_channels(as_interaction(interaction), "teardown"))

    session.expire_all()

    assert_response(
        interaction,
        ["Bot に必要な権限がありません。 不足している権限: チャンネルの管理"],
        ephemeral=True,
    )
    assert channel.deleted is False
    assert session.scalar(select(ManagedUiChannel)) is not None


def test_admin_teardown_ui_channels_reports_discord_forbidden_detail(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(id=2_104_6)
    channel = FakeTextChannel(
        id=60_003,
        name="レート戦はこちらから",
        guild=guild,
        fail_delete_with=make_forbidden(),
    )
    guild.channels.append(channel)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.REGISTER_PANEL,
        channel_id=channel.id,
        message_id=70_003,
        created_by_discord_user_id=10,
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(handlers.admin_teardown_ui_channels(as_interaction(interaction), "teardown"))

    session.expire_all()

    assert_response(
        interaction,
        ["Bot に必要な権限がありません。 Discord API: 403 Forbidden (error code: 0): Forbidden"],
        ephemeral=True,
    )
    assert channel.deleted is False
    assert session.scalar(select(ManagedUiChannel)) is not None


def test_admin_restrict_and_unrestrict_user_commands_manage_restrictions(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_695
    player = create_player(session, target_discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=matching_queue_service,
    )
    restrict_interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))
    unrestrict_interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.admin_restrict_user(
            as_interaction(restrict_interaction),
            PlayerAccessRestrictionType.QUEUE_JOIN.value,
            PlayerAccessRestrictionDuration.SEVEN_DAYS.value,
            target_user=FakeUser(
                id=target_discord_user_id,
                name="target-user",
                global_name="target-global",
                nick="target-guild",
            ),
            reason="test reason",
        )
    )
    asyncio.run(
        handlers.admin_unrestrict_user(
            as_interaction(unrestrict_interaction),
            PlayerAccessRestrictionType.QUEUE_JOIN.value,
            target_user=FakeUser(
                id=target_discord_user_id,
                name="target-user",
                global_name="target-global",
                nick="target-guild",
            ),
        )
    )

    session.expire_all()
    restriction = session.scalar(
        select(PlayerAccessRestriction).where(PlayerAccessRestriction.player_id == player.id)
    )
    persisted_player = session.scalar(select(Player).where(Player.id == player.id))
    queue_entry = session.scalar(
        select(MatchQueueEntry).where(MatchQueueEntry.player_id == player.id)
    )

    assert restrict_interaction.response.messages == [
        "指定したユーザーのキュー参加を7日制限しました。"
    ]
    assert unrestrict_interaction.response.messages == [
        "指定したユーザーのキュー参加制限を解除しました。"
    ]
    assert restriction is not None
    assert restriction.restriction_type == PlayerAccessRestrictionType.QUEUE_JOIN
    assert restriction.reason == "test reason"
    assert restriction.revoked_at is not None
    assert persisted_player is not None
    assert persisted_player.display_name == "target-guild"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at
    assert queue_entry is not None
    assert queue_entry.status == MatchQueueEntryStatus.WAITING


def test_admin_restrict_user_accepts_dummy_user_reference(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.admin_restrict_user(
            as_interaction(interaction),
            PlayerAccessRestrictionType.SPECTATE.value,
            PlayerAccessRestrictionDuration.ONE_DAY.value,
            dummy_user=f"<dummy_{target_discord_user_id}>",
        )
    )

    session.expire_all()
    restriction = session.scalar(
        select(PlayerAccessRestriction).where(PlayerAccessRestriction.player_id == player.id)
    )

    assert interaction.response.messages == ["指定したユーザーの観戦を1日制限しました。"]
    assert restriction is not None
    assert restriction.restriction_type == PlayerAccessRestrictionType.SPECTATE


def test_admin_restrict_user_requires_target_selection(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(
        handlers.admin_restrict_user(
            as_interaction(interaction),
            PlayerAccessRestrictionType.QUEUE_JOIN.value,
            PlayerAccessRestrictionDuration.SEVEN_DAYS.value,
        )
    )

    assert interaction.response.messages == ["対象ユーザーの指定が不正です。"]


def test_admin_add_penalty_accepts_dummy_user_reference(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 778
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.admin_add_penalty(
            as_interaction(interaction),
            PenaltyType.LATE,
            dummy_user=f"<dummy_{target_discord_user_id}>",
        )
    )

    session.expire_all()
    penalty = session.get(
        PlayerPenalty,
        {"player_id": player.id, "penalty_type": PenaltyType.LATE},
    )

    assert interaction.response.messages == ["ペナルティを加算しました。"]
    assert penalty is not None
    assert penalty.count == 1


def test_dev_commands_register_discord_user_id_as_last_option() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    expected_parameters = {
        "dev_register": ["discord_user_id"],
        "dev_join": ["match_format", "queue_name", "discord_user_id"],
        "dev_present": ["discord_user_id"],
        "dev_leave": ["discord_user_id"],
        "dev_player_info": ["discord_user_id"],
        "dev_player_info_season": ["season_id", "discord_user_id"],
        "dev_match_parent": ["match_id", "discord_user_id"],
        "dev_match_spectate": ["match_id", "discord_user_id"],
        "dev_match_win": ["match_id", "discord_user_id"],
        "dev_match_lose": ["match_id", "discord_user_id"],
        "dev_match_draw": ["match_id", "discord_user_id"],
        "dev_match_void": ["match_id", "discord_user_id"],
        "dev_match_approve": ["match_id", "discord_user_id"],
    }

    for command_name, expected in expected_parameters.items():
        command = tree.get_command(command_name)
        assert command is not None
        assert [parameter.name for parameter in command.parameters] == expected


def test_admin_restriction_commands_are_registered_with_expected_parameters() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    restrict_command = tree.get_command("admin_restrict_user")
    unrestrict_command = tree.get_command("admin_unrestrict_user")

    assert restrict_command is not None
    assert unrestrict_command is not None
    assert [parameter.name for parameter in restrict_command.parameters] == [
        "restriction_type",
        "duration",
        "user",
        "dummy_user",
        "reason",
    ]
    assert [parameter.name for parameter in unrestrict_command.parameters] == [
        "restriction_type",
        "user",
        "dummy_user",
    ]


def test_admin_penalty_commands_are_registered_with_expected_parameters() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    add_command = tree.get_command("admin_add_late")
    sub_command = tree.get_command("admin_sub_late")

    assert add_command is not None
    assert sub_command is not None
    assert [parameter.name for parameter in add_command.parameters] == ["user", "dummy_user"]
    assert [parameter.name for parameter in sub_command.parameters] == ["user", "dummy_user"]


def test_admin_managed_ui_commands_are_registered_with_expected_parameters() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    setup_custom_command = tree.get_command("admin_setup_custom_ui_channel")
    setup_all_command = tree.get_command("admin_setup_ui_channels")
    teardown_command = tree.get_command("admin_teardown_ui_channels")

    assert setup_custom_command is not None
    assert setup_all_command is not None
    assert teardown_command is not None
    assert [parameter.name for parameter in setup_custom_command.parameters] == [
        "ui_type",
        "channel_name",
    ]
    assert [parameter.name for parameter in setup_all_command.parameters] == []
    assert [parameter.name for parameter in teardown_command.parameters] == ["confirm"]


def test_match_spectate_command_is_registered_with_match_id_parameter() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    command = tree.get_command("match_spectate")
    assert command is not None
    assert [parameter.name for parameter in command.parameters] == ["match_id"]
