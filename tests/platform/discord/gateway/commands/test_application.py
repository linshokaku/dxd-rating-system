from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import discord
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.leaderboard.application import resolve_snapshot_date
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
from dxd_rating.contexts.seasons.application import (
    ensure_active_and_upcoming_seasons,
    get_database_now,
)
from dxd_rating.contexts.ui.application import (
    REGISTERED_PLAYER_ROLE_NAME,
    InfoThreadCommandName,
    get_required_managed_ui_definitions,
)
from dxd_rating.platform.config.bot import BotSettings
from dxd_rating.platform.db.models import (
    ActiveMatchState,
    FinalizedMatchResult,
    LeaderboardSnapshot,
    ManagedUiChannel,
    ManagedUiType,
    MatchFormat,
    MatchParticipant,
    MatchParticipantTeam,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchReport,
    MatchReportInputResult,
    MatchResult,
    MatchSpectator,
    MatchState,
    OutboxEvent,
    OutboxEventType,
    PenaltyType,
    Player,
    PlayerAccessRestriction,
    PlayerAccessRestrictionType,
    PlayerFormatStats,
    PlayerInfoThreadBinding,
    PlayerPenalty,
    Season,
)
from dxd_rating.platform.discord.copy.info import (
    INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
    INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
    INFO_CHANNEL_MESSAGE,
    INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
    INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
    INFO_THREAD_LEADERBOARD_MATCH_FORMAT_PLACEHOLDER,
    INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_LABEL,
    INFO_THREAD_LEADERBOARD_SEASON_PLACEHOLDER,
    INFO_THREAD_LEADERBOARD_SEASON_SELECT_BOTH_MESSAGE,
    INFO_THREAD_LEADERBOARD_SEASON_SELECT_SEASON_MESSAGE,
    INFO_THREAD_LEADERBOARD_SELECT_MATCH_FORMAT_MESSAGE,
    INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL,
    INFO_THREAD_PLAYER_INFO_SEASON_PLACEHOLDER,
    INFO_THREAD_PLAYER_INFO_SEASON_SELECT_SEASON_MESSAGE,
    INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL,
    build_info_thread_initial_message,
)
from dxd_rating.platform.discord.copy.match import MATCHMAKING_NEWS_CHANNEL_MESSAGE
from dxd_rating.platform.discord.copy.matchmaking import (
    MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL,
    MATCHMAKING_CHANNEL_MATCH_FORMAT_PLACEHOLDER,
    MATCHMAKING_CHANNEL_MESSAGE,
    MATCHMAKING_CHANNEL_QUEUE_NAME_PLACEHOLDER,
    MATCHMAKING_CHANNEL_SELECT_MATCH_FORMAT_MESSAGE,
    MATCHMAKING_CHANNEL_SELECT_QUEUE_NAME_MESSAGE,
    MATCHMAKING_CHANNEL_STATUS_PLACEHOLDER_MESSAGE,
    MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL,
    MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
    MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
    build_matchmaking_guide_message,
    build_matchmaking_status_message,
)
from dxd_rating.platform.discord.copy.registration import (
    REGISTER_PANEL_BUTTON_LABEL,
    REGISTER_PANEL_MESSAGE,
)
from dxd_rating.platform.discord.copy.system import (
    ADMIN_CONTACT_CHANNEL_MESSAGE,
    ADMIN_OPERATIONS_CHANNEL_MESSAGE,
    APPLICATION_COMMAND_INTERNAL_ERROR_MESSAGE,
    SYSTEM_ANNOUNCEMENTS_CHANNEL_MESSAGE,
)
from dxd_rating.platform.discord.gateway.commands import BotCommandHandlers, register_app_commands
from dxd_rating.platform.discord.ui import (
    INFO_CHANNEL_LEADERBOARD_BUTTON_CUSTOM_ID,
    INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_CUSTOM_ID,
    INFO_CHANNEL_PLAYER_INFO_BUTTON_CUSTOM_ID,
    INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_CUSTOM_ID,
    INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS,
    MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_CUSTOM_ID,
    MatchmakingNewsMatchAnnouncementSpectateButton,
    MatchmakingPanelView,
    MatchOperationThreadParentButton,
)
from dxd_rating.platform.runtime import MatchRuntime
from dxd_rating.shared.constants import MATCH_FORMAT_CHOICES, get_match_queue_class_definitions

DEFAULT_MATCH_FORMAT = MatchFormat.THREE_VS_THREE
DEFAULT_QUEUE_NAME = "beginner"
DEFAULT_MATCHMAKING_GUIDE_URL = (
    "https://github.com/linshokaku/dxd-rating-system/blob/main/docs/README.md"
)


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
    send_message_call_count: int = 0
    defer_call_count: int = 0
    interaction: FakeInteraction | None = field(default=None, repr=False)

    async def send_message(self, content: str, *, ephemeral: bool = False, **_: Any) -> None:
        self.deferred = True
        self.send_message_call_count += 1
        self.messages.append(content)
        self.ephemeral_flags.append(ephemeral)

    async def edit_message(
        self,
        *,
        view: discord.ui.View | discord.ui.LayoutView | None = None,
        **_: Any,
    ) -> None:
        self.deferred = True
        interaction = self.interaction
        if interaction is None or interaction.message is None:
            raise RuntimeError("edit_message requires an interaction message")
        interaction.message.view = view

    async def defer(
        self,
        *,
        ephemeral: bool = False,
        thinking: bool = False,
        **_: Any,
    ) -> None:
        self.deferred = True
        self.defer_call_count += 1
        self.defer_ephemeral = ephemeral
        self.defer_thinking = thinking

    def is_done(self) -> bool:
        return self.deferred


@dataclass
class FakeInteractionFollowup:
    response: FakeInteractionResponse
    send_call_count: int = 0

    async def send(self, content: str, *, ephemeral: bool = False, **_: Any) -> None:
        self.send_call_count += 1
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
        default_factory=lambda: discord.Permissions(
            manage_channels=True,
            manage_roles=True,
            create_private_threads=True,
            send_messages_in_threads=True,
        )
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
    view: discord.ui.View | discord.ui.LayoutView | None = None
    suppress_embeds: bool = False
    fail_edit_with: Exception | None = None

    async def edit(
        self,
        *,
        content: str | None = None,
        view: discord.ui.View | discord.ui.LayoutView | None = None,
        suppress_embeds: bool | None = None,
        **_: Any,
    ) -> discord.Message:
        if self.fail_edit_with is not None:
            raise self.fail_edit_with
        if content is not None:
            self.content = content
        if view is not None:
            self.view = view
        if suppress_embeds is not None:
            self.suppress_embeds = suppress_embeds
        return cast(discord.Message, self)


@dataclass
class FakeThread:
    id: int
    name: str
    parent: FakeTextChannel
    sent_messages: list[FakeMessage] = field(default_factory=list)
    added_user_ids: list[int] = field(default_factory=list)
    fail_send_with: Exception | None = None
    fail_delete_with: Exception | None = None
    deleted: bool = False

    async def add_user(self, user: object) -> None:
        user_id = getattr(user, "id", None)
        if isinstance(user_id, int):
            self.added_user_ids.append(user_id)

    async def send(
        self,
        content: str | None = None,
        *,
        view: discord.ui.View | None = None,
        suppress_embeds: bool = False,
        **_: Any,
    ) -> discord.Message:
        if self.fail_send_with is not None:
            raise self.fail_send_with

        message = FakeMessage(
            id=self.parent.guild.next_message_id,
            content="" if content is None else content,
            view=view,
            suppress_embeds=suppress_embeds,
        )
        self.parent.guild.next_message_id += 1
        self.sent_messages.append(message)
        return cast(discord.Message, message)

    async def delete(self, *_: Any, **__: Any) -> None:
        if self.fail_delete_with is not None:
            raise self.fail_delete_with

        self.deleted = True


@dataclass
class FakeTextChannel:
    id: int
    name: str
    guild: FakeGuild
    overwrites: dict[object, discord.PermissionOverwrite] = field(default_factory=dict)
    sent_messages: list[FakeMessage] = field(default_factory=list)
    created_threads: list[FakeThread] = field(default_factory=list)
    fail_send_with: Exception | None = None
    fail_send_call_errors: dict[int, Exception] = field(default_factory=dict)
    fail_fetch_message_with: Exception | None = None
    fail_create_thread_with: Exception | None = None
    fail_delete_with: Exception | None = None
    deleted: bool = False
    send_call_count: int = 0

    async def send(
        self,
        content: str | None = None,
        *,
        view: discord.ui.View | None = None,
        suppress_embeds: bool = False,
        **_: Any,
    ) -> discord.Message:
        self.send_call_count += 1
        call_error = self.fail_send_call_errors.get(self.send_call_count)
        if call_error is not None:
            raise call_error
        if self.fail_send_with is not None:
            raise self.fail_send_with

        message = FakeMessage(
            id=self.guild.next_message_id,
            content="" if content is None else content,
            view=view,
            suppress_embeds=suppress_embeds,
        )
        self.guild.next_message_id += 1
        self.sent_messages.append(message)
        return cast(discord.Message, message)

    async def fetch_message(self, message_id: int) -> discord.Message:
        if self.fail_fetch_message_with is not None:
            raise self.fail_fetch_message_with
        for message in self.sent_messages:
            if message.id == message_id:
                return cast(discord.Message, message)
        raise make_not_found()

    async def create_thread(
        self,
        *,
        name: str,
        **_: Any,
    ) -> discord.Thread:
        if self.fail_create_thread_with is not None:
            raise self.fail_create_thread_with

        thread = FakeThread(
            id=self.guild.next_channel_id,
            name=name,
            parent=self,
        )
        self.guild.next_channel_id += 1
        self.created_threads.append(thread)
        return cast(discord.Thread, thread)

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
class FakeUnsupportedGuildChannel:
    id: int
    name: str
    guild: FakeGuild


@dataclass
class FakeGuild:
    id: int
    channels: list[Any] = field(default_factory=list)
    members: dict[int, object] = field(default_factory=dict)
    default_role: FakeRole = field(default_factory=lambda: FakeRole(id=0))
    roles: list[FakeRole] = field(default_factory=list)
    me: FakeGuildMember | None = None
    next_channel_id: int = 20_001
    next_message_id: int = 30_001
    next_role_id: int = 40_001
    create_channel_error: Exception | None = None
    create_role_error: Exception | None = None
    next_channel_fail_send_with: Exception | None = None
    next_channel_fail_send_call_errors: dict[int, Exception] = field(default_factory=dict)
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
            fail_send_call_errors=dict(self.next_channel_fail_send_call_errors),
            fail_delete_with=self.next_channel_fail_delete_with,
        )
        self.next_channel_id += 1
        self.next_channel_fail_send_with = None
        self.next_channel_fail_send_call_errors = {}
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

    def get_channel(self, channel_id: int) -> Any | None:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None

    def get_member(self, member_id: int) -> object | None:
        return self.members.get(member_id)

    async def fetch_member(self, member_id: int) -> object:
        member = self.get_member(member_id)
        if member is None:
            raise LookupError(f"Member not found: {member_id}")
        return member


@dataclass
class FakeInteraction:
    user: FakeUser
    channel_id: int | None = 1_001
    guild_id: int | None = 2_001
    application_id: int | None = 3_001
    token: str = "interaction-token"
    guild: FakeGuild | None = None
    message: FakeMessage | None = None
    response: FakeInteractionResponse = field(default_factory=FakeInteractionResponse)
    followup: FakeInteractionFollowup = field(init=False)

    def __post_init__(self) -> None:
        self.response.interaction = self
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


def assert_response_sequence(
    interaction: FakeInteraction,
    expected_messages: list[str],
    expected_ephemeral_flags: list[bool],
) -> None:
    assert interaction.response.messages == expected_messages
    assert interaction.response.ephemeral_flags == expected_ephemeral_flags


def assert_deferred_followup_response(
    interaction: FakeInteraction,
    *,
    followup_send_call_count: int = 1,
) -> None:
    assert interaction.response.defer_call_count == 1
    assert interaction.response.defer_ephemeral is True
    assert interaction.response.defer_thinking is True
    assert interaction.response.send_message_call_count == 0
    assert interaction.followup.send_call_count == followup_send_call_count


def assert_presence_thread_controls(view: discord.ui.View | None) -> None:
    assert view is not None
    button_labels = [cast(discord.ui.Button[Any], child).label for child in view.children]
    assert button_labels == [
        MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
        MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
    ]


def assert_info_thread_player_info_initial_controls(view: discord.ui.View | None) -> None:
    assert view is not None
    assert len(view.children) == 1
    show_button = cast(discord.ui.Button[Any], view.children[0])

    assert show_button.label == INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL
    assert show_button.disabled is False


def assert_info_thread_player_info_season_initial_controls(
    view: discord.ui.View | None,
    *,
    expected_seasons: list[Season],
) -> None:
    assert view is not None
    assert len(view.children) == 2
    season_select = cast(discord.ui.Select[Any], view.children[0])
    show_button = cast(discord.ui.Button[Any], view.children[1])

    assert season_select.placeholder == INFO_THREAD_PLAYER_INFO_SEASON_PLACEHOLDER
    assert [option.label for option in season_select.options] == [
        season.name for season in expected_seasons[:INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS]
    ]
    assert [option.value for option in season_select.options] == [
        str(season.id) for season in expected_seasons[:INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS]
    ]
    assert [option.description for option in season_select.options] == [
        f"season_id: {season.id}"
        for season in expected_seasons[:INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS]
    ]
    assert season_select.disabled is False
    assert show_button.label == INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL
    assert show_button.disabled is False


def assert_info_thread_leaderboard_initial_controls(view: discord.ui.View | None) -> None:
    assert view is not None
    assert len(view.children) == 2
    match_format_select = cast(discord.ui.Select[Any], view.children[0])
    show_button = cast(discord.ui.Button[Any], view.children[1])

    assert match_format_select.placeholder == INFO_THREAD_LEADERBOARD_MATCH_FORMAT_PLACEHOLDER
    assert [option.value for option in match_format_select.options] == list(MATCH_FORMAT_CHOICES)
    assert match_format_select.disabled is False
    assert show_button.label == INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL
    assert show_button.disabled is False


def assert_info_thread_leaderboard_next_page_control(view: discord.ui.View | None) -> None:
    assert view is not None
    button = cast(discord.ui.Button[Any], getattr(view.children[0], "item", view.children[0]))
    assert button.label == INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_LABEL
    assert button.disabled is False


def assert_info_thread_leaderboard_season_initial_controls(
    view: discord.ui.View | None,
    *,
    expected_seasons: list[Season],
) -> None:
    assert view is not None
    assert len(view.children) == 3
    season_select = cast(discord.ui.Select[Any], view.children[0])
    match_format_select = cast(discord.ui.Select[Any], view.children[1])
    show_button = cast(discord.ui.Button[Any], view.children[2])

    assert season_select.placeholder == INFO_THREAD_LEADERBOARD_SEASON_PLACEHOLDER
    assert [option.label for option in season_select.options] == [
        season.name for season in expected_seasons[:INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS]
    ]
    assert [option.value for option in season_select.options] == [
        str(season.id) for season in expected_seasons[:INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS]
    ]
    assert [option.description for option in season_select.options] == [
        f"season_id: {season.id}"
        for season in expected_seasons[:INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS]
    ]
    assert season_select.disabled is False
    assert match_format_select.placeholder == INFO_THREAD_LEADERBOARD_MATCH_FORMAT_PLACEHOLDER
    assert [option.value for option in match_format_select.options] == list(MATCH_FORMAT_CHOICES)
    assert match_format_select.disabled is False
    assert show_button.label == INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL
    assert show_button.disabled is False


def assert_all_controls_disabled(view: discord.ui.View | discord.ui.LayoutView | None) -> None:
    assert view is not None
    assert len(view.children) > 0
    assert all(
        getattr(getattr(child, "item", child), "disabled", False) is True for child in view.children
    )


def set_select_values(select: discord.ui.Select[Any], values: list[str]) -> None:
    setattr(select, "_values", values)


@pytest.fixture(autouse=True)
def prepared_seasons(session: Session) -> None:
    ensure_active_and_upcoming_seasons(session)
    session.commit()


def create_settings(
    *,
    super_admin_user_ids: frozenset[int] = frozenset(),
    development_mode: bool = False,
    matchmaking_guide_url: str = DEFAULT_MATCHMAKING_GUIDE_URL,
) -> BotSettings:
    return BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=matchmaking_guide_url,
        development_mode=development_mode,
        super_admin_user_ids=super_admin_user_ids,
    )


def create_handlers(
    session_factory: sessionmaker[Session],
    *,
    super_admin_user_ids: frozenset[int] = frozenset(),
    development_mode: bool = False,
    matchmaking_guide_url: str = DEFAULT_MATCHMAKING_GUIDE_URL,
    matching_queue_service: MatchingQueueService | MatchRuntime | None = None,
) -> BotCommandHandlers:
    resolved_matching_queue_service = matching_queue_service
    if isinstance(matching_queue_service, MatchingQueueService):
        resolved_matching_queue_service = MatchRuntime(
            service=matching_queue_service,
            match_service=MatchFlowService(session_factory),
        )

    return BotCommandHandlers(
        settings=create_settings(
            super_admin_user_ids=super_admin_user_ids,
            development_mode=development_mode,
            matchmaking_guide_url=matchmaking_guide_url,
        ),
        session_factory=session_factory,
        matching_queue_service=resolved_matching_queue_service,
    )


def create_player(session: Session, discord_user_id: int) -> Player:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player


def create_players(session: Session, count: int, *, start_discord_user_id: int) -> list[Player]:
    return [create_player(session, start_discord_user_id + offset) for offset in range(count)]


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


def setup_matchmaking_managed_ui_channel(
    handlers: BotCommandHandlers,
    channel_id: int,
    *,
    created_by_discord_user_id: int = 10,
    message_id: int = 70_001,
    status_message_id: int | None = 70_000,
) -> None:
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
        channel_id=channel_id,
        message_id=message_id,
        status_message_id=status_message_id,
        created_by_discord_user_id=created_by_discord_user_id,
    )


def setup_info_managed_ui_channel(
    handlers: BotCommandHandlers,
    channel_id: int,
    *,
    created_by_discord_user_id: int = 10,
    message_id: int = 70_101,
) -> None:
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.INFO_CHANNEL,
        channel_id=channel_id,
        message_id=message_id,
        created_by_discord_user_id=created_by_discord_user_id,
    )


def create_active_info_thread(
    handlers: BotCommandHandlers,
    *,
    discord_user_id: int,
    guild: FakeGuild,
    info_channel: FakeTextChannel,
    command_name: InfoThreadCommandName = InfoThreadCommandName.PLAYER_INFO,
    interaction_channel_id: int = 13_199,
    user_name: str | None = None,
    user_global_name: str | None = None,
    user_nick: str | None = None,
) -> FakeThread:
    interaction = FakeInteraction(
        user=FakeUser(
            id=discord_user_id,
            name=user_name,
            global_name=user_global_name,
            nick=user_nick,
        ),
        channel_id=interaction_channel_id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.info_thread(as_interaction(interaction), command_name.value))

    assert len(info_channel.created_threads) > 0, interaction.response.messages
    return info_channel.created_threads[-1]


def create_active_dev_info_thread(
    handlers: BotCommandHandlers,
    *,
    executor_discord_user_id: int,
    target_discord_user_id: int,
    guild: FakeGuild,
    info_channel: FakeTextChannel,
    command_name: InfoThreadCommandName = InfoThreadCommandName.PLAYER_INFO,
    interaction_channel_id: int = 13_299,
    executor_name: str | None = None,
    executor_global_name: str | None = None,
    executor_nick: str | None = None,
) -> FakeThread:
    interaction = FakeInteraction(
        user=FakeUser(
            id=executor_discord_user_id,
            name=executor_name,
            global_name=executor_global_name,
            nick=executor_nick,
        ),
        channel_id=interaction_channel_id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_info_thread(
            as_interaction(interaction),
            command_name.value,
            str(target_discord_user_id),
        )
    )

    assert len(info_channel.created_threads) > 0, interaction.response.messages
    return info_channel.created_threads[-1]


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


def format_leaderboard_message(
    *,
    season_name: str,
    match_format: MatchFormat,
    page: int,
    entries: list[tuple[int, str, float, int | None, int | None, int | None]],
) -> str:
    first_rank = entries[0][0]
    last_rank = entries[-1][0]
    lines = [
        "ランキング",
        f"season: {season_name}",
        f"match_format: {match_format.value}",
        f"page: {page}",
        f"items: {first_rank}-{last_rank}",
        "",
    ]
    lines.extend(
        (
            f"{rank} / {display_name} / {rating:.2f} / "
            f"{format_rank_change(rank_change_1d)} / "
            f"{format_rank_change(rank_change_3d)} / "
            f"{format_rank_change(rank_change_7d)}"
        )
        for rank, display_name, rating, rank_change_1d, rank_change_3d, rank_change_7d in entries
    )
    return "\n".join(lines)


def format_rank_change(rank_change: int | None) -> str:
    if rank_change is None:
        return "-"
    if rank_change > 0:
        return f"+{rank_change}"
    return str(rank_change)


def format_leaderboard_season_message(
    *,
    season_id: int,
    season_name: str,
    match_format: MatchFormat,
    page: int,
    entries: list[tuple[int, str, float]],
) -> str:
    first_rank = entries[0][0]
    last_rank = entries[-1][0]
    lines = [
        "ランキング",
        f"season_id: {season_id}",
        f"season_name: {season_name}",
        f"match_format: {match_format.value}",
        f"page: {page}",
        f"items: {first_rank}-{last_rank}",
        "",
    ]
    lines.extend(
        f"{rank} / {display_name} / {rating:.2f}" for rank, display_name, rating in entries
    )
    return "\n".join(lines)


def get_queue_entry(session: Session, player_id: int) -> MatchQueueEntry:
    session.expire_all()
    queue_entry = session.scalar(
        select(MatchQueueEntry).where(MatchQueueEntry.player_id == player_id)
    )
    assert queue_entry is not None
    return queue_entry


def get_outbox_events(session: Session) -> list[OutboxEvent]:
    session.expire_all()
    return session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()


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


def seed_waiting_entries_with_presence_threads(
    session_factory: sessionmaker[Session],
    players: list[Player],
    *,
    channel_id: int,
    guild_id: int,
    start_presence_thread_channel_id: int,
) -> None:
    queue_service = MatchingQueueService(session_factory)
    for index, player in enumerate(players):
        join_result = queue_service.join_queue(
            player.id,
            DEFAULT_MATCH_FORMAT,
            DEFAULT_QUEUE_NAME,
            notification_context=MatchingQueueNotificationContext(
                channel_id=channel_id,
                guild_id=guild_id,
                mention_discord_user_id=player.discord_user_id,
            ),
        )
        assert queue_service.update_waiting_presence_thread_channel_id(
            join_result.queue_entry_id,
            start_presence_thread_channel_id + index,
        )


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
    guild = FakeGuild(id=4_001)
    matchmaking_channel = FakeTextChannel(
        id=3_001,
        name="レート戦マッチング",
        guild=guild,
    )
    command_channel = FakeTextChannel(
        id=3_002,
        name="雑談",
        guild=guild,
    )
    guild.channels.extend([matchmaking_channel, command_channel])
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(
            id=discord_user_id,
            name="queue-user",
            global_name="queue-global",
            nick="queue-guild",
        ),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
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
        ["キューに参加しました。5分間マッチングします。\n在席確認は <#20001> で行ってください。"],
        ephemeral=True,
    )
    assert queue_entry.notification_channel_id == matchmaking_channel.id
    assert queue_entry.presence_thread_channel_id == 20_001
    assert queue_entry.notification_guild_id == 4_001
    assert queue_entry.notification_dm_discord_user_id is None
    assert queue_entry.notification_interaction_application_id is None
    assert queue_entry.notification_interaction_token is None
    assert queue_entry.notification_mention_discord_user_id == discord_user_id
    assert persisted_player is not None
    assert persisted_player.display_name == "queue-guild"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at
    assert len(command_channel.created_threads) == 0
    assert len(matchmaking_channel.created_threads) == 1
    assert matchmaking_channel.created_threads[0].name == "在席確認-queue-guild"
    assert matchmaking_channel.created_threads[0].added_user_ids == [discord_user_id]
    assert [
        message.content for message in matchmaking_channel.created_threads[0].sent_messages
    ] == ["キューに参加しました。5分間マッチングします。"]
    assert_presence_thread_controls(matchmaking_channel.created_threads[0].sent_messages[0].view)


def test_join_command_routes_match_created_to_presence_threads_without_parent_fallback(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    joining_discord_user_id = 123_456_789_012_345_681
    existing_players = create_players(session, 5, start_discord_user_id=123_456_789_012_345_700)
    joining_player = create_player(session, joining_discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=4_011)
    matchmaking_channel = FakeTextChannel(
        id=3_011,
        name="レート戦マッチング",
        guild=guild,
    )
    matchmaking_news_channel = FakeTextChannel(
        id=3_012,
        name="レート戦マッチ速報",
        guild=guild,
    )
    command_channel = FakeTextChannel(
        id=3_013,
        name="雑談",
        guild=guild,
    )
    guild.channels.extend([matchmaking_channel, matchmaking_news_channel, command_channel])
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.MATCHMAKING_NEWS_CHANNEL,
        channel_id=matchmaking_news_channel.id,
        message_id=70_012,
        created_by_discord_user_id=joining_discord_user_id,
    )
    seed_waiting_entries_with_presence_threads(
        session_factory,
        existing_players,
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        start_presence_thread_channel_id=20_100,
    )
    interaction = FakeInteraction(
        user=FakeUser(
            id=joining_discord_user_id,
            name="queue-user",
            global_name="queue-global",
            nick="queue-guild",
        ),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    queue_entry = get_queue_entry(session, joining_player.id)
    match_created_events = [
        event
        for event in get_outbox_events(session)
        if event.event_type == OutboxEventType.MATCH_CREATED
    ]
    destination_channel_ids = {
        event.payload["destination"]["channel_id"] for event in match_created_events
    }
    participant_events = [
        event
        for event in match_created_events
        if event.payload["destination"]["channel_id"] != matchmaking_news_channel.id
    ]

    assert queue_entry.status == MatchQueueEntryStatus.MATCHED
    assert len(match_created_events) == 7
    assert matchmaking_channel.id not in destination_channel_ids
    assert matchmaking_news_channel.id in destination_channel_ids
    assert queue_entry.presence_thread_channel_id == 20_001
    assert queue_entry.presence_thread_channel_id in destination_channel_ids
    assert len(participant_events) == 6
    assert {event.payload["destination"]["channel_id"] for event in participant_events} == {
        20_001,
        20_100,
        20_101,
        20_102,
        20_103,
        20_104,
    }
    assert all("mention_discord_user_id" in event.payload for event in participant_events)


def test_join_command_requires_registered_player(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=4_005)
    matchmaking_channel = FakeTextChannel(
        id=3_005,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=123_456_789_012_345_681),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

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


def test_join_command_returns_internal_error_when_matchmaking_channel_is_not_setup(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_0
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=4_010)
    command_channel = FakeTextChannel(
        id=3_010,
        name="雑談",
        guild=guild,
    )
    guild.channels.append(command_channel)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

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
    guild = FakeGuild(id=4_011)
    matchmaking_channel = FakeTextChannel(
        id=3_011,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

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
    guild = FakeGuild(id=4_012)
    matchmaking_channel = FakeTextChannel(
        id=3_012,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    assert_response(interaction, ["現在キュー参加を制限されています。"], ephemeral=True)


def test_matchmaking_panel_join_button_requires_match_format_selection(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    view = MatchmakingPanelView(handlers)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_681_3))
    join_button = cast(discord.ui.Button[Any], view.children[2])

    asyncio.run(join_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        [MATCHMAKING_CHANNEL_SELECT_MATCH_FORMAT_MESSAGE],
        ephemeral=True,
    )
    assert_deferred_followup_response(interaction)


def test_matchmaking_panel_join_button_requires_queue_selection(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    view = MatchmakingPanelView(handlers)
    user = FakeUser(id=123_456_789_012_345_681_4)
    match_format_select = cast(discord.ui.Select[Any], view.children[0])
    join_button = cast(discord.ui.Button[Any], view.children[2])

    set_select_values(match_format_select, [MatchFormat.TWO_VS_TWO.value])
    select_interaction = FakeInteraction(user=user)
    asyncio.run(match_format_select.callback(as_interaction(select_interaction)))

    interaction = FakeInteraction(user=user)
    asyncio.run(join_button.callback(as_interaction(interaction)))

    assert select_interaction.response.deferred is True
    assert select_interaction.response.messages == []
    assert_response(
        interaction,
        [MATCHMAKING_CHANNEL_SELECT_QUEUE_NAME_MESSAGE],
        ephemeral=True,
    )
    assert_deferred_followup_response(interaction)


def test_matchmaking_panel_join_button_uses_selected_values_for_join(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_5
    player = create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    view = MatchmakingPanelView(handlers)
    match_format_select = cast(discord.ui.Select[Any], view.children[0])
    queue_name_select = cast(discord.ui.Select[Any], view.children[1])
    join_button = cast(discord.ui.Button[Any], view.children[2])
    user = FakeUser(
        id=discord_user_id,
        name="ui-queue-user",
        global_name="ui-queue-global",
        nick="ui-queue-guild",
    )
    guild = FakeGuild(id=9_101)
    channel = FakeTextChannel(
        id=9_001,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(channel)
    setup_matchmaking_managed_ui_channel(handlers, channel.id)

    set_select_values(match_format_select, [MatchFormat.ONE_VS_ONE.value])
    asyncio.run(match_format_select.callback(as_interaction(FakeInteraction(user=user))))

    set_select_values(queue_name_select, ["regular"])
    asyncio.run(queue_name_select.callback(as_interaction(FakeInteraction(user=user))))

    interaction = FakeInteraction(
        user=user,
        channel_id=channel.id,
        guild_id=guild.id,
        guild=guild,
    )
    asyncio.run(join_button.callback(as_interaction(interaction)))

    queue_entry = get_queue_entry(session, player.id)

    assert_response(
        interaction,
        ["キューに参加しました。5分間マッチングします。\n在席確認は <#20001> で行ってください。"],
        ephemeral=True,
    )
    assert_deferred_followup_response(interaction)
    assert queue_entry.match_format == MatchFormat.ONE_VS_ONE
    assert queue_entry.notification_channel_id == channel.id
    assert queue_entry.presence_thread_channel_id == 20_001
    assert queue_entry.notification_guild_id == 9_101
    assert queue_entry.notification_mention_discord_user_id == discord_user_id
    assert len(channel.created_threads) == 1
    assert channel.created_threads[0].name == "在席確認-ui-queue-guild"
    assert channel.created_threads[0].added_user_ids == [discord_user_id]
    assert [message.content for message in channel.created_threads[0].sent_messages] == [
        "キューに参加しました。5分間マッチングします。"
    ]
    assert_presence_thread_controls(channel.created_threads[0].sent_messages[0].view)


def test_matchmaking_presence_thread_present_button_updates_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_6
    player = create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    user = FakeUser(
        id=discord_user_id,
        name="thread-user",
        global_name="thread-global",
        nick="thread-guild",
    )
    guild = FakeGuild(id=9_102)
    channel = FakeTextChannel(
        id=9_002,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(channel)
    setup_matchmaking_managed_ui_channel(handlers, channel.id)
    join_interaction = FakeInteraction(
        user=user,
        channel_id=channel.id,
        guild_id=guild.id,
        guild=guild,
    )
    button_interaction = FakeInteraction(
        user=user,
        channel_id=None,
        guild_id=guild.id,
        guild=guild,
    )

    async def scenario() -> None:
        await handlers.join(
            as_interaction(join_interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )

        thread = channel.created_threads[0]
        thread_message = thread.sent_messages[0]
        assert thread_message.view is not None
        present_button = cast(discord.ui.Button[Any], thread_message.view.children[0])
        button_interaction.channel_id = thread.id
        await present_button.callback(as_interaction(button_interaction))

    asyncio.run(scenario())

    thread = channel.created_threads[0]

    with session_factory() as verification_session:
        queue_entry = verification_session.scalar(
            select(MatchQueueEntry).where(MatchQueueEntry.player_id == player.id)
        )

    assert queue_entry is not None
    assert_response(
        button_interaction,
        ["在席を更新しました。次の期限は5分後です。"],
        ephemeral=True,
    )
    assert_deferred_followup_response(button_interaction)
    assert queue_entry.status == MatchQueueEntryStatus.WAITING
    assert queue_entry.notification_channel_id == channel.id
    assert queue_entry.presence_thread_channel_id == thread.id
    assert queue_entry.notification_guild_id == guild.id


def test_matchmaking_presence_thread_leave_button_leaves_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_7
    player = create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    user = FakeUser(
        id=discord_user_id,
        name="thread-user-leave",
        global_name="thread-global-leave",
        nick="thread-guild-leave",
    )
    guild = FakeGuild(id=9_103)
    channel = FakeTextChannel(
        id=9_003,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(channel)
    setup_matchmaking_managed_ui_channel(handlers, channel.id)
    join_interaction = FakeInteraction(
        user=user,
        channel_id=channel.id,
        guild_id=guild.id,
        guild=guild,
    )
    button_interaction = FakeInteraction(
        user=user,
        channel_id=None,
        guild_id=guild.id,
        guild=guild,
    )

    async def scenario() -> None:
        await handlers.join(
            as_interaction(join_interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )

        thread = channel.created_threads[0]
        thread_message = thread.sent_messages[0]
        assert thread_message.view is not None
        leave_button = cast(discord.ui.Button[Any], thread_message.view.children[1])
        button_interaction.channel_id = thread.id
        await leave_button.callback(as_interaction(button_interaction))

    asyncio.run(scenario())

    with session_factory() as verification_session:
        queue_entry = verification_session.scalar(
            select(MatchQueueEntry).where(MatchQueueEntry.player_id == player.id)
        )

    assert queue_entry is not None
    assert_response(
        button_interaction,
        ["キューから退出しました。"],
        ephemeral=True,
    )
    assert queue_entry.status == MatchQueueEntryStatus.LEFT


def test_matchmaking_presence_thread_present_button_rejects_unbound_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_8
    player = create_player(session, discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=matching_queue_service,
    )
    user = FakeUser(
        id=discord_user_id,
        name="stale-thread-user",
        global_name="stale-thread-global",
        nick="stale-thread-guild",
    )
    guild = FakeGuild(id=9_104)
    channel = FakeTextChannel(
        id=9_004,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(channel)
    setup_matchmaking_managed_ui_channel(handlers, channel.id)
    join_interaction = FakeInteraction(
        user=user,
        channel_id=channel.id,
        guild_id=guild.id,
        guild=guild,
    )
    button_interaction = FakeInteraction(
        user=user,
        channel_id=None,
        guild_id=guild.id,
        guild=guild,
    )

    async def scenario() -> None:
        await handlers.join(
            as_interaction(join_interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )

        thread = channel.created_threads[0]
        thread_message = thread.sent_messages[0]
        assert thread_message.view is not None
        present_button = cast(discord.ui.Button[Any], thread_message.view.children[0])
        queue_entry = get_queue_entry(session, player.id)
        matching_queue_service.update_waiting_presence_thread_channel_id(
            queue_entry.id,
            99_004,
        )
        button_interaction.channel_id = thread.id
        await present_button.callback(as_interaction(button_interaction))

    asyncio.run(scenario())

    with session_factory() as verification_session:
        queue_entry = verification_session.scalar(
            select(MatchQueueEntry).where(MatchQueueEntry.player_id == player.id)
        )

    assert queue_entry is not None
    assert_response(
        button_interaction,
        [
            "このスレッドは現在参加中のキューには紐づいていません。再参加する場合は親チャンネルの参加ボタンから参加してください。"
        ],
        ephemeral=True,
    )
    assert queue_entry.status == MatchQueueEntryStatus.WAITING
    assert queue_entry.notification_channel_id == channel.id
    assert queue_entry.presence_thread_channel_id == 99_004
    assert queue_entry.revision == 1


def test_matchmaking_presence_thread_leave_button_rejects_unbound_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_9
    player = create_player(session, discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=matching_queue_service,
    )
    user = FakeUser(
        id=discord_user_id,
        name="stale-thread-user-leave",
        global_name="stale-thread-global-leave",
        nick="stale-thread-guild-leave",
    )
    guild = FakeGuild(id=9_105)
    channel = FakeTextChannel(
        id=9_005,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(channel)
    setup_matchmaking_managed_ui_channel(handlers, channel.id)
    join_interaction = FakeInteraction(
        user=user,
        channel_id=channel.id,
        guild_id=guild.id,
        guild=guild,
    )
    button_interaction = FakeInteraction(
        user=user,
        channel_id=None,
        guild_id=guild.id,
        guild=guild,
    )

    async def scenario() -> None:
        await handlers.join(
            as_interaction(join_interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )

        thread = channel.created_threads[0]
        thread_message = thread.sent_messages[0]
        assert thread_message.view is not None
        leave_button = cast(discord.ui.Button[Any], thread_message.view.children[1])
        queue_entry = get_queue_entry(session, player.id)
        matching_queue_service.update_waiting_presence_thread_channel_id(
            queue_entry.id,
            99_005,
        )
        button_interaction.channel_id = thread.id
        await leave_button.callback(as_interaction(button_interaction))

    asyncio.run(scenario())

    with session_factory() as verification_session:
        queue_entry = verification_session.scalar(
            select(MatchQueueEntry).where(MatchQueueEntry.player_id == player.id)
        )

    assert queue_entry is not None
    assert_response(
        button_interaction,
        [
            "このスレッドは現在参加中のキューには紐づいていません。再参加する場合は親チャンネルの参加ボタンから参加してください。"
        ],
        ephemeral=True,
    )
    assert queue_entry.status == MatchQueueEntryStatus.WAITING
    assert queue_entry.notification_channel_id == channel.id
    assert queue_entry.presence_thread_channel_id == 99_005


def test_present_command_updates_waiting_entry_without_overwriting_notification_context(
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

    with session_factory() as verification_session:
        queue_entry = verification_session.scalar(
            select(MatchQueueEntry).where(MatchQueueEntry.player_id == player.id)
        )

    assert queue_entry is not None

    assert_response(
        interaction,
        ["在席を更新しました。次の期限は5分後です。"],
        ephemeral=True,
    )
    assert queue_entry.notification_channel_id == 5_001
    assert queue_entry.notification_guild_id == 6_001
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


def test_update_matchmaking_status_command_updates_second_message(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 123_456_789_012_345_684_1
    queued_discord_user_id = 123_456_789_012_345_684_2
    create_player(session, executor_discord_user_id)
    queued_player = create_player(session, queued_discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=matching_queue_service,
    )
    guild = FakeGuild(id=9_201)
    command_channel = FakeTextChannel(id=9_202, name="雑談", guild=guild)
    guild.channels.append(command_channel)
    setup_interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(setup_interaction),
            ManagedUiType.MATCHMAKING_CHANNEL.value,
            "レート戦マッチング",
        )
    )

    matchmaking_channel = find_channel_by_name(guild, "レート戦マッチング")
    matching_queue_service.join_queue(
        queued_player.id,
        DEFAULT_MATCH_FORMAT,
        DEFAULT_QUEUE_NAME,
    )
    expected_message = build_matchmaking_status_message(
        matching_queue_service.get_matchmaking_status_snapshot()
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.update_matchmaking_status(as_interaction(interaction)))

    session.expire_all()
    managed_ui_channel = session.scalar(
        select(ManagedUiChannel).where(
            ManagedUiChannel.ui_type == ManagedUiType.MATCHMAKING_CHANNEL
        )
    )

    assert managed_ui_channel is not None
    assert_response(interaction, ["参加状況を更新しました。"], ephemeral=True)
    assert matchmaking_channel.sent_messages[1].content == expected_message
    assert len(matchmaking_channel.sent_messages[1].content.splitlines()) == (
        len(get_match_queue_class_definitions()) + 1
    )
    assert matchmaking_channel.sent_messages[2].content == MATCHMAKING_CHANNEL_MESSAGE
    assert managed_ui_channel.status_message_id == matchmaking_channel.sent_messages[1].id
    assert managed_ui_channel.message_id == matchmaking_channel.sent_messages[2].id


def test_matchmaking_status_button_updates_second_message(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 123_456_789_012_345_684_8
    queued_discord_user_id = 123_456_789_012_345_684_9
    create_player(session, executor_discord_user_id)
    queued_player = create_player(session, queued_discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=matching_queue_service,
    )
    guild = FakeGuild(id=9_261)
    command_channel = FakeTextChannel(id=9_262, name="雑談", guild=guild)
    guild.channels.append(command_channel)
    setup_interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(setup_interaction),
            ManagedUiType.MATCHMAKING_CHANNEL.value,
            "レート戦マッチング",
        )
    )

    matchmaking_channel = find_channel_by_name(guild, "レート戦マッチング")
    matching_queue_service.join_queue(
        queued_player.id,
        DEFAULT_MATCH_FORMAT,
        DEFAULT_QUEUE_NAME,
    )
    expected_message = build_matchmaking_status_message(
        matching_queue_service.get_matchmaking_status_snapshot()
    )
    status_message = matchmaking_channel.sent_messages[1]
    assert status_message.view is not None
    status_button = cast(discord.ui.Button[Any], status_message.view.children[0])
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
        message=status_message,
    )

    asyncio.run(status_button.callback(as_interaction(interaction)))

    assert_response(interaction, ["参加状況を更新しました。"], ephemeral=True)
    assert_deferred_followup_response(interaction)
    assert status_message.content == expected_message
    assert matchmaking_channel.sent_messages[2].content == MATCHMAKING_CHANNEL_MESSAGE


def test_matchmaking_status_button_requires_registered_player(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 123_456_789_012_345_685_0
    create_player(session, executor_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=9_271)
    command_channel = FakeTextChannel(
        id=9_272,
        name="雑談",
        guild=guild,
    )
    guild.channels.append(command_channel)
    setup_interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(setup_interaction),
            ManagedUiType.MATCHMAKING_CHANNEL.value,
            "レート戦マッチング",
        )
    )

    matchmaking_channel = find_channel_by_name(guild, "レート戦マッチング")
    status_message = matchmaking_channel.sent_messages[1]
    assert status_message.view is not None
    status_button = cast(discord.ui.Button[Any], status_message.view.children[0])
    interaction = FakeInteraction(
        user=FakeUser(id=123_456_789_012_345_685_2),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
        message=status_message,
    )

    asyncio.run(status_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


def test_matchmaking_status_button_returns_generic_failure_when_edit_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_685_1
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=9_281)
    command_channel = FakeTextChannel(id=9_282, name="雑談", guild=guild)
    guild.channels.append(command_channel)
    setup_interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(setup_interaction),
            ManagedUiType.MATCHMAKING_CHANNEL.value,
            "レート戦マッチング",
        )
    )

    matchmaking_channel = find_channel_by_name(guild, "レート戦マッチング")
    status_message = matchmaking_channel.sent_messages[1]
    status_message.fail_edit_with = RuntimeError("boom")
    assert status_message.view is not None
    status_button = cast(discord.ui.Button[Any], status_message.view.children[0])
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
        message=status_message,
    )

    asyncio.run(status_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        ["参加状況の更新に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_update_matchmaking_status_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=9_211)
    matchmaking_channel = FakeTextChannel(
        id=9_212,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=123_456_789_012_345_684_3),
        channel_id=9_299,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.update_matchmaking_status(as_interaction(interaction)))

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


def test_update_matchmaking_status_command_returns_generic_failure_when_matchmaking_channel_is_missing(  # noqa: E501
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_684_4
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=9_221)
    command_channel = FakeTextChannel(id=9_222, name="雑談", guild=guild)
    guild.channels.append(command_channel)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.update_matchmaking_status(as_interaction(interaction)))

    assert_response(
        interaction,
        ["参加状況の更新に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_update_matchmaking_status_command_returns_generic_failure_when_status_message_id_is_missing(  # noqa: E501
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_684_5
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=9_231)
    matchmaking_channel = FakeTextChannel(
        id=9_232,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(
        handlers,
        matchmaking_channel.id,
        status_message_id=None,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=9_299_1,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.update_matchmaking_status(as_interaction(interaction)))

    assert_response(
        interaction,
        ["参加状況の更新に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_update_matchmaking_status_command_returns_generic_failure_when_fetch_message_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_684_6
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=9_241)
    matchmaking_channel = FakeTextChannel(
        id=9_242,
        name="レート戦マッチング",
        guild=guild,
        fail_fetch_message_with=RuntimeError("boom"),
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(
        handlers,
        matchmaking_channel.id,
        status_message_id=70_242,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=9_299_2,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.update_matchmaking_status(as_interaction(interaction)))

    assert_response(
        interaction,
        ["参加状況の更新に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_update_matchmaking_status_command_returns_generic_failure_when_edit_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_684_7
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=9_251)
    matchmaking_channel = FakeTextChannel(
        id=9_252,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    guide_message = asyncio.run(matchmaking_channel.send(content="guide"))
    status_message = asyncio.run(matchmaking_channel.send(content="status"))
    ui_message = asyncio.run(matchmaking_channel.send(content=MATCHMAKING_CHANNEL_MESSAGE))
    del guide_message
    status_message.fail_edit_with = RuntimeError("boom")
    setup_matchmaking_managed_ui_channel(
        handlers,
        matchmaking_channel.id,
        message_id=ui_message.id,
        status_message_id=status_message.id,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=9_299_3,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.update_matchmaking_status(as_interaction(interaction)))

    assert_response(
        interaction,
        ["参加状況の更新に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_player_info_command_returns_requesting_player_stats_in_active_info_thread(
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
    guild = FakeGuild(id=14_099)
    info_channel = FakeTextChannel(id=13_099, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        interaction_channel_id=13_198,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(
        interaction,
        ["プレイヤー情報を表示しました。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
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
        ),
    ]
    assert created_thread.sent_messages[1].view is None


def test_info_thread_player_info_initial_message_includes_button(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 223_456_789_012_345_686
    create_player(session, discord_user_id)
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_01)
    info_channel = FakeTextChannel(id=13_099_01, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO,
        interaction_channel_id=13_198_01,
    )

    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
    ]
    assert_info_thread_player_info_initial_controls(created_thread.sent_messages[0].view)


def test_info_thread_player_info_button_posts_current_stats_to_active_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 223_456_789_012_345_687
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
    guild = FakeGuild(id=14_099_02)
    info_channel = FakeTextChannel(id=13_099_02, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO,
        interaction_channel_id=13_198_02,
    )

    initial_message = created_thread.sent_messages[0]
    assert initial_message.view is not None
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[0])
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )

    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert_response(interaction, ["プレイヤー情報を表示しました。"], ephemeral=True)
    assert_deferred_followup_response(interaction)
    assert_all_controls_disabled(initial_message.view)
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
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
        ),
    ]
    assert created_thread.sent_messages[1].view is None


def test_info_thread_player_info_button_rejects_inactive_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 223_456_789_012_345_688
    create_player(session, discord_user_id)
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_03)
    info_channel = FakeTextChannel(id=13_099_03, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    stale_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO,
        interaction_channel_id=13_198_03,
    )
    stale_initial_message = stale_thread.sent_messages[0]
    create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO,
        interaction_channel_id=13_198_04,
    )

    assert stale_initial_message.view is not None
    show_button = cast(discord.ui.Button[Any], stale_initial_message.view.children[0])
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=stale_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=stale_initial_message,
    )

    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        [
            "このスレッドは現在の情報確認用スレッドではありません。"
            "最新の情報確認用スレッドを利用してください。"
        ],
        ephemeral=True,
    )
    assert_all_controls_disabled(stale_initial_message.view)
    assert len(stale_thread.sent_messages) == 1


def test_info_thread_player_info_button_returns_internal_error_and_disables_message(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 223_456_789_012_345_689
    create_player(session, discord_user_id)
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_04)
    info_channel = FakeTextChannel(id=13_099_04, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO,
        interaction_channel_id=13_198_05,
    )
    session.execute(delete(PlayerFormatStats))
    session.execute(delete(Season))
    session.commit()

    initial_message = created_thread.sent_messages[0]
    assert initial_message.view is not None
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[0])
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )

    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        ["プレイヤー情報の取得に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )
    assert_deferred_followup_response(interaction)
    assert_all_controls_disabled(initial_message.view)
    assert len(created_thread.sent_messages) == 1


def test_info_thread_player_info_season_initial_message_includes_controls(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    create_player(session, 223_456_789_012_345_690)
    session.add_all(
        Season(
            name=f"archive-season-{index:02d}",
            start_at=current_time - timedelta(days=31 + index),
            end_at=current_time - timedelta(days=30 + index),
            completed=True,
            completed_at=current_time - timedelta(days=30 + index),
        )
        for index in range(27)
    )
    session.flush()
    expected_seasons = session.scalars(
        select(Season)
        .where(Season.start_at <= current_time)
        .order_by(Season.start_at.desc(), Season.id.desc())
    ).all()
    session.commit()

    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_05)
    info_channel = FakeTextChannel(id=13_099_05, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=223_456_789_012_345_690,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO_SEASON,
        interaction_channel_id=13_198_06,
    )

    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO_SEASON),
    ]
    assert_info_thread_player_info_season_initial_controls(
        created_thread.sent_messages[0].view,
        expected_seasons=expected_seasons,
    )


def test_info_thread_player_info_season_button_requires_season_selection(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 223_456_789_012_345_691
    create_player(session, discord_user_id)
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_06)
    info_channel = FakeTextChannel(id=13_099_06, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO_SEASON,
        interaction_channel_id=13_198_07,
    )

    initial_message = created_thread.sent_messages[0]
    assert initial_message.view is not None
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[1])
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )

    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        [INFO_THREAD_PLAYER_INFO_SEASON_SELECT_SEASON_MESSAGE],
        ephemeral=True,
    )
    assert_deferred_followup_response(interaction)
    assert_all_controls_disabled(initial_message.view)
    assert len(created_thread.sent_messages) == 1


def test_info_thread_player_info_season_button_posts_requested_season_stats_to_active_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="archive-season",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    discord_user_id = 223_456_789_012_345_692
    player = create_player(session, discord_user_id)
    session.add_all(
        (
            PlayerFormatStats(
                player_id=player.id,
                season_id=season.id,
                match_format=MatchFormat.ONE_VS_ONE,
                rating=1500.0,
            ),
            PlayerFormatStats(
                player_id=player.id,
                season_id=season.id,
                match_format=MatchFormat.TWO_VS_TWO,
                rating=1492.0,
                games_played=3,
                wins=1,
                losses=2,
            ),
            PlayerFormatStats(
                player_id=player.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1611.0,
                games_played=6,
                wins=4,
                losses=1,
                draws=1,
                last_played_at=datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc),
            ),
        )
    )
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_07)
    info_channel = FakeTextChannel(id=13_099_07, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO_SEASON,
        interaction_channel_id=13_198_08,
    )

    initial_message = created_thread.sent_messages[0]
    assert initial_message.view is not None
    season_select = cast(discord.ui.Select[Any], initial_message.view.children[0])
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[1])

    set_select_values(season_select, [str(season.id)])
    season_interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
    )
    asyncio.run(season_select.callback(as_interaction(season_interaction)))

    assert initial_message.view is not None
    assert all(
        getattr(child, "disabled", False) is False for child in initial_message.view.children
    )

    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )
    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert season_interaction.response.deferred is True
    assert_response(interaction, ["シーズン別プレイヤー情報を表示しました。"], ephemeral=True)
    assert_all_controls_disabled(initial_message.view)
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO_SEASON),
        format_player_info_message(
            {
                MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.TWO_VS_TWO: (1492.0, 3, 1, 2, 0, None),
                MatchFormat.THREE_VS_THREE: (
                    1611.0,
                    6,
                    4,
                    1,
                    1,
                    datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc),
                ),
            },
            season_id=season.id,
            season_name=season.name,
        ),
    ]
    assert created_thread.sent_messages[1].view is None


def test_info_thread_player_info_season_button_rejects_inactive_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="archive-season",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    discord_user_id = 223_456_789_012_345_693
    create_player(session, discord_user_id)
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_08)
    info_channel = FakeTextChannel(id=13_099_08, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    stale_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO_SEASON,
        interaction_channel_id=13_198_09,
    )
    initial_message = stale_thread.sent_messages[0]
    assert initial_message.view is not None
    season_select = cast(discord.ui.Select[Any], initial_message.view.children[0])
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[1])

    set_select_values(season_select, [str(season.id)])
    asyncio.run(
        season_select.callback(
            as_interaction(
                FakeInteraction(
                    user=FakeUser(id=discord_user_id),
                    channel_id=stale_thread.id,
                    guild_id=guild.id,
                    guild=guild,
                )
            )
        )
    )

    create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO_SEASON,
        interaction_channel_id=13_198_10,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=stale_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )

    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        [
            "このスレッドは現在の情報確認用スレッドではありません。"
            "最新の情報確認用スレッドを利用してください。"
        ],
        ephemeral=True,
    )
    assert_all_controls_disabled(initial_message.view)
    assert len(stale_thread.sent_messages) == 1


def test_player_info_season_command_returns_requested_season_stats_in_active_info_thread(
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
    guild = FakeGuild(id=14_099_1)
    info_channel = FakeTextChannel(id=13_099_1, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_1,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_1,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info_season(as_interaction(interaction), season_pair.upcoming.id))

    assert_response(
        interaction,
        ["シーズン別プレイヤー情報を表示しました。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
        format_player_info_message(
            {
                MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.TWO_VS_TWO: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.THREE_VS_THREE: (1601.0, 4, 3, 1, 0, None),
            },
            season_id=season_pair.upcoming.id,
            season_name="next-spring",
        ),
    ]
    assert created_thread.sent_messages[1].view is None


def test_player_info_season_command_requires_active_info_thread_binding(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_611
    season_pair = ensure_active_and_upcoming_seasons(session)
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.player_info_season(as_interaction(interaction), season_pair.upcoming.id))

    assert_response(
        interaction,
        ["先に /info_thread を実行してください。"],
        ephemeral=True,
    )


def test_player_info_season_command_returns_thread_not_found_when_bound_thread_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_612
    player = create_player(session, discord_user_id)
    season_pair = ensure_active_and_upcoming_seasons(session)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_2)
    info_channel = FakeTextChannel(id=13_099_2, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    handlers.info_thread_binding_service.upsert_latest_thread_channel_id(
        player_id=player.id,
        thread_channel_id=99_002,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_2,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info_season(as_interaction(interaction), season_pair.upcoming.id))

    assert_response(
        interaction,
        ["情報確認用スレッドが見つかりません。先に /info_thread を実行してください。"],
        ephemeral=True,
    )


def test_player_info_season_command_returns_thread_not_found_when_bound_thread_send_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_613
    season_pair = ensure_active_and_upcoming_seasons(session)
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_3)
    info_channel = FakeTextChannel(id=13_099_3, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        interaction_channel_id=13_198_3,
    )
    created_thread.fail_send_with = make_not_found()
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_3,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info_season(as_interaction(interaction), season_pair.upcoming.id))

    assert_response(
        interaction,
        ["情報確認用スレッドが見つかりません。先に /info_thread を実行してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
    ]


def test_player_info_season_command_returns_season_not_found_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_614
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_4)
    info_channel = FakeTextChannel(id=13_099_4, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        interaction_channel_id=13_198_4,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_4,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info_season(as_interaction(interaction), 999_999))

    assert_response(
        interaction,
        ["指定したシーズンが見つかりません。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
    ]


def test_player_info_season_command_returns_stats_not_found_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_615
    create_player(session, discord_user_id)
    missing_stats_season = Season(
        name="archive-cup",
        start_at=datetime(2025, 1, 13, 15, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2025, 2, 13, 15, 0, 0, tzinfo=timezone.utc),
        completed=True,
        completed_at=datetime(2025, 2, 13, 15, 0, 0, tzinfo=timezone.utc),
    )
    session.add(missing_stats_season)
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_5)
    info_channel = FakeTextChannel(id=13_099_5, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        interaction_channel_id=13_198_5,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_5,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.player_info_season(
            as_interaction(interaction),
            missing_stats_season.id,
        )
    )

    assert_response(
        interaction,
        ["指定したシーズンのプレイヤー情報はありません。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
    ]


def test_player_info_season_command_returns_internal_error_message_when_lookup_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    class FailingSeasonPlayerLookupService:
        def get_player_info_by_discord_user_id_and_season_id(
            self,
            discord_user_id: int,
            season_id: int,
        ) -> None:
            raise RuntimeError(f"boom discord_user_id={discord_user_id} season_id={season_id}")

    discord_user_id = 123_456_789_012_345_616
    season_pair = ensure_active_and_upcoming_seasons(session)
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_6)
    info_channel = FakeTextChannel(id=13_099_6, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        interaction_channel_id=13_198_6,
    )
    handlers.player_lookup_service = cast(Any, FailingSeasonPlayerLookupService())
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_6,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info_season(as_interaction(interaction), season_pair.upcoming.id))

    assert_response(
        interaction,
        ["シーズン別プレイヤー情報の取得に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
    ]


def test_player_info_season_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_617))

    asyncio.run(handlers.player_info_season(as_interaction(interaction), 1))

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


def test_leaderboard_command_posts_current_leaderboard_to_active_info_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    requesting_discord_user_id = 123_456_789_012_345_620
    bob_discord_user_id = 123_456_789_012_345_621
    carol_discord_user_id = 123_456_789_012_345_622
    alice = create_player(session, requesting_discord_user_id)
    bob = create_player(session, bob_discord_user_id)
    carol = create_player(session, carol_discord_user_id)
    season_pair = ensure_active_and_upcoming_seasons(session)

    alice_stats = get_player_format_stats(session, alice.id)
    bob_stats = get_player_format_stats(session, bob.id)
    carol_stats = get_player_format_stats(session, carol.id)
    alice_stats.rating = 1600
    alice_stats.games_played = 2
    alice_stats.wins = 2
    bob_stats.rating = 1600
    bob_stats.games_played = 5
    bob_stats.wins = 4
    bob_stats.losses = 1
    carol_stats.rating = 1600
    carol_stats.games_played = 5
    carol_stats.wins = 3
    carol_stats.losses = 2
    alice.display_name = "Alice"
    bob.display_name = "Bob"
    carol.display_name = "Carol"
    current_snapshot_date = resolve_snapshot_date(get_database_now(session))
    session.add_all(
        (
            LeaderboardSnapshot(
                snapshot_date=current_snapshot_date - timedelta(days=1),
                season_id=season_pair.active.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=bob.id,
                rank=2,
                rating=1590,
                games_played=4,
            ),
            LeaderboardSnapshot(
                snapshot_date=current_snapshot_date - timedelta(days=1),
                season_id=season_pair.active.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=carol.id,
                rank=1,
                rating=1610,
                games_played=4,
            ),
            LeaderboardSnapshot(
                snapshot_date=current_snapshot_date - timedelta(days=3),
                season_id=season_pair.active.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=alice.id,
                rank=1,
                rating=1650,
                games_played=1,
            ),
            LeaderboardSnapshot(
                snapshot_date=current_snapshot_date - timedelta(days=3),
                season_id=season_pair.active.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=bob.id,
                rank=3,
                rating=1580,
                games_played=3,
            ),
        )
    )
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_7)
    info_channel = FakeTextChannel(id=13_099_7, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7,
        user_name="Alice",
    )
    interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Alice"),
        channel_id=13_197_7,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard(
            as_interaction(interaction),
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["ランキングを表示しました。"],
        ephemeral=True,
    )
    assert_info_thread_leaderboard_initial_controls(created_thread.sent_messages[0].view)
    assert created_thread.sent_messages[1].view is None
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
        format_leaderboard_message(
            season_name=season_pair.active.name,
            match_format=MatchFormat.THREE_VS_THREE,
            page=1,
            entries=[
                (1, "Bob", 1600.0, 1, 2, None),
                (2, "Carol", 1600.0, -1, None, None),
                (3, "Alice", 1600.0, None, -2, None),
            ],
        ),
    ]


def test_info_thread_leaderboard_initial_message_includes_controls(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_620_1
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_7_1)
    info_channel = FakeTextChannel(id=13_099_7_1, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)

    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_1,
    )

    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
    ]
    assert_info_thread_leaderboard_initial_controls(created_thread.sent_messages[0].view)


def test_info_thread_leaderboard_button_requires_match_format_selection(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_620_2
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_7_2)
    info_channel = FakeTextChannel(id=13_099_7_2, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_2,
    )

    initial_message = created_thread.sent_messages[0]
    assert initial_message.view is not None
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[1])
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )

    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        [INFO_THREAD_LEADERBOARD_SELECT_MATCH_FORMAT_MESSAGE],
        ephemeral=True,
    )
    assert_deferred_followup_response(interaction)
    assert_all_controls_disabled(initial_message.view)
    assert len(created_thread.sent_messages) == 1


def test_info_thread_leaderboard_button_posts_page_one_to_active_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    requesting_discord_user_id = 123_456_789_012_345_620_3
    bob_discord_user_id = 123_456_789_012_345_621_3
    carol_discord_user_id = 123_456_789_012_345_622_3
    alice = create_player(session, requesting_discord_user_id)
    bob = create_player(session, bob_discord_user_id)
    carol = create_player(session, carol_discord_user_id)
    season_pair = ensure_active_and_upcoming_seasons(session)

    alice_stats = get_player_format_stats(session, alice.id)
    bob_stats = get_player_format_stats(session, bob.id)
    carol_stats = get_player_format_stats(session, carol.id)
    alice_stats.rating = 1600
    alice_stats.games_played = 2
    alice_stats.wins = 2
    bob_stats.rating = 1600
    bob_stats.games_played = 5
    bob_stats.wins = 4
    bob_stats.losses = 1
    carol_stats.rating = 1600
    carol_stats.games_played = 5
    carol_stats.wins = 3
    carol_stats.losses = 2
    alice.display_name = "Alice"
    bob.display_name = "Bob"
    carol.display_name = "Carol"
    current_snapshot_date = resolve_snapshot_date(get_database_now(session))
    session.add_all(
        (
            LeaderboardSnapshot(
                snapshot_date=current_snapshot_date - timedelta(days=1),
                season_id=season_pair.active.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=bob.id,
                rank=2,
                rating=1590,
                games_played=4,
            ),
            LeaderboardSnapshot(
                snapshot_date=current_snapshot_date - timedelta(days=1),
                season_id=season_pair.active.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=carol.id,
                rank=1,
                rating=1610,
                games_played=4,
            ),
            LeaderboardSnapshot(
                snapshot_date=current_snapshot_date - timedelta(days=3),
                season_id=season_pair.active.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=alice.id,
                rank=1,
                rating=1650,
                games_played=1,
            ),
            LeaderboardSnapshot(
                snapshot_date=current_snapshot_date - timedelta(days=3),
                season_id=season_pair.active.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=bob.id,
                rank=3,
                rating=1580,
                games_played=3,
            ),
        )
    )
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_7_3)
    info_channel = FakeTextChannel(id=13_099_7_3, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_3,
        user_name="Alice",
    )
    initial_message = created_thread.sent_messages[0]
    assert initial_message.view is not None
    match_format_select = cast(discord.ui.Select[Any], initial_message.view.children[0])
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[1])
    user = FakeUser(id=requesting_discord_user_id, name="Alice")

    set_select_values(match_format_select, [MatchFormat.THREE_VS_THREE.value])
    select_interaction = FakeInteraction(
        user=user,
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
    )
    asyncio.run(match_format_select.callback(as_interaction(select_interaction)))

    assert initial_message.view is not None
    assert all(
        getattr(child, "disabled", False) is False for child in initial_message.view.children
    )

    interaction = FakeInteraction(
        user=user,
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )
    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert select_interaction.response.deferred is True
    assert select_interaction.response.messages == []
    assert_response(
        interaction,
        ["ランキングを表示しました。"],
        ephemeral=True,
    )
    assert_all_controls_disabled(initial_message.view)
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
        format_leaderboard_message(
            season_name=season_pair.active.name,
            match_format=MatchFormat.THREE_VS_THREE,
            page=1,
            entries=[
                (1, "Bob", 1600.0, 1, 2, None),
                (2, "Carol", 1600.0, -1, None, None),
                (3, "Alice", 1600.0, None, -2, None),
            ],
        ),
    ]
    assert created_thread.sent_messages[1].view is None


def test_leaderboard_command_adds_next_page_button_when_next_page_exists(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    requesting_discord_user_id = 123_456_789_012_345_620_4
    requester = create_player(session, requesting_discord_user_id)
    additional_players = create_players(
        session,
        20,
        start_discord_user_id=123_456_789_012_345_720,
    )
    season_pair = ensure_active_and_upcoming_seasons(session)
    all_players = [requester, *additional_players]
    for index, player in enumerate(all_players):
        format_stats = get_player_format_stats(session, player.id)
        format_stats.match_format = MatchFormat.THREE_VS_THREE
        format_stats.rating = 2000 - index
        format_stats.games_played = 1
        format_stats.wins = 1
        player.display_name = f"Player {index + 1}"
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_7_4)
    info_channel = FakeTextChannel(id=13_099_7_4, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_4,
        user_name="Player 1",
    )
    interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
        channel_id=13_197_7_4,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard(
            as_interaction(interaction),
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["ランキングを表示しました。"],
        ephemeral=True,
    )
    assert_info_thread_leaderboard_initial_controls(created_thread.sent_messages[0].view)
    assert_info_thread_leaderboard_next_page_control(created_thread.sent_messages[1].view)
    assert created_thread.sent_messages[1].content == format_leaderboard_message(
        season_name=season_pair.active.name,
        match_format=MatchFormat.THREE_VS_THREE,
        page=1,
        entries=[
            (index + 1, f"Player {index + 1}", float(2000 - index), None, None, None)
            for index in range(20)
        ],
    )


def test_info_thread_leaderboard_next_page_button_posts_requested_page(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    requesting_discord_user_id = 123_456_789_012_345_620_5
    requester = create_player(session, requesting_discord_user_id)
    additional_players = create_players(
        session,
        20,
        start_discord_user_id=123_456_789_012_345_740,
    )
    season_pair = ensure_active_and_upcoming_seasons(session)
    all_players = [requester, *additional_players]
    for index, player in enumerate(all_players):
        format_stats = get_player_format_stats(session, player.id)
        format_stats.rating = 2000 - index
        format_stats.games_played = 1
        format_stats.wins = 1
        player.display_name = f"Player {index + 1}"
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_7_5)
    info_channel = FakeTextChannel(id=13_099_7_5, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_5,
        user_name="Player 1",
    )
    interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
        channel_id=13_197_7_5,
        guild_id=guild.id,
        guild=guild,
    )

    async def scenario() -> None:
        await handlers.leaderboard(
            as_interaction(interaction),
            MatchFormat.THREE_VS_THREE.value,
            1,
        )

        next_page_message = created_thread.sent_messages[1]
        assert next_page_message.view is not None
        next_page_button = cast(discord.ui.Button[Any], next_page_message.view.children[0])
        button_interaction = FakeInteraction(
            user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
            channel_id=created_thread.id,
            guild_id=guild.id,
            guild=guild,
            message=next_page_message,
        )
        await next_page_button.callback(as_interaction(button_interaction))

        assert_response(
            button_interaction,
            ["ランキングを表示しました。"],
            ephemeral=True,
        )
        assert_deferred_followup_response(button_interaction)
        assert_all_controls_disabled(next_page_message.view)

    asyncio.run(scenario())

    assert created_thread.sent_messages[2].content == format_leaderboard_message(
        season_name=season_pair.active.name,
        match_format=MatchFormat.THREE_VS_THREE,
        page=2,
        entries=[
            (21, "Player 21", 1980.0, None, None, None),
        ],
    )
    assert created_thread.sent_messages[2].view is None


def test_info_thread_leaderboard_button_rejects_inactive_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_620_6
    player = create_player(session, discord_user_id)
    format_stats = get_player_format_stats(session, player.id)
    format_stats.games_played = 1
    format_stats.wins = 1
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_7_6)
    info_channel = FakeTextChannel(id=13_099_7_6, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    stale_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_6,
    )
    stale_initial_message = stale_thread.sent_messages[0]
    assert stale_initial_message.view is not None
    match_format_select = cast(discord.ui.Select[Any], stale_initial_message.view.children[0])
    show_button = cast(discord.ui.Button[Any], stale_initial_message.view.children[1])
    user = FakeUser(id=discord_user_id)

    set_select_values(match_format_select, [MatchFormat.THREE_VS_THREE.value])
    asyncio.run(
        match_format_select.callback(
            as_interaction(
                FakeInteraction(
                    user=user,
                    channel_id=stale_thread.id,
                    guild_id=guild.id,
                    guild=guild,
                )
            )
        )
    )

    create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_6_1,
    )
    stale_button_interaction = FakeInteraction(
        user=user,
        channel_id=stale_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=stale_initial_message,
    )

    asyncio.run(show_button.callback(as_interaction(stale_button_interaction)))

    assert_response(
        stale_button_interaction,
        [
            "このスレッドは現在の情報確認用スレッドではありません。最新の情報確認用スレッドを利用してください。"
        ],
        ephemeral=True,
    )
    assert_all_controls_disabled(stale_initial_message.view)
    assert len(stale_thread.sent_messages) == 1


def test_info_thread_leaderboard_next_page_button_rejects_inactive_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    requesting_discord_user_id = 123_456_789_012_345_620_7
    requester = create_player(session, requesting_discord_user_id)
    additional_players = create_players(
        session,
        20,
        start_discord_user_id=123_456_789_012_345_760,
    )
    all_players = [requester, *additional_players]
    for index, player in enumerate(all_players):
        format_stats = get_player_format_stats(session, player.id)
        format_stats.rating = 2000 - index
        format_stats.games_played = 1
        format_stats.wins = 1
        player.display_name = f"Player {index + 1}"
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_7_7)
    info_channel = FakeTextChannel(id=13_099_7_7, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    stale_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_7,
        user_name="Player 1",
    )
    interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
        channel_id=13_197_7_7,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard(
            as_interaction(interaction),
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    next_page_message = stale_thread.sent_messages[1]
    assert next_page_message.view is not None
    next_page_button = cast(discord.ui.Button[Any], next_page_message.view.children[0])

    create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_7_7_1,
        user_name="Player 1",
    )
    stale_button_interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
        channel_id=stale_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=next_page_message,
    )

    asyncio.run(next_page_button.callback(as_interaction(stale_button_interaction)))

    assert_response(
        stale_button_interaction,
        [
            "このスレッドは現在の情報確認用スレッドではありません。最新の情報確認用スレッドを利用してください。"
        ],
        ephemeral=True,
    )
    assert_all_controls_disabled(next_page_message.view)
    assert len(stale_thread.sent_messages) == 2


def test_leaderboard_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_623))

    asyncio.run(
        handlers.leaderboard(as_interaction(interaction), MatchFormat.THREE_VS_THREE.value, 1)
    )

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


def test_leaderboard_command_requires_active_info_thread_binding(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_624
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(
        handlers.leaderboard(as_interaction(interaction), MatchFormat.THREE_VS_THREE.value, 1)
    )

    assert_response(
        interaction,
        ["先に /info_thread を実行してください。"],
        ephemeral=True,
    )


def test_leaderboard_command_returns_thread_not_found_when_bound_thread_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_625
    player = create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_8)
    info_channel = FakeTextChannel(id=13_099_8, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    handlers.info_thread_binding_service.upsert_latest_thread_channel_id(
        player_id=player.id,
        thread_channel_id=99_003,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_8,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard(as_interaction(interaction), MatchFormat.THREE_VS_THREE.value, 1)
    )

    assert_response(
        interaction,
        ["情報確認用スレッドが見つかりません。先に /info_thread を実行してください。"],
        ephemeral=True,
    )


def test_leaderboard_command_returns_thread_not_found_when_bound_thread_send_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_626
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_099_9)
    info_channel = FakeTextChannel(id=13_099_9, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_9,
    )
    create_player(session, 123_456_789_012_345_627)
    get_player_format_stats(session, 2).games_played = 1
    session.commit()
    created_thread.fail_send_with = make_not_found()
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_9,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard(as_interaction(interaction), MatchFormat.THREE_VS_THREE.value, 1)
    )

    assert_response(
        interaction,
        ["情報確認用スレッドが見つかりません。先に /info_thread を実行してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
    ]


def test_leaderboard_command_returns_invalid_page_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_628
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_0)
    info_channel = FakeTextChannel(id=13_100_0, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_0,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_0,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard(as_interaction(interaction), MatchFormat.THREE_VS_THREE.value, 0)
    )

    assert_response(
        interaction,
        ["page は 1 以上で指定してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
    ]


def test_leaderboard_command_returns_invalid_match_format_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_629
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_1)
    info_channel = FakeTextChannel(id=13_100_1, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_1,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_1,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.leaderboard(as_interaction(interaction), "invalid", 1))

    assert_response(
        interaction,
        ["指定したフォーマットは存在しません。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
    ]


def test_leaderboard_command_returns_page_not_found_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_630
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_2)
    info_channel = FakeTextChannel(id=13_100_2, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_2,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_2,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard(as_interaction(interaction), MatchFormat.THREE_VS_THREE.value, 1)
    )

    assert_response(
        interaction,
        ["指定したページにはランキングがありません。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
    ]


def test_leaderboard_command_returns_internal_error_message_when_lookup_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    class FailingLeaderboardService:
        def get_current_leaderboard_page(
            self,
            match_format: str,
            page: int,
        ) -> None:
            raise RuntimeError(f"boom match_format={match_format} page={page}")

    discord_user_id = 123_456_789_012_345_631
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    handlers.leaderboard_service = cast(Any, FailingLeaderboardService())
    guild = FakeGuild(id=14_100_3)
    info_channel = FakeTextChannel(id=13_100_3, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_198_3,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_3,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard(as_interaction(interaction), MatchFormat.THREE_VS_THREE.value, 1)
    )

    assert_response(
        interaction,
        ["ランキングの取得に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
    ]


def test_info_thread_leaderboard_season_initial_message_includes_controls(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    create_player(session, 123_456_789_012_345_631)
    session.add_all(
        Season(
            name=f"archive-{index:02d}",
            start_at=current_time - timedelta(days=31 + index),
            end_at=current_time - timedelta(days=30 + index),
            completed=True,
            completed_at=current_time - timedelta(days=30 + index),
        )
        for index in range(27)
    )
    session.flush()
    expected_seasons = session.scalars(
        select(Season)
        .where(Season.start_at <= current_time)
        .order_by(Season.start_at.desc(), Season.id.desc())
    ).all()
    session.commit()

    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_3)
    info_channel = FakeTextChannel(id=13_100_3, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=123_456_789_012_345_631,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3,
    )

    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
    ]
    assert_info_thread_leaderboard_season_initial_controls(
        created_thread.sent_messages[0].view,
        expected_seasons=expected_seasons,
    )


@pytest.mark.parametrize(
    ("select_season", "select_match_format", "expected_message"),
    [
        (False, False, INFO_THREAD_LEADERBOARD_SEASON_SELECT_BOTH_MESSAGE),
        (False, True, INFO_THREAD_LEADERBOARD_SEASON_SELECT_SEASON_MESSAGE),
        (True, False, INFO_THREAD_LEADERBOARD_SELECT_MATCH_FORMAT_MESSAGE),
    ],
)
def test_info_thread_leaderboard_season_button_requires_required_selections(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    select_season: bool,
    select_match_format: bool,
    expected_message: str,
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    discord_user_id = 123_456_789_012_345_632_1
    create_player(session, discord_user_id)
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_3_1)
    info_channel = FakeTextChannel(id=13_100_3_1, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3_1,
    )

    initial_message = created_thread.sent_messages[0]
    assert initial_message.view is not None
    season_select = cast(discord.ui.Select[Any], initial_message.view.children[0])
    match_format_select = cast(discord.ui.Select[Any], initial_message.view.children[1])
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[2])

    if select_season:
        set_select_values(season_select, [str(season.id)])
        asyncio.run(
            season_select.callback(
                as_interaction(
                    FakeInteraction(
                        user=FakeUser(id=discord_user_id),
                        channel_id=created_thread.id,
                        guild_id=guild.id,
                        guild=guild,
                    )
                )
            )
        )
    if select_match_format:
        set_select_values(match_format_select, [MatchFormat.THREE_VS_THREE.value])
        asyncio.run(
            match_format_select.callback(
                as_interaction(
                    FakeInteraction(
                        user=FakeUser(id=discord_user_id),
                        channel_id=created_thread.id,
                        guild_id=guild.id,
                        guild=guild,
                    )
                )
            )
        )

    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )
    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert_response(interaction, [expected_message], ephemeral=True)
    assert_all_controls_disabled(initial_message.view)
    assert len(created_thread.sent_messages) == 1


def test_info_thread_leaderboard_season_button_posts_page_one_to_active_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    requesting_discord_user_id = 123_456_789_012_345_632_2
    bob_discord_user_id = 123_456_789_012_345_633
    carol_discord_user_id = 123_456_789_012_345_634
    alice = create_player(session, requesting_discord_user_id)
    bob = create_player(session, bob_discord_user_id)
    carol = create_player(session, carol_discord_user_id)
    session.add_all(
        (
            PlayerFormatStats(
                player_id=alice.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1600,
                games_played=2,
                wins=2,
            ),
            PlayerFormatStats(
                player_id=bob.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1610,
                games_played=5,
                wins=4,
                losses=1,
            ),
            PlayerFormatStats(
                player_id=carol.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1600,
                games_played=5,
                wins=3,
                losses=2,
            ),
        )
    )
    alice.display_name = "Alice"
    bob.display_name = "Bob"
    carol.display_name = "Carol"
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_3_2)
    info_channel = FakeTextChannel(id=13_100_3_2, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3_2,
        user_name="Alice",
    )

    initial_message = created_thread.sent_messages[0]
    assert initial_message.view is not None
    season_select = cast(discord.ui.Select[Any], initial_message.view.children[0])
    match_format_select = cast(discord.ui.Select[Any], initial_message.view.children[1])
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[2])
    user = FakeUser(id=requesting_discord_user_id, name="Alice")

    set_select_values(season_select, [str(season.id)])
    season_interaction = FakeInteraction(
        user=user,
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
    )
    asyncio.run(season_select.callback(as_interaction(season_interaction)))

    set_select_values(match_format_select, [MatchFormat.THREE_VS_THREE.value])
    match_format_interaction = FakeInteraction(
        user=user,
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
    )
    asyncio.run(match_format_select.callback(as_interaction(match_format_interaction)))

    assert initial_message.view is not None
    assert all(
        getattr(child, "disabled", False) is False for child in initial_message.view.children
    )

    interaction = FakeInteraction(
        user=user,
        channel_id=created_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )
    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert season_interaction.response.deferred is True
    assert match_format_interaction.response.deferred is True
    assert_response(interaction, ["ランキングを表示しました。"], ephemeral=True)
    assert_all_controls_disabled(initial_message.view)
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
        format_leaderboard_season_message(
            season_id=season.id,
            season_name=season.name,
            match_format=MatchFormat.THREE_VS_THREE,
            page=1,
            entries=[
                (1, "Bob", 1610.0),
                (2, "Carol", 1600.0),
                (3, "Alice", 1600.0),
            ],
        ),
    ]
    assert created_thread.sent_messages[1].view is None


def test_leaderboard_season_command_adds_next_page_button_when_next_page_exists(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202601delta",
        start_at=current_time - timedelta(days=70),
        end_at=current_time - timedelta(days=40),
        completed=True,
        completed_at=current_time - timedelta(days=40),
    )
    session.add(season)
    session.flush()
    requesting_discord_user_id = 123_456_789_012_345_632_3
    requester = create_player(session, requesting_discord_user_id)
    additional_players = create_players(
        session,
        20,
        start_discord_user_id=123_456_789_012_345_820,
    )
    all_players = [requester, *additional_players]
    for index, player in enumerate(all_players):
        session.add(
            PlayerFormatStats(
                player_id=player.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=2000 - index,
                games_played=1,
                wins=1,
            )
        )
        player.display_name = f"Player {index + 1}"
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_3_3)
    info_channel = FakeTextChannel(id=13_100_3_3, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3_3,
        user_name="Player 1",
    )
    interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
        channel_id=13_197_3_3,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    expected_started_seasons = session.scalars(
        select(Season)
        .where(Season.start_at <= current_time)
        .order_by(Season.start_at.desc(), Season.id.desc())
    ).all()
    assert_response(interaction, ["ランキングを表示しました。"], ephemeral=True)
    assert_info_thread_leaderboard_season_initial_controls(
        created_thread.sent_messages[0].view,
        expected_seasons=expected_started_seasons,
    )
    assert_info_thread_leaderboard_next_page_control(created_thread.sent_messages[1].view)
    assert created_thread.sent_messages[1].content == format_leaderboard_season_message(
        season_id=season.id,
        season_name=season.name,
        match_format=MatchFormat.THREE_VS_THREE,
        page=1,
        entries=[
            (
                index + 1,
                f"Player {index + 1}",
                float(2000 - index),
            )
            for index in range(20)
        ],
    )


def test_info_thread_leaderboard_season_next_page_button_posts_requested_page(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202601delta",
        start_at=current_time - timedelta(days=70),
        end_at=current_time - timedelta(days=40),
        completed=True,
        completed_at=current_time - timedelta(days=40),
    )
    session.add(season)
    session.flush()
    requesting_discord_user_id = 123_456_789_012_345_632_4
    requester = create_player(session, requesting_discord_user_id)
    additional_players = create_players(
        session,
        20,
        start_discord_user_id=123_456_789_012_345_840,
    )
    all_players = [requester, *additional_players]
    for index, player in enumerate(all_players):
        session.add(
            PlayerFormatStats(
                player_id=player.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=2000 - index,
                games_played=1,
                wins=1,
            )
        )
        player.display_name = f"Player {index + 1}"
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_3_4)
    info_channel = FakeTextChannel(id=13_100_3_4, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3_4,
        user_name="Player 1",
    )
    interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
        channel_id=13_197_3_4,
        guild_id=guild.id,
        guild=guild,
    )

    async def scenario() -> None:
        await handlers.leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )

        next_page_message = created_thread.sent_messages[1]
        assert next_page_message.view is not None
        next_page_button = cast(discord.ui.Button[Any], next_page_message.view.children[0])
        button_interaction = FakeInteraction(
            user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
            channel_id=created_thread.id,
            guild_id=guild.id,
            guild=guild,
            message=next_page_message,
        )
        await next_page_button.callback(as_interaction(button_interaction))

        assert_response(button_interaction, ["ランキングを表示しました。"], ephemeral=True)
        assert_all_controls_disabled(next_page_message.view)

    asyncio.run(scenario())

    assert created_thread.sent_messages[2].content == format_leaderboard_season_message(
        season_id=season.id,
        season_name=season.name,
        match_format=MatchFormat.THREE_VS_THREE,
        page=2,
        entries=[(21, "Player 21", 1980.0)],
    )
    assert created_thread.sent_messages[2].view is None


def test_info_thread_leaderboard_season_button_rejects_inactive_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    discord_user_id = 123_456_789_012_345_632_5
    player = create_player(session, discord_user_id)
    session.add(
        PlayerFormatStats(
            player_id=player.id,
            season_id=season.id,
            match_format=MatchFormat.THREE_VS_THREE,
            rating=1600,
            games_played=1,
            wins=1,
        )
    )
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_3_5)
    info_channel = FakeTextChannel(id=13_100_3_5, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    stale_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3_5,
    )
    initial_message = stale_thread.sent_messages[0]
    assert initial_message.view is not None
    season_select = cast(discord.ui.Select[Any], initial_message.view.children[0])
    match_format_select = cast(discord.ui.Select[Any], initial_message.view.children[1])
    show_button = cast(discord.ui.Button[Any], initial_message.view.children[2])
    user = FakeUser(id=discord_user_id)

    set_select_values(season_select, [str(season.id)])
    asyncio.run(
        season_select.callback(
            as_interaction(
                FakeInteraction(
                    user=user,
                    channel_id=stale_thread.id,
                    guild_id=guild.id,
                    guild=guild,
                )
            )
        )
    )
    set_select_values(match_format_select, [MatchFormat.THREE_VS_THREE.value])
    asyncio.run(
        match_format_select.callback(
            as_interaction(
                FakeInteraction(
                    user=user,
                    channel_id=stale_thread.id,
                    guild_id=guild.id,
                    guild=guild,
                )
            )
        )
    )

    create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3_5_1,
    )
    interaction = FakeInteraction(
        user=user,
        channel_id=stale_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=initial_message,
    )

    asyncio.run(show_button.callback(as_interaction(interaction)))

    assert_response(
        interaction,
        [
            "このスレッドは現在の情報確認用スレッドではありません。最新の情報確認用スレッドを利用してください。"
        ],
        ephemeral=True,
    )
    assert_all_controls_disabled(initial_message.view)
    assert len(stale_thread.sent_messages) == 1


def test_info_thread_leaderboard_season_next_page_button_rejects_inactive_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202601delta",
        start_at=current_time - timedelta(days=70),
        end_at=current_time - timedelta(days=40),
        completed=True,
        completed_at=current_time - timedelta(days=40),
    )
    session.add(season)
    session.flush()
    requesting_discord_user_id = 123_456_789_012_345_632_6
    requester = create_player(session, requesting_discord_user_id)
    additional_players = create_players(
        session,
        20,
        start_discord_user_id=123_456_789_012_345_860,
    )
    all_players = [requester, *additional_players]
    for index, player in enumerate(all_players):
        session.add(
            PlayerFormatStats(
                player_id=player.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=2000 - index,
                games_played=1,
                wins=1,
            )
        )
        player.display_name = f"Player {index + 1}"
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_3_6)
    info_channel = FakeTextChannel(id=13_100_3_6, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    stale_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3_6,
        user_name="Player 1",
    )
    interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
        channel_id=13_197_3_6,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    next_page_message = stale_thread.sent_messages[1]
    assert next_page_message.view is not None
    next_page_button = cast(discord.ui.Button[Any], next_page_message.view.children[0])

    create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_3_6_1,
        user_name="Player 1",
    )
    button_interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Player 1"),
        channel_id=stale_thread.id,
        guild_id=guild.id,
        guild=guild,
        message=next_page_message,
    )

    asyncio.run(next_page_button.callback(as_interaction(button_interaction)))

    assert_response(
        button_interaction,
        [
            "このスレッドは現在の情報確認用スレッドではありません。最新の情報確認用スレッドを利用してください。"
        ],
        ephemeral=True,
    )
    assert_all_controls_disabled(next_page_message.view)
    assert len(stale_thread.sent_messages) == 2


def test_leaderboard_season_command_posts_requested_season_leaderboard_to_info_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    requesting_discord_user_id = 123_456_789_012_345_632
    bob_discord_user_id = 123_456_789_012_345_633
    carol_discord_user_id = 123_456_789_012_345_634
    alice = create_player(session, requesting_discord_user_id)
    bob = create_player(session, bob_discord_user_id)
    carol = create_player(session, carol_discord_user_id)
    session.add_all(
        (
            PlayerFormatStats(
                player_id=alice.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1600,
                games_played=2,
                wins=2,
            ),
            PlayerFormatStats(
                player_id=bob.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1610,
                games_played=5,
                wins=4,
                losses=1,
            ),
            PlayerFormatStats(
                player_id=carol.id,
                season_id=season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1600,
                games_played=5,
                wins=3,
                losses=2,
            ),
        )
    )
    alice.display_name = "Alice"
    bob.display_name = "Bob"
    carol.display_name = "Carol"
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_4)
    info_channel = FakeTextChannel(id=13_100_4, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=requesting_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_4,
        user_name="Alice",
    )
    interaction = FakeInteraction(
        user=FakeUser(id=requesting_discord_user_id, name="Alice"),
        channel_id=13_197_4,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(interaction, ["ランキングを表示しました。"], ephemeral=True)
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
        format_leaderboard_season_message(
            season_id=season.id,
            season_name=season.name,
            match_format=MatchFormat.THREE_VS_THREE,
            page=1,
            entries=[
                (1, "Bob", 1610.0),
                (2, "Carol", 1600.0),
                (3, "Alice", 1600.0),
            ],
        ),
    ]


def test_leaderboard_season_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_635))

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            1,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


def test_leaderboard_season_command_requires_active_info_thread_binding(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_636
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            1,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(interaction, ["先に /info_thread を実行してください。"], ephemeral=True)


def test_leaderboard_season_command_returns_thread_not_found_when_bound_thread_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_637
    player = create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_5)
    info_channel = FakeTextChannel(id=13_100_5, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    handlers.info_thread_binding_service.upsert_latest_thread_channel_id(
        player_id=player.id,
        thread_channel_id=99_004,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_5,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            1,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["情報確認用スレッドが見つかりません。先に /info_thread を実行してください。"],
        ephemeral=True,
    )


def test_leaderboard_season_command_returns_thread_not_found_when_bound_thread_send_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    discord_user_id = 123_456_789_012_345_638
    player = create_player(session, discord_user_id)
    session.add(
        PlayerFormatStats(
            player_id=player.id,
            season_id=season.id,
            match_format=MatchFormat.THREE_VS_THREE,
            rating=1600,
            games_played=1,
            wins=1,
        )
    )
    session.commit()
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_6)
    info_channel = FakeTextChannel(id=13_100_6, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_6,
    )
    created_thread.fail_send_with = make_not_found()
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_6,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["情報確認用スレッドが見つかりません。先に /info_thread を実行してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
    ]


def test_leaderboard_season_command_returns_invalid_page_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    discord_user_id = 123_456_789_012_345_639
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_7)
    info_channel = FakeTextChannel(id=13_100_7, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_7,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_7,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            0,
        )
    )

    assert_response(interaction, ["page は 1 以上で指定してください。"], ephemeral=True)
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
    ]


def test_leaderboard_season_command_returns_invalid_match_format_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    discord_user_id = 123_456_789_012_345_640
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_8)
    info_channel = FakeTextChannel(id=13_100_8, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_8,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_8,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.leaderboard_season(as_interaction(interaction), season.id, "invalid", 1))

    assert_response(
        interaction,
        ["指定したフォーマットは存在しません。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
    ]


def test_leaderboard_season_command_returns_missing_season_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_641
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_100_9)
    info_channel = FakeTextChannel(id=13_100_9, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_198_9,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_197_9,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            999_999,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["指定したシーズンが見つかりません。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
    ]


def test_leaderboard_season_command_returns_not_started_season_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="future-season",
        start_at=current_time + timedelta(days=1),
        end_at=current_time + timedelta(days=31),
        completed=False,
        completed_at=None,
    )
    session.add(season)
    session.flush()
    discord_user_id = 123_456_789_012_345_642
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_101_0)
    info_channel = FakeTextChannel(id=13_101_0, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_199_0,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_198_0,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["指定したシーズンはまだ開始していません。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
    ]


def test_leaderboard_season_command_returns_page_not_found_without_posting_to_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    discord_user_id = 123_456_789_012_345_643
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_101_1)
    info_channel = FakeTextChannel(id=13_101_1, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_199_1,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_198_1,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["指定したページにはランキングがありません。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
    ]


def test_leaderboard_season_command_returns_internal_error_message_when_lookup_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    class FailingLeaderboardService:
        def get_season_leaderboard_page(
            self,
            season_id: int,
            match_format: str,
            page: int,
        ) -> None:
            raise RuntimeError(
                f"boom season_id={season_id} match_format={match_format} page={page}"
            )

    discord_user_id = 123_456_789_012_345_644
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    handlers.leaderboard_service = cast(Any, FailingLeaderboardService())
    guild = FakeGuild(id=14_101_2)
    info_channel = FakeTextChannel(id=13_101_2, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_199_2,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_198_2,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.leaderboard_season(
            as_interaction(interaction),
            1,
            MatchFormat.THREE_VS_THREE.value,
            1,
        )
    )

    assert_response(
        interaction,
        ["シーズン別ランキングの取得に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
    ]


@pytest.mark.parametrize(
    ("command_name", "expected_initial_message"),
    [
        (
            InfoThreadCommandName.LEADERBOARD,
            build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD),
        ),
        (
            InfoThreadCommandName.LEADERBOARD_SEASON,
            build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD_SEASON),
        ),
        (
            InfoThreadCommandName.PLAYER_INFO,
            build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
        ),
        (
            InfoThreadCommandName.PLAYER_INFO_SEASON,
            build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO_SEASON),
        ),
    ],
)
def test_info_thread_command_creates_thread_and_binding_for_registered_player(
    session: Session,
    session_factory: sessionmaker[Session],
    command_name: InfoThreadCommandName,
    expected_initial_message: str,
) -> None:
    discord_user_id = 123_456_789_012_345_686_2
    admin_discord_user_id = 10
    player = create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({admin_discord_user_id}),
    )
    guild = FakeGuild(
        id=14_100,
        members={admin_discord_user_id: FakeMember(id=admin_discord_user_id)},
    )
    info_channel = FakeTextChannel(
        id=13_100,
        name="レート戦情報",
        guild=guild,
    )
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(
            id=discord_user_id,
            name="info-user",
            global_name="info-global",
            nick="info-guild",
        ),
        channel_id=13_199,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.info_thread(as_interaction(interaction), command_name.value))

    session.expire_all()
    binding = session.get(PlayerInfoThreadBinding, player.id)

    assert_response(interaction, ["情報確認用スレッドを作成しました。"], ephemeral=True)
    assert len(info_channel.created_threads) == 1
    created_thread = info_channel.created_threads[0]
    assert created_thread.name == "情報-info-guild"
    assert created_thread.added_user_ids == [discord_user_id, admin_discord_user_id]
    assert [message.content for message in created_thread.sent_messages] == [
        expected_initial_message
    ]
    assert binding is not None
    assert binding.thread_channel_id == created_thread.id


def test_info_thread_command_overwrites_latest_binding_with_new_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_686_3
    player = create_player(session, discord_user_id)
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    guild = FakeGuild(id=14_101, members={10: FakeMember(id=10)})
    info_channel = FakeTextChannel(id=13_101, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id, nick="first-info"),
        channel_id=13_198,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.info_thread(as_interaction(interaction), InfoThreadCommandName.PLAYER_INFO.value)
    )
    asyncio.run(
        handlers.info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.LEADERBOARD.value,
        )
    )

    session.expire_all()
    binding = session.get(PlayerInfoThreadBinding, player.id)
    bindings = session.scalars(select(PlayerInfoThreadBinding)).all()

    assert len(info_channel.created_threads) == 2
    assert binding is not None
    assert binding.thread_channel_id == info_channel.created_threads[-1].id
    assert len(bindings) == 1


def test_info_thread_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_686_4))

    asyncio.run(
        handlers.info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
        )
    )

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


def test_info_thread_command_returns_channel_missing_when_info_channel_is_not_setup(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_686_5
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        guild_id=14_102,
        guild=FakeGuild(id=14_102),
    )

    asyncio.run(
        handlers.info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
        )
    )

    assert_response(
        interaction,
        ["情報確認用チャンネルが見つかりません。管理者に確認してください。"],
        ephemeral=True,
    )


def test_info_thread_command_returns_channel_missing_when_info_channel_cannot_create_threads(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_686_6
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_103)
    unsupported_channel = FakeUnsupportedGuildChannel(
        id=13_103,
        name="レート戦情報",
        guild=guild,
    )
    guild.channels.append(unsupported_channel)
    setup_info_managed_ui_channel(handlers, unsupported_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
        )
    )

    assert_response(
        interaction,
        ["情報確認用チャンネルが見つかりません。管理者に確認してください。"],
        ephemeral=True,
    )


def test_info_thread_command_returns_generic_error_when_thread_creation_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_686_7
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    guild = FakeGuild(id=14_104, members={10: FakeMember(id=10)})
    info_channel = FakeTextChannel(
        id=13_104,
        name="レート戦情報",
        guild=guild,
        fail_create_thread_with=RuntimeError("boom"),
    )
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
        )
    )

    assert_response(
        interaction,
        ["情報確認用スレッドの作成に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )
    assert info_channel.created_threads == []


def test_info_thread_command_cleans_up_created_thread_when_binding_save_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    class FailingInfoThreadBindingService:
        def upsert_latest_thread_channel_id(
            self,
            *,
            player_id: int,
            thread_channel_id: int,
        ) -> None:
            raise RuntimeError("boom")

    discord_user_id = 123_456_789_012_345_686_8
    player = create_player(session, discord_user_id)
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    handlers.info_thread_binding_service = cast(Any, FailingInfoThreadBindingService())
    guild = FakeGuild(id=14_105, members={10: FakeMember(id=10)})
    info_channel = FakeTextChannel(id=13_105, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
        )
    )

    session.expire_all()
    binding = session.get(PlayerInfoThreadBinding, player.id)

    assert_response(
        interaction,
        ["情報確認用スレッドの作成に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )
    assert len(info_channel.created_threads) == 1
    assert info_channel.created_threads[0].deleted is True
    assert binding is None


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


def test_player_info_command_requires_active_info_thread_binding(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_687_1
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(
        interaction,
        ["先に /info_thread を実行してください。"],
        ephemeral=True,
    )


def test_player_info_command_returns_thread_not_found_when_bound_thread_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_687_2
    player = create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_106)
    info_channel = FakeTextChannel(id=13_106, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    handlers.info_thread_binding_service.upsert_latest_thread_channel_id(
        player_id=player.id,
        thread_channel_id=99_001,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_196,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(
        interaction,
        ["情報確認用スレッドが見つかりません。先に /info_thread を実行してください。"],
        ephemeral=True,
    )


def test_player_info_command_returns_thread_not_found_when_bound_thread_send_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_687_3
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_107)
    info_channel = FakeTextChannel(id=13_107, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        interaction_channel_id=13_195,
    )
    created_thread.fail_send_with = make_not_found()
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_194,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(
        interaction,
        ["情報確認用スレッドが見つかりません。先に /info_thread を実行してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
    ]


def test_player_info_command_returns_internal_error_message_when_seasons_are_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_687_4
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    guild = FakeGuild(id=14_108)
    info_channel = FakeTextChannel(id=13_108, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_info_thread(
        handlers,
        discord_user_id=discord_user_id,
        guild=guild,
        info_channel=info_channel,
        interaction_channel_id=13_193,
    )
    session.execute(delete(PlayerFormatStats))
    session.execute(delete(Season))
    session.commit()
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=13_192,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(
        interaction,
        ["プレイヤー情報の取得に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
    ]


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

    assert_response(
        interaction,
        ["観戦応募を受け付けました。現在 1 / 6 人です。"],
        ephemeral=True,
    )
    assert persisted_spectator is not None


def test_match_spectate_command_invites_requesting_player_to_match_operation_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_710,
        channel_id=13_010,
        guild_id=14_010,
    )
    spectator_discord_user_id = 123_456_789_012_345_716
    spectator = create_player(session, spectator_discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=14_010)
    matchmaking_channel = FakeTextChannel(
        id=13_010,
        name="レート戦マッチング",
        guild=guild,
    )
    command_channel = FakeTextChannel(
        id=13_011,
        name="雑談",
        guild=guild,
    )
    guild.channels.extend([matchmaking_channel, command_channel])
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    match_operation_thread = cast(
        FakeThread,
        asyncio.run(matchmaking_channel.create_thread(name=f"試合-{match_id}")),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=spectator_discord_user_id),
        channel_id=command_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.match_spectate(as_interaction(interaction), match_id))

    persisted_spectator = session.scalar(
        select(MatchSpectator).where(
            MatchSpectator.match_id == match_id,
            MatchSpectator.player_id == spectator.id,
        )
    )

    assert_response(
        interaction,
        ["観戦応募を受け付けました。現在 1 / 6 人です。"],
        ephemeral=True,
    )
    assert persisted_spectator is not None
    assert match_operation_thread.added_user_ids == [spectator_discord_user_id]


def test_matchmaking_news_match_announcement_spectate_button_responds_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_710_1,
        channel_id=13_012,
        guild_id=14_012,
    )
    spectator_discord_user_id = 123_456_789_012_345_716_1
    spectator = create_player(session, spectator_discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=14_012)
    matchmaking_channel = FakeTextChannel(
        id=13_012,
        name="レート戦マッチング",
        guild=guild,
    )
    announcement_channel = FakeTextChannel(
        id=13_013,
        name="レート戦マッチ速報",
        guild=guild,
    )
    guild.channels.extend([matchmaking_channel, announcement_channel])
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    match_operation_thread = cast(
        FakeThread,
        asyncio.run(matchmaking_channel.create_thread(name=f"試合-{match_id}")),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=spectator_discord_user_id),
        channel_id=announcement_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.spectate_from_matchmaking_news_match_announcement(
            as_interaction(interaction),
            match_id,
        )
    )

    persisted_spectator = session.scalar(
        select(MatchSpectator).where(
            MatchSpectator.match_id == match_id,
            MatchSpectator.player_id == spectator.id,
        )
    )

    assert_response(
        interaction,
        ["観戦応募を受け付けました。現在 1 / 6 人です。"],
        ephemeral=True,
    )
    assert persisted_spectator is not None
    assert match_operation_thread.added_user_ids == [spectator_discord_user_id]


def test_matchmaking_news_match_announcement_spectate_button_defers_and_replies_via_followup(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_710_2,
        channel_id=13_022,
        guild_id=14_022,
    )
    spectator_discord_user_id = 123_456_789_012_345_716_2
    spectator = create_player(session, spectator_discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(id=14_022)
    matchmaking_channel = FakeTextChannel(
        id=13_022,
        name="レート戦マッチング",
        guild=guild,
    )
    announcement_channel = FakeTextChannel(
        id=13_023,
        name="レート戦マッチ速報",
        guild=guild,
    )
    guild.channels.extend([matchmaking_channel, announcement_channel])
    setup_matchmaking_managed_ui_channel(handlers, matchmaking_channel.id)
    match_operation_thread = cast(
        FakeThread,
        asyncio.run(matchmaking_channel.create_thread(name=f"試合-{match_id}")),
    )
    button = MatchmakingNewsMatchAnnouncementSpectateButton(
        match_id,
        interaction_handler=handlers,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=spectator_discord_user_id),
        channel_id=announcement_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(button.callback(as_interaction(interaction)))

    persisted_spectator = session.scalar(
        select(MatchSpectator).where(
            MatchSpectator.match_id == match_id,
            MatchSpectator.player_id == spectator.id,
        )
    )

    assert_response(
        interaction,
        ["観戦応募を受け付けました。現在 1 / 6 人です。"],
        ephemeral=True,
    )
    assert_deferred_followup_response(interaction)
    assert persisted_spectator is not None
    assert match_operation_thread.added_user_ids == [spectator_discord_user_id]


@pytest.mark.parametrize(
    "handler_name",
    [
        "match_void",
        "void_from_match_operation_thread",
    ],
)
def test_match_void_actions_respond_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
    handler_name: str,
) -> None:
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_717,
        channel_id=13_014,
        guild_id=14_014,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=players[0].discord_user_id),
        channel_id=13_114,
        guild_id=14_014,
    )

    asyncio.run(getattr(handlers, handler_name)(as_interaction(interaction), match_id))

    latest_report = session.scalar(
        select(MatchReport)
        .where(
            MatchReport.match_id == match_id,
            MatchReport.player_id == players[0].id,
            MatchReport.is_latest.is_(True),
        )
        .order_by(MatchReport.id.desc())
    )

    assert latest_report is not None
    assert latest_report.reported_input_result == MatchReportInputResult.VOID
    assert_response(
        interaction,
        ["勝敗報告を受け付けました。"],
        ephemeral=True,
    )


@pytest.mark.parametrize(
    ("handler_name", "reported_input_result"),
    [
        ("match_win", MatchReportInputResult.WIN),
        ("win_from_match_operation_thread", MatchReportInputResult.WIN),
        ("match_draw", MatchReportInputResult.DRAW),
        ("draw_from_match_operation_thread", MatchReportInputResult.DRAW),
        ("match_lose", MatchReportInputResult.LOSE),
        ("lose_from_match_operation_thread", MatchReportInputResult.LOSE),
    ],
)
def test_match_report_actions_respond_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
    handler_name: str,
    reported_input_result: MatchReportInputResult,
) -> None:
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_718,
        channel_id=13_014_1,
        guild_id=14_014_1,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    setup_matchmaking_managed_ui_channel(handlers, 13_014_1)
    match_service = MatchFlowService(session_factory)
    match_service.volunteer_parent(match_id, players[0].id)

    session.expire_all()
    active_state = session.scalar(
        select(ActiveMatchState).where(ActiveMatchState.match_id == match_id)
    )
    assert active_state is not None
    now = datetime.now(timezone.utc)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()
    assert match_service.process_report_open(match_id) is True

    interaction = FakeInteraction(
        user=FakeUser(id=players[0].discord_user_id),
        channel_id=13_114_1,
        guild_id=14_014_1,
    )

    asyncio.run(
        getattr(handlers, handler_name)(
            as_interaction(interaction),
            match_id,
        )
    )

    latest_report = session.scalar(
        select(MatchReport)
        .where(
            MatchReport.match_id == match_id,
            MatchReport.player_id == players[0].id,
            MatchReport.is_latest.is_(True),
        )
        .order_by(MatchReport.id.desc())
    )

    assert latest_report is not None
    assert latest_report.reported_input_result == reported_input_result
    assert_response(
        interaction,
        ["勝敗報告を受け付けました。"],
        ephemeral=True,
    )


@pytest.mark.parametrize(
    "handler_name",
    [
        "match_parent",
        "parent_from_match_operation_thread",
    ],
)
def test_match_parent_actions_respond_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
    handler_name: str,
) -> None:
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_723,
        channel_id=13_015,
        guild_id=14_015,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    setup_matchmaking_managed_ui_channel(handlers, 13_015)
    interaction = FakeInteraction(
        user=FakeUser(id=players[0].discord_user_id),
        channel_id=13_115,
        guild_id=14_015,
    )

    asyncio.run(getattr(handlers, handler_name)(as_interaction(interaction), match_id))

    active_state = session.scalar(
        select(ActiveMatchState).where(ActiveMatchState.match_id == match_id)
    )

    assert active_state is not None
    assert active_state.parent_player_id == players[0].id
    assert active_state.parent_decided_at is not None
    assert_response(
        interaction,
        ["親に立候補しました。"],
        ephemeral=True,
    )


def test_match_operation_thread_parent_button_defers_and_replies_via_followup(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_723_1,
        channel_id=13_025,
        guild_id=14_025,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    setup_matchmaking_managed_ui_channel(handlers, 13_025)
    button = MatchOperationThreadParentButton(match_id, interaction_handler=handlers)
    interaction = FakeInteraction(
        user=FakeUser(id=players[0].discord_user_id),
        channel_id=13_125,
        guild_id=14_025,
    )

    asyncio.run(button.callback(as_interaction(interaction)))

    active_state = session.scalar(
        select(ActiveMatchState).where(ActiveMatchState.match_id == match_id)
    )

    assert active_state is not None
    assert active_state.parent_player_id == players[0].id
    assert active_state.parent_decided_at is not None
    assert_response(
        interaction,
        ["親に立候補しました。"],
        ephemeral=True,
    )
    assert_deferred_followup_response(interaction)


@pytest.mark.parametrize(
    "handler_name",
    [
        "match_approve",
        "approve_from_match_operation_thread",
    ],
)
def test_match_approve_actions_respond_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
    handler_name: str,
) -> None:
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_724,
        channel_id=13_016,
        guild_id=14_016,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    setup_matchmaking_managed_ui_channel(handlers, 13_016)
    match_service = MatchFlowService(session_factory)
    match_service.volunteer_parent(match_id, players[0].id)

    session.expire_all()
    active_state = session.scalar(
        select(ActiveMatchState).where(ActiveMatchState.match_id == match_id)
    )
    assert active_state is not None
    now = datetime.now(timezone.utc)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()
    assert match_service.process_report_open(match_id) is True

    participants = session.scalars(
        select(MatchParticipant).where(MatchParticipant.match_id == match_id)
    ).all()
    participant_by_player_id = {participant.player_id: participant for participant in participants}
    dissenting_player = next(
        player
        for player in players
        if participant_by_player_id[player.id].team == MatchParticipantTeam.TEAM_B
    )

    for player in players:
        participant = participant_by_player_id[player.id]
        if participant.team == MatchParticipantTeam.TEAM_A:
            input_result = MatchReportInputResult.WIN
        elif player.id == dissenting_player.id:
            input_result = MatchReportInputResult.DRAW
        else:
            input_result = MatchReportInputResult.LOSE
        match_service.submit_report(match_id, player.id, input_result)

    interaction = FakeInteraction(
        user=FakeUser(id=dissenting_player.discord_user_id),
        channel_id=13_116,
        guild_id=14_016,
    )

    asyncio.run(getattr(handlers, handler_name)(as_interaction(interaction), match_id))

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    assert active_state.state == MatchState.FINALIZED
    assert_response(
        interaction,
        ["仮決定結果を承認しました。"],
        ephemeral=True,
    )


@pytest.mark.parametrize(
    "handler_name",
    [
        "match_approve",
        "approve_from_match_operation_thread",
    ],
)
def test_match_approve_actions_return_business_error_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
    handler_name: str,
) -> None:
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_725,
        channel_id=13_017,
        guild_id=14_017,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=players[0].discord_user_id),
        channel_id=13_117,
        guild_id=14_017,
    )

    asyncio.run(getattr(handlers, handler_name)(as_interaction(interaction), match_id))

    assert_response(
        interaction,
        ["この試合は承認期間中ではありません。"],
        ephemeral=True,
    )


def test_match_spectate_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_707))

    asyncio.run(handlers.match_spectate(as_interaction(interaction), 1))

    assert_response(
        interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )


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

    assert_response(interaction, ["現在観戦を制限されています。"], ephemeral=True)


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
    guild = FakeGuild(id=12_003)
    matchmaking_channel = FakeTextChannel(
        id=11_003,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(
        handlers,
        matchmaking_channel.id,
        created_by_discord_user_id=executor_discord_user_id,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

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

    assert_response(
        interaction,
        ["指定したユーザーの観戦応募を受け付けました。"],
        ephemeral=True,
    )
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
    guild = FakeGuild(id=12_003)
    matchmaking_channel = FakeTextChannel(
        id=11_003,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(
        handlers,
        matchmaking_channel.id,
        created_by_discord_user_id=executor_discord_user_id,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_match_spectate(
            as_interaction(interaction),
            match_id,
            str(target_discord_user_id),
        )
    )

    assert_response(
        interaction,
        ["指定したユーザーは現在観戦を制限されています。"],
        ephemeral=True,
    )


def test_dev_match_parent_responds_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=target_discord_user_id,
        channel_id=13_018,
        guild_id=14_018,
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    setup_matchmaking_managed_ui_channel(handlers, 13_018)
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_118,
        guild_id=14_018,
    )

    asyncio.run(
        handlers.dev_match_parent(
            as_interaction(interaction),
            match_id,
            str(target_discord_user_id),
        )
    )

    active_state = session.scalar(
        select(ActiveMatchState).where(ActiveMatchState.match_id == match_id)
    )

    assert active_state is not None
    assert active_state.parent_player_id == players[0].id
    assert active_state.parent_decided_at is not None
    assert_response(
        interaction,
        ["指定したユーザーを親に立候補させました。"],
        ephemeral=True,
    )


@pytest.mark.parametrize(
    ("handler_name", "reported_input_result"),
    [
        ("dev_match_win", MatchReportInputResult.WIN),
        ("dev_match_lose", MatchReportInputResult.LOSE),
        ("dev_match_draw", MatchReportInputResult.DRAW),
        ("dev_match_void", MatchReportInputResult.VOID),
    ],
)
def test_dev_match_report_actions_respond_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
    handler_name: str,
    reported_input_result: MatchReportInputResult,
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=target_discord_user_id,
        channel_id=13_019,
        guild_id=14_019,
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    setup_matchmaking_managed_ui_channel(handlers, 13_019)
    match_service = MatchFlowService(session_factory)
    match_service.volunteer_parent(match_id, players[0].id)

    session.expire_all()
    active_state = session.scalar(
        select(ActiveMatchState).where(ActiveMatchState.match_id == match_id)
    )
    assert active_state is not None
    now = datetime.now(timezone.utc)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()
    assert match_service.process_report_open(match_id) is True

    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_119,
        guild_id=14_019,
    )

    asyncio.run(
        getattr(handlers, handler_name)(
            as_interaction(interaction),
            match_id,
            str(target_discord_user_id),
        )
    )

    latest_report = session.scalar(
        select(MatchReport)
        .where(
            MatchReport.match_id == match_id,
            MatchReport.player_id == players[0].id,
            MatchReport.is_latest.is_(True),
        )
        .order_by(MatchReport.id.desc())
    )

    assert latest_report is not None
    assert latest_report.reported_input_result == reported_input_result
    assert_response(
        interaction,
        ["指定したユーザーの勝敗報告を受け付けました。"],
        ephemeral=True,
    )


def test_dev_match_approve_responds_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=777,
        channel_id=13_020,
        guild_id=14_020,
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    setup_matchmaking_managed_ui_channel(handlers, 13_020)
    match_service = MatchFlowService(session_factory)
    match_service.volunteer_parent(match_id, players[0].id)

    session.expire_all()
    active_state = session.scalar(
        select(ActiveMatchState).where(ActiveMatchState.match_id == match_id)
    )
    assert active_state is not None
    now = datetime.now(timezone.utc)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()
    assert match_service.process_report_open(match_id) is True

    participants = session.scalars(
        select(MatchParticipant).where(MatchParticipant.match_id == match_id)
    ).all()
    participant_by_player_id = {participant.player_id: participant for participant in participants}
    dissenting_player = next(
        player
        for player in players
        if participant_by_player_id[player.id].team == MatchParticipantTeam.TEAM_B
    )

    for player in players:
        participant = participant_by_player_id[player.id]
        if participant.team == MatchParticipantTeam.TEAM_A:
            input_result = MatchReportInputResult.WIN
        elif player.id == dissenting_player.id:
            input_result = MatchReportInputResult.DRAW
        else:
            input_result = MatchReportInputResult.LOSE
        match_service.submit_report(match_id, player.id, input_result)

    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_120,
        guild_id=14_020,
    )

    asyncio.run(
        handlers.dev_match_approve(
            as_interaction(interaction),
            match_id,
            str(dissenting_player.discord_user_id),
        )
    )

    session.expire_all()
    active_state = session.get(ActiveMatchState, match_id)
    assert active_state is not None
    assert active_state.state == MatchState.FINALIZED
    assert_response(
        interaction,
        ["指定したユーザーが仮決定結果を承認しました。"],
        ephemeral=True,
    )


def test_dev_match_approve_validates_dummy_user_id_ephemerally(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_match_approve(as_interaction(interaction), 1, "not-a-number"))

    assert_response(interaction, ["discord_user_id が不正です。"], ephemeral=True)


def test_dev_register_requires_admin(
    session: Session,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(
        logging.WARNING,
        logger="dxd_rating.platform.discord.gateway.commands.application",
    ):
        handlers = create_handlers(session_factory)
        interaction = FakeInteraction(user=FakeUser(id=10))

        asyncio.run(handlers.dev_register(as_interaction(interaction), "123456789012345685"))

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == 123_456_789_012_345_685)
    )

    assert_response(
        interaction,
        ["このコマンドは管理者のみ実行できます。"],
        ephemeral=True,
    )
    assert (
        "Rejected admin-only command executor_discord_user_id=10 guild_id=2001 channel_id=1001"
    ) in caplog.text
    assert persisted_player is None


def test_dev_register_sets_fixed_dummy_display_name(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "777"))

    persisted_player = session.scalar(select(Player).where(Player.discord_user_id == 777))

    assert_response(interaction, ["ダミーユーザーを登録しました。"], ephemeral=True)
    assert persisted_player is not None
    assert persisted_player.display_name == "<dummy_777>"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_dev_register_validates_discord_user_id(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "not-a-number"))

    assert_response(interaction, ["discord_user_id が不正です。"], ephemeral=True)


def test_dev_register_rejects_non_dummy_discord_user_id(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "1001"))

    assert_response(interaction, ["discord_user_id が不正です。"], ephemeral=True)


def test_dev_register_returns_internal_error_message_ephemerally(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    session.execute(delete(Season))
    session.commit()
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "777"))

    assert_response(
        interaction,
        ["ダミーユーザーの登録に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


def test_dev_join_requires_admin(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    create_player(session, 123_456_789_012_345_685)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(
        handlers.dev_join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
            "123456789012345685",
        )
    )

    assert_response(
        interaction,
        ["このコマンドは管理者のみ実行できます。"],
        ephemeral=True,
    )


def test_dev_join_creates_presence_thread_for_dummy_user_under_matchmaking_channel(
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
    guild = FakeGuild(
        id=12_001,
        members={executor_discord_user_id: FakeMember(id=executor_discord_user_id)},
    )
    matchmaking_channel = FakeTextChannel(
        id=11_001,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(
        handlers,
        matchmaking_channel.id,
        created_by_discord_user_id=executor_discord_user_id,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=99_001,
        guild_id=guild.id,
        guild=guild,
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

    assert_response(interaction, ["指定したユーザーをキューに参加させました。"], ephemeral=True)
    assert queue_entry.notification_channel_id == matchmaking_channel.id
    assert queue_entry.presence_thread_channel_id == 20_001
    assert queue_entry.notification_guild_id == guild.id
    assert queue_entry.notification_dm_discord_user_id is None
    assert queue_entry.notification_interaction_application_id is None
    assert queue_entry.notification_interaction_token is None
    assert queue_entry.notification_mention_discord_user_id == target_discord_user_id
    assert len(matchmaking_channel.created_threads) == 1
    assert matchmaking_channel.created_threads[0].name == "在席確認-<dummy_777>"
    assert matchmaking_channel.created_threads[0].added_user_ids == [executor_discord_user_id]


def test_dev_join_routes_match_created_to_presence_threads_without_parent_fallback(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    existing_players = create_players(session, 5, start_discord_user_id=880)
    target_player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    guild = FakeGuild(
        id=12_011,
        members={executor_discord_user_id: FakeMember(id=executor_discord_user_id)},
    )
    matchmaking_channel = FakeTextChannel(
        id=11_011,
        name="レート戦マッチング",
        guild=guild,
    )
    matchmaking_news_channel = FakeTextChannel(
        id=11_012,
        name="レート戦マッチ速報",
        guild=guild,
    )
    guild.channels.extend([matchmaking_channel, matchmaking_news_channel])
    setup_matchmaking_managed_ui_channel(
        handlers,
        matchmaking_channel.id,
        created_by_discord_user_id=executor_discord_user_id,
    )
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.MATCHMAKING_NEWS_CHANNEL,
        channel_id=matchmaking_news_channel.id,
        message_id=71_012,
        created_by_discord_user_id=executor_discord_user_id,
    )
    seed_waiting_entries_with_presence_threads(
        session_factory,
        existing_players,
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        start_presence_thread_channel_id=21_100,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=99_011,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
            str(target_discord_user_id),
        )
    )

    queue_entry = get_queue_entry(session, target_player.id)
    match_created_events = [
        event
        for event in get_outbox_events(session)
        if event.event_type == OutboxEventType.MATCH_CREATED
    ]
    destination_channel_ids = {
        event.payload["destination"]["channel_id"] for event in match_created_events
    }
    participant_events = [
        event
        for event in match_created_events
        if event.payload["destination"]["channel_id"] != matchmaking_news_channel.id
    ]

    assert queue_entry.status == MatchQueueEntryStatus.MATCHED
    assert len(match_created_events) == 7
    assert matchmaking_channel.id not in destination_channel_ids
    assert matchmaking_news_channel.id in destination_channel_ids
    assert queue_entry.presence_thread_channel_id == 20_001
    assert queue_entry.presence_thread_channel_id in destination_channel_ids
    assert len(participant_events) == 6
    assert {event.payload["destination"]["channel_id"] for event in participant_events} == {
        20_001,
        21_100,
        21_101,
        21_102,
        21_103,
        21_104,
    }
    assert all("mention_discord_user_id" in event.payload for event in participant_events)


def test_dev_join_returns_internal_error_when_setup_matchmaking_channel_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 778
    create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
        channel_id=11_099,
        message_id=71_099,
        created_by_discord_user_id=executor_discord_user_id,
    )
    guild = FakeGuild(id=12_002)
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=99_002,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
            str(target_discord_user_id),
        )
    )

    assert_response(
        interaction,
        ["指定したユーザーのキュー参加に失敗しました。管理者に確認してください。"],
        ephemeral=True,
    )


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
    guild = FakeGuild(id=12_003)
    matchmaking_channel = FakeTextChannel(
        id=11_003,
        name="レート戦マッチング",
        guild=guild,
    )
    guild.channels.append(matchmaking_channel)
    setup_matchmaking_managed_ui_channel(
        handlers,
        matchmaking_channel.id,
        created_by_discord_user_id=executor_discord_user_id,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=matchmaking_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
            str(target_discord_user_id),
        )
    )

    assert_response(
        interaction,
        ["指定したユーザーは現在キュー参加を制限されています。"],
        ephemeral=True,
    )


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

    assert_response(
        interaction,
        ["指定したユーザーは期限切れのためキューから外れました。"],
        ephemeral=True,
    )


def test_dev_present_preserves_existing_presence_thread_destination(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_688
    player = create_player(session, target_discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(
        player.id,
        DEFAULT_MATCH_FORMAT,
        DEFAULT_QUEUE_NAME,
        notification_context=MatchingQueueNotificationContext(
            channel_id=41_001,
            guild_id=42_001,
            mention_discord_user_id=target_discord_user_id,
        ),
    )
    queue_entry = get_queue_entry(session, player.id)
    matching_queue_service.update_waiting_presence_thread_channel_id(queue_entry.id, 43_001)

    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=matching_queue_service,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=99_001,
        guild_id=99_002,
    )

    asyncio.run(handlers.dev_present(as_interaction(interaction), str(target_discord_user_id)))

    queue_entry = get_queue_entry(session, player.id)

    assert_response(interaction, ["指定したユーザーの在席を更新しました。"], ephemeral=True)
    assert queue_entry.notification_channel_id == 41_001
    assert queue_entry.notification_guild_id == 42_001
    assert queue_entry.presence_thread_channel_id == 43_001


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

    assert_response(interaction, ["指定したユーザーは未登録です。"], ephemeral=True)


def test_dev_player_info_requires_admin(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    create_player(session, 123_456_789_012_345_689)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "123456789012345689"))

    assert_response(
        interaction,
        ["このコマンドは管理者のみ実行できます。"],
        ephemeral=True,
    )


def test_dev_info_thread_requires_admin(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    target_discord_user_id = 123_456_789_012_345_689_1
    create_player(session, target_discord_user_id)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(
        handlers.dev_info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
            str(target_discord_user_id),
        )
    )

    assert_response(
        interaction,
        ["このコマンドは管理者のみ実行できます。"],
        ephemeral=True,
    )


def test_dev_info_thread_validates_discord_user_id(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(
        handlers.dev_info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
            "not-a-number",
        )
    )

    assert_response(interaction, ["discord_user_id が不正です。"], ephemeral=True)


def test_dev_info_thread_creates_thread_and_binding_for_real_user(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_689_2
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    guild = FakeGuild(
        id=14_200,
        members={
            target_discord_user_id: FakeMember(
                id=target_discord_user_id,
                nick="target-guild",
            )
        },
    )
    info_channel = FakeTextChannel(id=13_200, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id, nick="admin-guild"),
        channel_id=13_299,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.LEADERBOARD.value,
            str(target_discord_user_id),
        )
    )

    session.expire_all()
    binding = session.get(PlayerInfoThreadBinding, player.id)

    assert_response(
        interaction,
        ["指定したユーザーの情報確認用スレッドを作成しました。"],
        ephemeral=True,
    )
    assert len(info_channel.created_threads) == 1
    created_thread = info_channel.created_threads[0]
    assert created_thread.name == "情報-target-guild"
    assert created_thread.added_user_ids == [target_discord_user_id, executor_discord_user_id]
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD)
    ]
    assert binding is not None
    assert binding.thread_channel_id == created_thread.id


def test_dev_info_thread_creates_thread_and_binding_for_dummy_user(
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
    guild = FakeGuild(id=14_201)
    info_channel = FakeTextChannel(id=13_201, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_300,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
            str(target_discord_user_id),
        )
    )

    session.expire_all()
    binding = session.get(PlayerInfoThreadBinding, player.id)

    assert_response(
        interaction,
        ["指定したユーザーの情報確認用スレッドを作成しました。"],
        ephemeral=True,
    )
    assert len(info_channel.created_threads) == 1
    created_thread = info_channel.created_threads[0]
    assert created_thread.name == "情報-<dummy_777>"
    assert created_thread.added_user_ids == [executor_discord_user_id]
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO)
    ]
    assert binding is not None
    assert binding.thread_channel_id == created_thread.id


def test_dev_info_thread_overwrites_latest_binding_with_new_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_689_3
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    guild = FakeGuild(id=14_202)
    info_channel = FakeTextChannel(id=13_202, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_301,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
            str(target_discord_user_id),
        )
    )
    asyncio.run(
        handlers.dev_info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.LEADERBOARD.value,
            str(target_discord_user_id),
        )
    )

    session.expire_all()
    binding = session.get(PlayerInfoThreadBinding, player.id)

    assert len(info_channel.created_threads) == 2
    assert binding is not None
    assert binding.thread_channel_id == info_channel.created_threads[-1].id


def test_dev_info_thread_returns_target_not_registered_message(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(
        handlers.dev_info_thread(
            as_interaction(interaction),
            InfoThreadCommandName.PLAYER_INFO.value,
            "123456789012345699",
        )
    )

    assert_response(interaction, ["指定したユーザーは未登録です。"], ephemeral=True)


def test_dev_player_info_validates_discord_user_id(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "not-a-number"))

    assert_response(interaction, ["discord_user_id が不正です。"], ephemeral=True)


def test_dev_player_info_season_validates_discord_user_id(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(
        handlers.dev_player_info_season(
            as_interaction(interaction),
            1,
            "not-a-number",
        )
    )

    assert_response(interaction, ["discord_user_id が不正です。"], ephemeral=True)


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
    guild = FakeGuild(id=14_203)
    info_channel = FakeTextChannel(id=13_203, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_dev_info_thread(
        handlers,
        executor_discord_user_id=executor_discord_user_id,
        target_discord_user_id=target_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO,
        interaction_channel_id=13_302,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_303,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), str(target_discord_user_id)))

    assert_response(
        interaction,
        ["指定したユーザーのプレイヤー情報を表示しました。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO),
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
        ),
    ]
    assert created_thread.sent_messages[1].view is None


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
    guild = FakeGuild(id=14_204)
    info_channel = FakeTextChannel(id=13_204, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_dev_info_thread(
        handlers,
        executor_discord_user_id=executor_discord_user_id,
        target_discord_user_id=target_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO_SEASON,
        interaction_channel_id=13_304,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_305,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_player_info_season(
            as_interaction(interaction),
            season_pair.upcoming.id,
            str(target_discord_user_id),
        )
    )

    assert_response(
        interaction,
        ["指定したユーザーのシーズン別プレイヤー情報を表示しました。"],
        ephemeral=True,
    )
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.PLAYER_INFO_SEASON),
        format_player_info_message(
            {
                MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.TWO_VS_TWO: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.THREE_VS_THREE: (1488.0, 0, 0, 0, 0, None),
            },
            season_id=season_pair.upcoming.id,
            season_name="next-summer",
        ),
    ]
    assert created_thread.sent_messages[1].view is None


def test_dev_player_info_requires_info_thread_binding(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690_2
    create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), str(target_discord_user_id)))

    assert_response(
        interaction,
        ["先に /info_thread または /dev_info_thread を実行してください。"],
        ephemeral=True,
    )


def test_dev_player_info_returns_thread_not_found_when_bound_thread_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690_3
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    guild = FakeGuild(id=14_205)
    info_channel = FakeTextChannel(id=13_205, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    handlers.info_thread_binding_service.upsert_latest_thread_channel_id(
        player_id=player.id,
        thread_channel_id=99_005,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_306,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), str(target_discord_user_id)))

    assert_response(
        interaction,
        [
            "情報確認用スレッドが見つかりません。"
            "先に /info_thread または /dev_info_thread を実行してください。"
        ],
        ephemeral=True,
    )


def test_dev_player_info_returns_target_not_registered_message(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "123456789012345691"))

    assert_response(interaction, ["指定したユーザーは未登録です。"], ephemeral=True)


def test_dev_info_thread_binding_is_shared_with_normal_player_info(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690_4
    player = create_player(session, target_discord_user_id)
    three_vs_three_stats = get_player_format_stats(session, player.id)
    three_vs_three_stats.rating = 1510.0
    session.commit()
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    guild = FakeGuild(
        id=14_206,
        members={target_discord_user_id: FakeMember(id=target_discord_user_id, nick="shared-user")},
    )
    info_channel = FakeTextChannel(id=13_206, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_dev_info_thread(
        handlers,
        executor_discord_user_id=executor_discord_user_id,
        target_discord_user_id=target_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.PLAYER_INFO,
        interaction_channel_id=13_307,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=target_discord_user_id, nick="shared-user"),
        channel_id=13_308,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert_response(interaction, ["プレイヤー情報を表示しました。"], ephemeral=True)
    assert len(created_thread.sent_messages) == 2


def test_dev_leaderboard_posts_current_leaderboard_to_target_info_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690_5
    bob_discord_user_id = 123_456_789_012_345_690_6
    carol_discord_user_id = 123_456_789_012_345_690_7
    target = create_player(session, target_discord_user_id)
    bob = create_player(session, bob_discord_user_id)
    carol = create_player(session, carol_discord_user_id)
    season_pair = ensure_active_and_upcoming_seasons(session)
    target_stats = get_player_format_stats(session, target.id)
    bob_stats = get_player_format_stats(session, bob.id)
    carol_stats = get_player_format_stats(session, carol.id)
    target_stats.rating = 1600
    target_stats.games_played = 2
    target_stats.wins = 2
    bob_stats.rating = 1600
    bob_stats.games_played = 5
    bob_stats.wins = 4
    bob_stats.losses = 1
    carol_stats.rating = 1600
    carol_stats.games_played = 5
    carol_stats.wins = 3
    carol_stats.losses = 2
    target.display_name = "Target"
    bob.display_name = "Bob"
    carol.display_name = "Carol"
    session.commit()
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    guild = FakeGuild(id=14_207)
    info_channel = FakeTextChannel(id=13_207, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_dev_info_thread(
        handlers,
        executor_discord_user_id=executor_discord_user_id,
        target_discord_user_id=target_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD,
        interaction_channel_id=13_309,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_310,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_leaderboard(
            as_interaction(interaction),
            MatchFormat.THREE_VS_THREE.value,
            1,
            str(target_discord_user_id),
        )
    )

    assert_response(
        interaction,
        ["指定したユーザーの情報確認用スレッドにランキングを表示しました。"],
        ephemeral=True,
    )
    assert created_thread.sent_messages[1].content == format_leaderboard_message(
        season_name=season_pair.active.name,
        match_format=MatchFormat.THREE_VS_THREE,
        page=1,
        entries=[
            (1, "Bob", 1600.0, None, None, None),
            (2, "Carol", 1600.0, None, None, None),
            (3, "Target", 1600.0, None, None, None),
        ],
    )
    assert created_thread.sent_messages[1].view is None


def test_dev_leaderboard_requires_info_thread_binding(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690_8
    create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.dev_leaderboard(
            as_interaction(interaction),
            MatchFormat.THREE_VS_THREE.value,
            1,
            str(target_discord_user_id),
        )
    )

    assert_response(
        interaction,
        ["先に /info_thread または /dev_info_thread を実行してください。"],
        ephemeral=True,
    )


def test_dev_leaderboard_season_posts_requested_season_leaderboard_to_target_info_thread(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690_9
    target = create_player(session, target_discord_user_id)
    current_time = get_database_now(session)
    season = Season(
        name="202602delta",
        start_at=current_time - timedelta(days=40),
        end_at=current_time - timedelta(days=10),
        completed=True,
        completed_at=current_time - timedelta(days=10),
    )
    session.add(season)
    session.flush()
    session.add(
        PlayerFormatStats(
            player_id=target.id,
            season_id=season.id,
            match_format=MatchFormat.THREE_VS_THREE,
            rating=1600,
            games_played=1,
            wins=1,
        )
    )
    session.commit()
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    guild = FakeGuild(id=14_208)
    info_channel = FakeTextChannel(id=13_208, name="レート戦情報", guild=guild)
    guild.channels.append(info_channel)
    setup_info_managed_ui_channel(handlers, info_channel.id)
    created_thread = create_active_dev_info_thread(
        handlers,
        executor_discord_user_id=executor_discord_user_id,
        target_discord_user_id=target_discord_user_id,
        guild=guild,
        info_channel=info_channel,
        command_name=InfoThreadCommandName.LEADERBOARD_SEASON,
        interaction_channel_id=13_311,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_312,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.dev_leaderboard_season(
            as_interaction(interaction),
            season.id,
            MatchFormat.THREE_VS_THREE.value,
            1,
            str(target_discord_user_id),
        )
    )

    assert_response(
        interaction,
        ["指定したユーザーの情報確認用スレッドにシーズン別ランキングを表示しました。"],
        ephemeral=True,
    )
    assert created_thread.sent_messages[1].content == format_leaderboard_season_message(
        season_id=season.id,
        season_name=season.name,
        match_format=MatchFormat.THREE_VS_THREE,
        page=1,
        entries=[
            (1, "1234567890123456909", 1600.0),
        ],
    )
    assert created_thread.sent_messages[1].view is None


def test_dev_is_admin_returns_yes_or_no(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    admin_interaction = FakeInteraction(user=FakeUser(id=10))
    non_admin_interaction = FakeInteraction(user=FakeUser(id=20))

    asyncio.run(handlers.dev_is_admin(as_interaction(admin_interaction)))
    asyncio.run(handlers.dev_is_admin(as_interaction(non_admin_interaction)))

    assert_response(admin_interaction, ["はい"], ephemeral=True)
    assert_response(non_admin_interaction, ["いいえ"], ephemeral=True)


def test_admin_match_result_responds_ephemerally_and_posts_public_followup(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    match_id, players = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_694,
        channel_id=13_021,
        guild_id=14_021,
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    setup_matchmaking_managed_ui_channel(handlers, 13_021)
    match_service = MatchFlowService(session_factory)
    match_service.volunteer_parent(match_id, players[0].id)

    session.expire_all()
    active_state = session.scalar(
        select(ActiveMatchState).where(ActiveMatchState.match_id == match_id)
    )
    assert active_state is not None
    now = datetime.now(timezone.utc)
    active_state.report_open_at = now - timedelta(minutes=1)
    active_state.report_deadline_at = now + timedelta(minutes=10)
    session.commit()
    assert match_service.process_report_open(match_id) is True

    participants = session.scalars(
        select(MatchParticipant).where(MatchParticipant.match_id == match_id)
    ).all()
    participant_by_player_id = {participant.player_id: participant for participant in participants}
    dissenting_player = next(
        player
        for player in players
        if participant_by_player_id[player.id].team == MatchParticipantTeam.TEAM_B
    )

    for player in players:
        participant = participant_by_player_id[player.id]
        if participant.team == MatchParticipantTeam.TEAM_A:
            input_result = MatchReportInputResult.WIN
        elif player.id == dissenting_player.id:
            input_result = MatchReportInputResult.DRAW
        else:
            input_result = MatchReportInputResult.LOSE
        match_service.submit_report(match_id, player.id, input_result)

    match_service.approve_provisional_result(match_id, dissenting_player.id)

    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=13_121,
        guild_id=14_021,
    )

    asyncio.run(
        handlers.admin_match_result(
            as_interaction(interaction),
            match_id,
            MatchResult.DRAW.value,
        )
    )

    session.expire_all()
    finalized_result = session.get(FinalizedMatchResult, match_id)

    assert finalized_result is not None
    assert finalized_result.final_result == MatchResult.DRAW
    assert_response_sequence(
        interaction,
        [
            "試合結果を上書きしました。",
            f"match_id: {match_id} の試合結果が管理者操作により「引き分け」に上書きされました。",
        ],
        [True, False],
    )


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

    assert_response(interaction, ["シーズン名を変更しました。"], ephemeral=True)
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


def test_admin_setup_custom_ui_channel_creates_info_channel_buttons(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    registered_role = FakeRole(id=55_010, name=REGISTERED_PLAYER_ROLE_NAME)
    guild = FakeGuild(id=2_108, roles=[registered_role])
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
            ManagedUiType.INFO_CHANNEL.value,
            "レート戦情報",
        )
    )

    session.expire_all()
    managed_ui_channel = session.scalar(select(ManagedUiChannel))
    persisted_channel = guild.channels[0]
    persisted_message = persisted_channel.sent_messages[0]

    assert_response(interaction, ["UI 設置チャンネルを作成しました。"], ephemeral=True)
    assert managed_ui_channel is not None
    assert managed_ui_channel.ui_type == ManagedUiType.INFO_CHANNEL
    assert managed_ui_channel.channel_id == persisted_channel.id
    assert managed_ui_channel.message_id == persisted_message.id
    assert managed_ui_channel.created_by_discord_user_id == executor_discord_user_id
    assert persisted_channel.overwrites[guild.default_role].view_channel is False
    assert persisted_channel.overwrites[guild.default_role].send_messages is False
    assert persisted_channel.overwrites[registered_role].view_channel is True
    assert persisted_channel.overwrites[registered_role].send_messages is False
    assert persisted_message.content == INFO_CHANNEL_MESSAGE
    assert persisted_message.view is not None
    info_buttons = [
        cast(discord.ui.Button[Any], child) for child in persisted_message.view.children
    ]
    assert [(button.label, button.custom_id) for button in info_buttons] == [
        (
            INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
            INFO_CHANNEL_LEADERBOARD_BUTTON_CUSTOM_ID,
        ),
        (
            INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
            INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_CUSTOM_ID,
        ),
        (
            INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
            INFO_CHANNEL_PLAYER_INFO_BUTTON_CUSTOM_ID,
        ),
        (
            INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
            INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_CUSTOM_ID,
        ),
    ]
    assert all(button.style is discord.ButtonStyle.primary for button in info_buttons)


def test_admin_setup_custom_ui_channel_creates_matchmaking_channel_with_placeholder_status_message(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    matchmaking_guide_url = "https://example.com/guide"
    registered_role = FakeRole(id=55_010_5, name=REGISTERED_PLAYER_ROLE_NAME)
    guild = FakeGuild(id=2_108_05, roles=[registered_role])
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matchmaking_guide_url=matchmaking_guide_url,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(interaction),
            ManagedUiType.MATCHMAKING_CHANNEL.value,
            "レート戦マッチング",
        )
    )

    session.expire_all()
    managed_ui_channel = session.scalar(select(ManagedUiChannel))
    persisted_channel = guild.channels[0]

    assert_response(interaction, ["UI 設置チャンネルを作成しました。"], ephemeral=True)
    assert managed_ui_channel is not None
    assert managed_ui_channel.ui_type == ManagedUiType.MATCHMAKING_CHANNEL
    assert managed_ui_channel.channel_id == persisted_channel.id
    assert managed_ui_channel.status_message_id == persisted_channel.sent_messages[1].id
    assert managed_ui_channel.message_id == persisted_channel.sent_messages[2].id
    assert managed_ui_channel.created_by_discord_user_id == executor_discord_user_id
    assert persisted_channel.overwrites[guild.default_role].view_channel is False
    assert persisted_channel.overwrites[guild.default_role].send_messages is False
    assert persisted_channel.overwrites[registered_role].view_channel is True
    assert persisted_channel.overwrites[registered_role].send_messages is False
    assert len(persisted_channel.sent_messages) == 3
    assert persisted_channel.sent_messages[0].content == build_matchmaking_guide_message(
        matchmaking_guide_url
    )
    assert persisted_channel.sent_messages[0].view is None
    assert persisted_channel.sent_messages[0].suppress_embeds is True
    assert (
        persisted_channel.sent_messages[1].content == MATCHMAKING_CHANNEL_STATUS_PLACEHOLDER_MESSAGE
    )
    assert persisted_channel.sent_messages[1].view is not None
    assert persisted_channel.sent_messages[1].suppress_embeds is False
    status_button = cast(
        discord.ui.Button[Any],
        persisted_channel.sent_messages[1].view.children[0],
    )
    assert status_button.label == MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL
    assert status_button.style is discord.ButtonStyle.success
    assert status_button.custom_id == MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_CUSTOM_ID
    assert persisted_channel.sent_messages[2].content == MATCHMAKING_CHANNEL_MESSAGE
    assert persisted_channel.sent_messages[2].view is not None
    assert persisted_channel.sent_messages[2].suppress_embeds is False
    match_format_select = cast(
        discord.ui.Select[Any],
        persisted_channel.sent_messages[2].view.children[0],
    )
    queue_name_select = cast(
        discord.ui.Select[Any],
        persisted_channel.sent_messages[2].view.children[1],
    )
    join_button = cast(
        discord.ui.Button[Any],
        persisted_channel.sent_messages[2].view.children[2],
    )
    assert match_format_select.placeholder == MATCHMAKING_CHANNEL_MATCH_FORMAT_PLACEHOLDER
    assert [option.value for option in match_format_select.options] == ["1v1", "2v2", "3v3"]
    assert queue_name_select.placeholder == MATCHMAKING_CHANNEL_QUEUE_NAME_PLACEHOLDER
    assert [option.value for option in queue_name_select.options] == [
        "beginner",
        "regular",
        "master",
    ]
    assert join_button.label == MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL


def test_admin_setup_custom_ui_channel_creates_admin_operations_channel_for_super_admins(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    second_super_admin_discord_user_id = 20
    second_super_admin = FakeGuildMember(id=second_super_admin_discord_user_id)
    guild = FakeGuild(
        id=2_108_3,
        members={second_super_admin_discord_user_id: second_super_admin},
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset(
            {executor_discord_user_id, second_super_admin_discord_user_id}
        ),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(interaction),
            ManagedUiType.ADMIN_OPERATIONS_CHANNEL.value,
            "運営専用",
        )
    )

    session.expire_all()
    managed_ui_channel = session.scalar(select(ManagedUiChannel))
    persisted_channel = guild.channels[0]
    persisted_message = persisted_channel.sent_messages[0]

    assert_response(interaction, ["UI 設置チャンネルを作成しました。"], ephemeral=True)
    assert managed_ui_channel is not None
    assert managed_ui_channel.ui_type == ManagedUiType.ADMIN_OPERATIONS_CHANNEL
    assert managed_ui_channel.channel_id == persisted_channel.id
    assert managed_ui_channel.message_id == persisted_message.id
    assert persisted_channel.overwrites[guild.default_role].view_channel is False
    assert persisted_channel.overwrites[interaction.user].view_channel is True
    assert persisted_channel.overwrites[interaction.user].send_messages is True
    assert persisted_channel.overwrites[second_super_admin].view_channel is True
    assert persisted_channel.overwrites[second_super_admin].send_messages is True
    assert persisted_message.content == ADMIN_OPERATIONS_CHANNEL_MESSAGE


def test_info_channel_button_creates_info_thread_via_existing_command_flow(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_802
    player = create_player(session, target_discord_user_id)
    registered_role = FakeRole(id=55_012, name=REGISTERED_PLAYER_ROLE_NAME)
    guild = FakeGuild(
        id=2_108_1,
        roles=[registered_role],
        members={executor_discord_user_id: FakeMember(id=executor_discord_user_id)},
    )
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
            ManagedUiType.INFO_CHANNEL.value,
            "レート戦情報",
        )
    )

    persisted_channel = guild.channels[0]
    persisted_message = persisted_channel.sent_messages[0]
    leaderboard_button = cast(discord.ui.Button[Any], persisted_message.view.children[0])
    button_interaction = FakeInteraction(
        user=FakeUser(id=target_discord_user_id, nick="info-button"),
        channel_id=persisted_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(leaderboard_button.callback(as_interaction(button_interaction)))

    session.expire_all()
    binding = session.get(PlayerInfoThreadBinding, player.id)

    assert_response(button_interaction, ["情報確認用スレッドを作成しました。"], ephemeral=True)
    assert_deferred_followup_response(button_interaction)
    assert len(persisted_channel.created_threads) == 1
    created_thread = persisted_channel.created_threads[0]
    assert created_thread.added_user_ids == [target_discord_user_id, executor_discord_user_id]
    assert [message.content for message in created_thread.sent_messages] == [
        build_info_thread_initial_message(InfoThreadCommandName.LEADERBOARD)
    ]
    assert binding is not None
    assert binding.thread_channel_id == created_thread.id


def test_info_channel_button_returns_registration_required_for_unregistered_user(
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    registered_role = FakeRole(id=55_013, name=REGISTERED_PLAYER_ROLE_NAME)
    guild = FakeGuild(id=2_108_2, roles=[registered_role])
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
            ManagedUiType.INFO_CHANNEL.value,
            "レート戦情報",
        )
    )

    persisted_channel = guild.channels[0]
    persisted_message = persisted_channel.sent_messages[0]
    player_info_button = cast(discord.ui.Button[Any], persisted_message.view.children[2])
    button_interaction = FakeInteraction(
        user=FakeUser(id=123_456_789_012_345_803),
        channel_id=persisted_channel.id,
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(player_info_button.callback(as_interaction(button_interaction)))

    assert_response(
        button_interaction,
        ["プレイヤー登録が必要です。先に /register を実行してください。"],
        ephemeral=True,
    )
    assert_deferred_followup_response(button_interaction)
    assert persisted_channel.created_threads == []


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


def test_admin_setup_custom_ui_channel_rolls_back_when_second_matchmaking_message_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(
        id=2_101_1,
        next_channel_fail_send_call_errors={2: RuntimeError("boom")},
    )
    registered_role = FakeRole(id=55_010_6, name=REGISTERED_PLAYER_ROLE_NAME)
    guild.roles.append(registered_role)
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

    assert interaction.response.messages == [
        "UI 設置チャンネルの作成に失敗しました。管理者に確認してください。"
    ]
    assert interaction.response.ephemeral_flags == [True]
    assert guild.channels == []
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_setup_custom_ui_channel_rolls_back_when_third_matchmaking_message_fails(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(
        id=2_101_2,
        next_channel_fail_send_call_errors={3: RuntimeError("boom")},
    )
    registered_role = FakeRole(id=55_010_7, name=REGISTERED_PLAYER_ROLE_NAME)
    guild.roles.append(registered_role)
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
        [
            "Bot に必要な権限がありません。 不足している権限: "
            "チャンネルの管理, ロールの管理, プライベートスレッドの作成, "
            "スレッドでメッセージを送信"
        ],
        ephemeral=True,
    )
    assert guild.channels == []
    assert session.scalar(select(ManagedUiChannel)) is None


def test_admin_setup_custom_ui_channel_creates_info_channel_without_thread_permissions(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    registered_role = FakeRole(id=55_011, name=REGISTERED_PLAYER_ROLE_NAME)
    guild = FakeGuild(
        id=2_101_55,
        roles=[registered_role],
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

    asyncio.run(
        handlers.admin_setup_custom_ui_channel(
            as_interaction(interaction),
            ManagedUiType.INFO_CHANNEL.value,
            "レート戦情報",
        )
    )

    session.expire_all()

    assert_response(interaction, ["UI 設置チャンネルを作成しました。"], ephemeral=True)
    assert guild.channels[0].sent_messages[0].content == INFO_CHANNEL_MESSAGE
    assert guild.channels[0].sent_messages[0].view is not None
    assert session.scalar(select(ManagedUiChannel)) is not None


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
    assert matchmaking_channel.overwrites[guild.me].create_private_threads is True
    assert matchmaking_channel.overwrites[guild.me].send_messages_in_threads is True
    assert len(matchmaking_channel.sent_messages) == 3
    assert matchmaking_channel.sent_messages[0].content == build_matchmaking_guide_message(
        DEFAULT_MATCHMAKING_GUIDE_URL
    )
    assert matchmaking_channel.sent_messages[0].view is None
    assert matchmaking_channel.sent_messages[0].suppress_embeds is True
    assert (
        matchmaking_channel.sent_messages[1].content
        == MATCHMAKING_CHANNEL_STATUS_PLACEHOLDER_MESSAGE
    )
    assert matchmaking_channel.sent_messages[1].view is not None
    assert matchmaking_channel.sent_messages[1].suppress_embeds is False
    status_button = cast(
        discord.ui.Button[Any],
        matchmaking_channel.sent_messages[1].view.children[0],
    )
    assert status_button.label == MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL
    assert status_button.style is discord.ButtonStyle.success
    assert status_button.custom_id == MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_CUSTOM_ID
    assert matchmaking_channel.sent_messages[2].content == MATCHMAKING_CHANNEL_MESSAGE
    assert matchmaking_channel.sent_messages[2].view is not None
    assert matchmaking_channel.sent_messages[2].suppress_embeds is False
    matchmaking_channel_record = next(
        managed_ui_channel
        for managed_ui_channel in managed_ui_channels
        if managed_ui_channel.ui_type == ManagedUiType.MATCHMAKING_CHANNEL
    )
    assert matchmaking_channel_record.status_message_id == matchmaking_channel.sent_messages[1].id
    assert matchmaking_channel_record.message_id == matchmaking_channel.sent_messages[2].id
    match_format_select = cast(
        discord.ui.Select[Any],
        matchmaking_channel.sent_messages[2].view.children[0],
    )
    queue_name_select = cast(
        discord.ui.Select[Any],
        matchmaking_channel.sent_messages[2].view.children[1],
    )
    join_button = cast(
        discord.ui.Button[Any],
        matchmaking_channel.sent_messages[2].view.children[2],
    )
    assert match_format_select.placeholder == MATCHMAKING_CHANNEL_MATCH_FORMAT_PLACEHOLDER
    assert [option.value for option in match_format_select.options] == ["1v1", "2v2", "3v3"]
    assert queue_name_select.placeholder == MATCHMAKING_CHANNEL_QUEUE_NAME_PLACEHOLDER
    assert [option.value for option in queue_name_select.options] == [
        "beginner",
        "regular",
        "master",
    ]
    assert join_button.label == MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL

    matchmaking_news_channel = find_channel_by_name(guild, "レート戦マッチ速報")
    assert matchmaking_news_channel.overwrites[guild.default_role].view_channel is False
    assert matchmaking_news_channel.overwrites[registered_role].view_channel is True
    assert matchmaking_news_channel.sent_messages[0].content == MATCHMAKING_NEWS_CHANNEL_MESSAGE
    assert matchmaking_news_channel.sent_messages[0].view is None

    info_channel = find_channel_by_name(guild, "レート戦情報")
    assert info_channel.overwrites[guild.default_role].view_channel is False
    assert info_channel.overwrites[registered_role].view_channel is True
    assert info_channel.overwrites[registered_role].send_messages is False
    assert info_channel.sent_messages[0].content == INFO_CHANNEL_MESSAGE
    assert info_channel.sent_messages[0].view is not None
    info_buttons = [
        cast(discord.ui.Button[Any], child) for child in info_channel.sent_messages[0].view.children
    ]
    assert [(button.label, button.custom_id) for button in info_buttons] == [
        (
            INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
            INFO_CHANNEL_LEADERBOARD_BUTTON_CUSTOM_ID,
        ),
        (
            INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
            INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_CUSTOM_ID,
        ),
        (
            INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
            INFO_CHANNEL_PLAYER_INFO_BUTTON_CUSTOM_ID,
        ),
        (
            INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
            INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_CUSTOM_ID,
        ),
    ]
    assert all(button.style is discord.ButtonStyle.primary for button in info_buttons)

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

    admin_operations_channel = find_channel_by_name(guild, "運営専用")
    assert admin_operations_channel.overwrites[guild.default_role].view_channel is False
    assert admin_operations_channel.overwrites[interaction.user].view_channel is True
    assert admin_operations_channel.overwrites[interaction.user].send_messages is True
    assert admin_operations_channel.sent_messages[0].content == ADMIN_OPERATIONS_CHANNEL_MESSAGE


def test_admin_setup_ui_channels_creates_private_channels_in_development_mode(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    second_super_admin_discord_user_id = 20
    second_super_admin = FakeGuildMember(id=second_super_admin_discord_user_id)
    guild = FakeGuild(
        id=2_102_5_1,
        members={second_super_admin_discord_user_id: second_super_admin},
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset(
            {executor_discord_user_id, second_super_admin_discord_user_id}
        ),
        development_mode=True,
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
    assert len(managed_ui_channels) == len(get_required_managed_ui_definitions())
    assert all(
        channel.overwrites[guild.default_role].view_channel is False for channel in guild.channels
    )

    registered_role = find_role_by_name(guild, REGISTERED_PLAYER_ROLE_NAME)
    assert registered_role is not None

    register_channel = find_channel_by_name(guild, "レート戦はこちらから")
    assert register_channel.overwrites[interaction.user].view_channel is True
    assert register_channel.overwrites[interaction.user].send_messages is False

    matchmaking_channel = find_channel_by_name(guild, "レート戦マッチング")
    assert matchmaking_channel.overwrites[interaction.user].view_channel is True
    assert matchmaking_channel.overwrites[registered_role].view_channel is True
    assert matchmaking_channel.overwrites[guild.me].create_private_threads is True
    assert matchmaking_channel.overwrites[guild.me].send_messages_in_threads is True

    info_channel = find_channel_by_name(guild, "レート戦情報")
    assert info_channel.overwrites[interaction.user].view_channel is True
    assert info_channel.overwrites[interaction.user].send_messages is False
    assert info_channel.overwrites[registered_role].view_channel is True

    admin_contact_channel = find_channel_by_name(guild, "運営連絡・フィードバック")
    assert admin_contact_channel.overwrites[interaction.user].view_channel is True
    assert admin_contact_channel.overwrites[interaction.user].send_messages is True

    admin_operations_channel = find_channel_by_name(guild, "運営専用")
    assert admin_operations_channel.overwrites[interaction.user].view_channel is True
    assert admin_operations_channel.overwrites[interaction.user].send_messages is True
    assert admin_operations_channel.overwrites[second_super_admin].view_channel is True
    assert admin_operations_channel.overwrites[second_super_admin].send_messages is True


def test_admin_setup_ui_channels_reports_missing_manage_roles_permission(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(
        id=2_102_6,
        me=FakeGuildMember(
            id=999_999,
            guild_permissions=discord.Permissions(
                manage_channels=True,
                create_private_threads=True,
                send_messages_in_threads=True,
            ),
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


def test_admin_cleanup_ui_channels_deletes_only_setup_blocking_unmanaged_channels(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    guild = FakeGuild(id=2_102_7)
    duplicate_register_channel = FakeTextChannel(
        id=60_010,
        name="レート戦はこちらから",
        guild=guild,
    )
    blocking_matchmaking_channel = FakeTextChannel(
        id=60_011,
        name="レート戦マッチング",
        guild=guild,
    )
    blocking_news_channel = FakeTextChannel(
        id=60_012,
        name="レート戦マッチ速報",
        guild=guild,
    )
    unrelated_channel = FakeTextChannel(
        id=60_013,
        name="雑談",
        guild=guild,
    )
    guild.channels.extend(
        [
            duplicate_register_channel,
            blocking_matchmaking_channel,
            blocking_news_channel,
            unrelated_channel,
        ]
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    handlers.managed_ui_service.create_managed_ui_channel(
        ui_type=ManagedUiType.REGISTER_PANEL,
        channel_id=99_001,
        message_id=88_001,
        created_by_discord_user_id=executor_discord_user_id,
    )
    cleanup_interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )

    asyncio.run(
        handlers.admin_cleanup_ui_channels(
            as_interaction(cleanup_interaction),
            "cleanup",
        )
    )

    assert_response(
        cleanup_interaction,
        ["setup の障害となる重複チャンネルを削除しました。"],
        ephemeral=True,
    )
    assert duplicate_register_channel.deleted is False
    assert blocking_matchmaking_channel.deleted is True
    assert blocking_news_channel.deleted is True
    assert unrelated_channel.deleted is False

    setup_interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        guild_id=guild.id,
        guild=guild,
    )
    asyncio.run(handlers.admin_setup_ui_channels(as_interaction(setup_interaction)))

    assert_response(setup_interaction, ["必要な UI 設置チャンネルを作成しました。"], ephemeral=True)


def test_admin_cleanup_ui_channels_returns_empty_when_no_blocking_channels(
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(id=2_102_8)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(handlers.admin_cleanup_ui_channels(as_interaction(interaction), "cleanup"))

    assert_response(interaction, ["削除対象の重複チャンネルはありません。"], ephemeral=True)


def test_admin_cleanup_ui_channels_reports_missing_manage_channels_permission(
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(
        id=2_102_9,
        me=FakeGuildMember(
            id=999_999,
            guild_permissions=discord.Permissions.none(),
        ),
    )
    channel = FakeTextChannel(id=60_014, name="レート戦マッチング", guild=guild)
    guild.channels.append(channel)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(handlers.admin_cleanup_ui_channels(as_interaction(interaction), "cleanup"))

    assert_response(
        interaction,
        ["Bot に必要な権限がありません。 不足している権限: チャンネルの管理"],
        ephemeral=True,
    )
    assert channel.deleted is False


def test_admin_cleanup_ui_channels_reports_discord_forbidden_detail(
    session_factory: sessionmaker[Session],
) -> None:
    guild = FakeGuild(id=2_103_0)
    channel = FakeTextChannel(
        id=60_015,
        name="レート戦マッチング",
        guild=guild,
        fail_delete_with=make_forbidden(),
    )
    guild.channels.append(channel)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10), guild_id=guild.id, guild=guild)

    asyncio.run(handlers.admin_cleanup_ui_channels(as_interaction(interaction), "cleanup"))

    assert_response(
        interaction,
        ["Bot に必要な権限がありません。 Discord API: 403 Forbidden (error code: 0): Forbidden"],
        ephemeral=True,
    )
    assert channel.deleted is False


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

    assert_response_sequence(
        restrict_interaction,
        [
            "指定したユーザーのキュー参加を7日制限しました。",
            f"<@{target_discord_user_id}> のキュー参加を7日制限しました。",
        ],
        [True, False],
    )
    assert_response_sequence(
        unrestrict_interaction,
        [
            "指定したユーザーのキュー参加制限を解除しました。",
            f"<@{target_discord_user_id}> のキュー参加制限を解除しました。",
        ],
        [True, False],
    )
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

    assert_response_sequence(
        interaction,
        [
            "指定したユーザーの観戦を1日制限しました。",
            f"<dummy_{target_discord_user_id}> の観戦を1日制限しました。",
        ],
        [True, False],
    )
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

    assert_response(interaction, ["対象ユーザーの指定が不正です。"], ephemeral=True)


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

    assert_response_sequence(
        interaction,
        [
            "ペナルティを加算しました。",
            f"<dummy_{target_discord_user_id}> の遅刻ペナルティを+1しました。現在の累積: 1",
        ],
        [True, False],
    )
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
        "dev_info_thread": ["command_name", "discord_user_id"],
        "dev_player_info": ["discord_user_id"],
        "dev_player_info_season": ["season_id", "discord_user_id"],
        "dev_leaderboard": ["match_format", "page", "discord_user_id"],
        "dev_leaderboard_season": ["season_id", "match_format", "page", "discord_user_id"],
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
    cleanup_command = tree.get_command("admin_cleanup_ui_channels")
    teardown_command = tree.get_command("admin_teardown_ui_channels")

    assert setup_custom_command is not None
    assert setup_all_command is not None
    assert cleanup_command is not None
    assert teardown_command is not None
    assert [parameter.name for parameter in setup_custom_command.parameters] == [
        "ui_type",
        "channel_name",
    ]
    assert [choice.value for choice in setup_custom_command.parameters[0].choices] == [
        definition.ui_type.value for definition in get_required_managed_ui_definitions()
    ]
    assert [parameter.name for parameter in setup_all_command.parameters] == []
    assert [parameter.name for parameter in cleanup_command.parameters] == ["confirm"]
    assert [parameter.name for parameter in teardown_command.parameters] == ["confirm"]


def test_update_matchmaking_status_command_is_registered_without_parameters() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    command = tree.get_command("update_matchmaking_status")

    assert command is not None
    assert [parameter.name for parameter in command.parameters] == []


def test_info_thread_command_is_registered_with_expected_parameters_and_choices() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    command = tree.get_command("info_thread")

    assert command is not None
    assert [parameter.name for parameter in command.parameters] == ["command_name"]
    assert [choice.value for choice in command.parameters[0].choices] == [
        InfoThreadCommandName.LEADERBOARD.value,
        InfoThreadCommandName.LEADERBOARD_SEASON.value,
        InfoThreadCommandName.PLAYER_INFO.value,
        InfoThreadCommandName.PLAYER_INFO_SEASON.value,
    ]


def test_leaderboard_command_is_registered_with_expected_parameters_and_choices() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    command = tree.get_command("leaderboard")

    assert command is not None
    assert [parameter.name for parameter in command.parameters] == ["match_format", "page"]
    assert [choice.value for choice in command.parameters[0].choices] == list(MATCH_FORMAT_CHOICES)


def test_leaderboard_season_command_is_registered_with_expected_parameters_and_choices() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    command = tree.get_command("leaderboard_season")

    assert command is not None
    assert [parameter.name for parameter in command.parameters] == [
        "season_id",
        "match_format",
        "page",
    ]
    assert [choice.value for choice in command.parameters[1].choices] == list(MATCH_FORMAT_CHOICES)


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


def test_register_command_defers_and_replies_via_followup(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    interaction = FakeInteraction(user=FakeUser(id=10))

    register_app_commands(tree, handlers)

    command = tree.get_command("register")

    assert command is not None
    asyncio.run(command.callback(as_interaction(interaction)))

    assert_response(interaction, ["登録が完了しました。"], ephemeral=True)
    assert interaction.response.defer_call_count == 1
    assert interaction.response.defer_ephemeral is True
    assert interaction.response.defer_thinking is True
    assert interaction.response.send_message_call_count == 0
    assert interaction.followup.send_call_count == 1


def test_register_command_returns_generic_internal_error_on_unhandled_exception(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    handlers = create_handlers(session_factory)
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    interaction = FakeInteraction(user=FakeUser(id=10))

    async def raise_unhandled(interaction: discord.Interaction[Any]) -> None:
        raise RuntimeError("boom")

    handlers.register = raise_unhandled  # type: ignore[method-assign]
    register_app_commands(tree, handlers)

    command = tree.get_command("register")

    assert command is not None
    with caplog.at_level(logging.ERROR):
        asyncio.run(command.callback(as_interaction(interaction)))

    assert_response(interaction, [APPLICATION_COMMAND_INTERNAL_ERROR_MESSAGE], ephemeral=True)
    assert interaction.response.defer_call_count == 1
    assert interaction.followup.send_call_count == 1
    assert "Unhandled exception in application command" in caplog.text


def test_register_command_returns_generic_internal_error_when_handler_sends_no_response(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    handlers = create_handlers(session_factory)
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    interaction = FakeInteraction(user=FakeUser(id=10))

    async def no_response(interaction: discord.Interaction[Any]) -> None:
        return None

    handlers.register = no_response  # type: ignore[method-assign]
    register_app_commands(tree, handlers)

    command = tree.get_command("register")

    assert command is not None
    with caplog.at_level(logging.ERROR):
        asyncio.run(command.callback(as_interaction(interaction)))

    assert_response(interaction, [APPLICATION_COMMAND_INTERNAL_ERROR_MESSAGE], ephemeral=True)
    assert interaction.response.defer_call_count == 1
    assert interaction.followup.send_call_count == 1
    assert "Application command completed without executor response" in caplog.text


def test_common_runner_sends_executor_and_public_followups_for_admin_penalty(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123
    create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))
    asyncio.run(
        handlers.run_application_command(
            as_interaction(interaction),
            "admin_add_late",
            handlers.admin_add_penalty,
            PenaltyType.LATE,
            dummy_user=f"<dummy_{target_discord_user_id}>",
        )
    )

    assert_response_sequence(
        interaction,
        [
            "ペナルティを加算しました。",
            f"<dummy_{target_discord_user_id}> の遅刻ペナルティを+1しました。現在の累積: 1",
        ],
        [True, False],
    )
    assert interaction.response.defer_call_count == 1
    assert interaction.response.send_message_call_count == 0
    assert interaction.followup.send_call_count == 2
