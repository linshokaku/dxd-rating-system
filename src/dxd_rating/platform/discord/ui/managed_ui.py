from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import discord

from dxd_rating.contexts.ui.application import (
    InfoThreadCommandName,
    ManagedUiType,
    get_managed_ui_definition,
)
from dxd_rating.platform.db.models import MatchFormat
from dxd_rating.platform.discord.copy.info import (
    INFO_CHANNEL_FALLBACK_ERROR_MESSAGE,
    INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
    INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
    INFO_CHANNEL_MESSAGE,
    INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
    INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
)
from dxd_rating.platform.discord.copy.match import MATCHMAKING_NEWS_CHANNEL_MESSAGE
from dxd_rating.platform.discord.copy.matchmaking import (
    MATCHMAKING_CHANNEL_FALLBACK_ERROR_MESSAGE,
    MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL,
    MATCHMAKING_CHANNEL_QUEUE_NAME_PLACEHOLDER,
    MATCHMAKING_CHANNEL_SELECT_QUEUE_NAME_MESSAGE,
    MATCHMAKING_CHANNEL_STATUS_PLACEHOLDER_MESSAGE,
    MATCHMAKING_CHANNEL_STATUS_UPDATE_FALLBACK_ERROR_MESSAGE,
    MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL,
    build_matchmaking_guide_message,
    build_matchmaking_panel_message,
)
from dxd_rating.platform.discord.copy.registration import (
    REGISTER_PANEL_BUTTON_LABEL,
    REGISTER_PANEL_FALLBACK_ERROR_MESSAGE,
    REGISTER_PANEL_MESSAGE,
)
from dxd_rating.platform.discord.copy.system import (
    ADMIN_CONTACT_CHANNEL_MESSAGE,
    ADMIN_OPERATIONS_CHANNEL_MESSAGE,
    SYSTEM_ANNOUNCEMENTS_CHANNEL_MESSAGE,
)
from dxd_rating.shared.constants import (
    get_match_format_definitions,
    get_match_queue_class_definitions,
    normalize_match_queue_name,
)

REGISTER_PANEL_BUTTON_CUSTOM_ID = "dxd_rating:register_panel:register"
MATCHMAKING_CHANNEL_QUEUE_NAME_SELECT_CUSTOM_ID_PREFIX = "dxd_rating:matchmaking_channel:queue_name"
MATCHMAKING_CHANNEL_JOIN_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:matchmaking_channel:join"
MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_CUSTOM_ID = "dxd_rating:matchmaking_channel:update_status"
INFO_CHANNEL_LEADERBOARD_BUTTON_CUSTOM_ID = "dxd_rating:info_channel:leaderboard"
INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_CUSTOM_ID = "dxd_rating:info_channel:leaderboard_season"
INFO_CHANNEL_PLAYER_INFO_BUTTON_CUSTOM_ID = "dxd_rating:info_channel:player_info"
INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_CUSTOM_ID = "dxd_rating:info_channel:player_info_season"
MAX_MANAGED_UI_CHANNEL_NAME_LENGTH = 100

logger = logging.getLogger(__name__)


class _ComponentInteractionHandler(Protocol):
    async def send_component_message(
        self,
        interaction: discord.Interaction[Any],
        message: str,
    ) -> None: ...

    async def run_component_interaction(
        self,
        interaction: discord.Interaction[Any],
        interaction_name: str,
        callback: Callable[[], Awaitable[None]],
        *,
        fallback_message: str,
    ) -> None: ...


class RegisterPanelInteractionHandler(_ComponentInteractionHandler, Protocol):
    async def register_from_ui(self, interaction: discord.Interaction[Any]) -> None: ...


class MatchmakingPanelInteractionHandler(_ComponentInteractionHandler, Protocol):
    async def join_from_ui(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
        queue_name: str,
    ) -> None: ...


class MatchmakingStatusInteractionHandler(_ComponentInteractionHandler, Protocol):
    async def update_matchmaking_status_from_ui(
        self,
        interaction: discord.Interaction[Any],
    ) -> None: ...


class InfoChannelInteractionHandler(_ComponentInteractionHandler, Protocol):
    async def info_thread_from_ui(
        self,
        interaction: discord.Interaction[Any],
        command_name: InfoThreadCommandName,
    ) -> None: ...


class ManagedUiInteractionHandler(
    RegisterPanelInteractionHandler,
    MatchmakingStatusInteractionHandler,
    MatchmakingPanelInteractionHandler,
    InfoChannelInteractionHandler,
    Protocol,
):
    pass


class RegisterPanelView(discord.ui.View):
    def __init__(self, interaction_handler: RegisterPanelInteractionHandler) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler

    @discord.ui.button(
        label=REGISTER_PANEL_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id=REGISTER_PANEL_BUTTON_CUSTOM_ID,
    )
    async def register_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.run_component_interaction(
            interaction,
            "register_panel:register",
            lambda: self._interaction_handler.register_from_ui(interaction),
            fallback_message=REGISTER_PANEL_FALLBACK_ERROR_MESSAGE,
        )

    async def on_error(
        self,
        interaction: discord.Interaction[Any],
        error: Exception,
        _: discord.ui.Item[discord.ui.View],
    ) -> None:
        logger.exception("Register panel interaction failed", exc_info=error)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    REGISTER_PANEL_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    REGISTER_PANEL_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
        except Exception:
            logger.exception("Failed to send register panel fallback error response")


@dataclass(slots=True)
class MatchmakingPanelSelectionState:
    queue_name: str | None = None


@dataclass(frozen=True, slots=True)
class InitialManagedUiMessages:
    primary_message: discord.Message | None = None
    status_message: discord.Message | None = None
    matchmaking_one_v_one_message: discord.Message | None = None
    matchmaking_two_v_two_message: discord.Message | None = None
    matchmaking_three_v_three_message: discord.Message | None = None


def _build_matchmaking_component_custom_id(prefix: str, match_format: MatchFormat) -> str:
    return f"{prefix}:{match_format.value}"


def build_matchmaking_queue_name_select_custom_id(match_format: MatchFormat) -> str:
    return _build_matchmaking_component_custom_id(
        MATCHMAKING_CHANNEL_QUEUE_NAME_SELECT_CUSTOM_ID_PREFIX,
        match_format,
    )


def build_matchmaking_join_button_custom_id(match_format: MatchFormat) -> str:
    return _build_matchmaking_component_custom_id(
        MATCHMAKING_CHANNEL_JOIN_BUTTON_CUSTOM_ID_PREFIX,
        match_format,
    )


def _get_matchmaking_queue_names_for_format(match_format: MatchFormat) -> tuple[str, ...]:
    queue_names: list[str] = []
    seen_queue_names: set[str] = set()
    for definition in get_match_queue_class_definitions():
        if definition.match_format is not match_format:
            continue

        normalized_queue_name = normalize_match_queue_name(definition.queue_name)
        if normalized_queue_name in seen_queue_names:
            continue

        queue_names.append(definition.queue_name)
        seen_queue_names.add(normalized_queue_name)

    return tuple(queue_names)


def _build_matchmaking_queue_options(match_format: MatchFormat) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=queue_name, value=queue_name)
        for queue_name in _get_matchmaking_queue_names_for_format(match_format)
    ]


class FormatSpecificMatchmakingQueueNameSelect(
    discord.ui.Select["FormatSpecificMatchmakingPanelView"]
):
    def __init__(self, match_format: MatchFormat) -> None:
        self.match_format = match_format
        super().__init__(
            placeholder=MATCHMAKING_CHANNEL_QUEUE_NAME_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_build_matchmaking_queue_options(match_format),
            custom_id=build_matchmaking_queue_name_select_custom_id(match_format),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Matchmaking panel view is not attached")

        await view.select_queue_name(interaction, self.values[0])


class FormatSpecificMatchmakingJoinButton(
    discord.ui.Button["FormatSpecificMatchmakingPanelView"]
):
    def __init__(self, match_format: MatchFormat) -> None:
        self.match_format = match_format
        super().__init__(
            label=MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id=build_matchmaking_join_button_custom_id(match_format),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Matchmaking panel view is not attached")

        await view._interaction_handler.run_component_interaction(
            interaction,
            f"matchmaking_channel:{view.match_format.value}:join",
            lambda: view.join_queue(interaction),
            fallback_message=MATCHMAKING_CHANNEL_FALLBACK_ERROR_MESSAGE,
        )


class FormatSpecificMatchmakingPanelView(discord.ui.View):
    def __init__(
        self,
        interaction_handler: MatchmakingPanelInteractionHandler,
        match_format: MatchFormat,
    ) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler
        self.match_format = match_format
        self._selection_state_by_user_id: dict[int, MatchmakingPanelSelectionState] = {}
        self.queue_name_select = FormatSpecificMatchmakingQueueNameSelect(match_format)
        self.join_button = FormatSpecificMatchmakingJoinButton(match_format)
        self.add_item(self.queue_name_select)
        self.add_item(self.join_button)

    async def select_queue_name(
        self,
        interaction: discord.Interaction[Any],
        queue_name: str,
    ) -> None:
        selection_state = self._selection_state_by_user_id.setdefault(
            interaction.user.id,
            MatchmakingPanelSelectionState(),
        )
        selection_state.queue_name = queue_name
        await interaction.response.defer()

    async def join_queue(self, interaction: discord.Interaction[Any]) -> None:
        selection_state = self._selection_state_by_user_id.get(
            interaction.user.id,
            MatchmakingPanelSelectionState(),
        )
        if selection_state.queue_name is None:
            await self._interaction_handler.send_component_message(
                interaction,
                MATCHMAKING_CHANNEL_SELECT_QUEUE_NAME_MESSAGE,
            )
            return

        await self._interaction_handler.join_from_ui(
            interaction,
            self.match_format.value,
            selection_state.queue_name,
        )

    async def on_error(
        self,
        interaction: discord.Interaction[Any],
        error: Exception,
        _: discord.ui.Item[discord.ui.View],
    ) -> None:
        logger.exception("Matchmaking panel interaction failed", exc_info=error)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    MATCHMAKING_CHANNEL_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    MATCHMAKING_CHANNEL_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
        except Exception:
            logger.exception("Failed to send matchmaking panel fallback error response")


def create_matchmaking_panel_view(
    interaction_handler: MatchmakingPanelInteractionHandler,
    match_format: MatchFormat,
) -> discord.ui.View:
    return FormatSpecificMatchmakingPanelView(interaction_handler, match_format)


def create_matchmaking_panel_views(
    interaction_handler: MatchmakingPanelInteractionHandler,
) -> tuple[discord.ui.View, ...]:
    return tuple(
        create_matchmaking_panel_view(
            interaction_handler,
            definition.match_format,
        )
        for definition in get_match_format_definitions()
    )


class MatchmakingStatusView(discord.ui.View):
    def __init__(self, interaction_handler: MatchmakingStatusInteractionHandler) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler

    @discord.ui.button(
        label=MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL,
        style=discord.ButtonStyle.success,
        custom_id=MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_CUSTOM_ID,
    )
    async def update_status_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.run_component_interaction(
            interaction,
            "matchmaking_channel:update_status",
            lambda: self._interaction_handler.update_matchmaking_status_from_ui(interaction),
            fallback_message=MATCHMAKING_CHANNEL_STATUS_UPDATE_FALLBACK_ERROR_MESSAGE,
        )

    async def on_error(
        self,
        interaction: discord.Interaction[Any],
        error: Exception,
        _: discord.ui.Item[discord.ui.View],
    ) -> None:
        logger.exception("Matchmaking status interaction failed", exc_info=error)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    MATCHMAKING_CHANNEL_STATUS_UPDATE_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    MATCHMAKING_CHANNEL_STATUS_UPDATE_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
        except Exception:
            logger.exception("Failed to send matchmaking status fallback error response")


class InfoChannelView(discord.ui.View):
    def __init__(self, interaction_handler: InfoChannelInteractionHandler) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler

    @discord.ui.button(
        label=INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id=INFO_CHANNEL_LEADERBOARD_BUTTON_CUSTOM_ID,
        row=0,
    )
    async def leaderboard_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.run_component_interaction(
            interaction,
            "info_channel:leaderboard",
            lambda: self._interaction_handler.info_thread_from_ui(
                interaction,
                InfoThreadCommandName.LEADERBOARD,
            ),
            fallback_message=INFO_CHANNEL_FALLBACK_ERROR_MESSAGE,
        )

    @discord.ui.button(
        label=INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id=INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_CUSTOM_ID,
        row=0,
    )
    async def leaderboard_season_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.run_component_interaction(
            interaction,
            "info_channel:leaderboard_season",
            lambda: self._interaction_handler.info_thread_from_ui(
                interaction,
                InfoThreadCommandName.LEADERBOARD_SEASON,
            ),
            fallback_message=INFO_CHANNEL_FALLBACK_ERROR_MESSAGE,
        )

    @discord.ui.button(
        label=INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id=INFO_CHANNEL_PLAYER_INFO_BUTTON_CUSTOM_ID,
        row=1,
    )
    async def player_info_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.run_component_interaction(
            interaction,
            "info_channel:player_info",
            lambda: self._interaction_handler.info_thread_from_ui(
                interaction,
                InfoThreadCommandName.PLAYER_INFO,
            ),
            fallback_message=INFO_CHANNEL_FALLBACK_ERROR_MESSAGE,
        )

    @discord.ui.button(
        label=INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id=INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_CUSTOM_ID,
        row=1,
    )
    async def player_info_season_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.run_component_interaction(
            interaction,
            "info_channel:player_info_season",
            lambda: self._interaction_handler.info_thread_from_ui(
                interaction,
                InfoThreadCommandName.PLAYER_INFO_SEASON,
            ),
            fallback_message=INFO_CHANNEL_FALLBACK_ERROR_MESSAGE,
        )

    async def on_error(
        self,
        interaction: discord.Interaction[Any],
        error: Exception,
        _: discord.ui.Item[discord.ui.View],
    ) -> None:
        logger.exception("Info channel interaction failed", exc_info=error)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    INFO_CHANNEL_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    INFO_CHANNEL_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
        except Exception:
            logger.exception("Failed to send info channel fallback error response")


def create_persistent_views(
    interaction_handler: ManagedUiInteractionHandler,
) -> tuple[discord.ui.View, ...]:
    return (
        RegisterPanelView(interaction_handler),
        MatchmakingStatusView(interaction_handler),
        *create_matchmaking_panel_views(interaction_handler),
        InfoChannelView(interaction_handler),
    )


def has_persistent_managed_ui_view(ui_type: ManagedUiType) -> bool:
    return get_managed_ui_definition(ui_type).installs_persistent_view


def create_managed_ui_view(
    ui_type: ManagedUiType,
    interaction_handler: ManagedUiInteractionHandler,
) -> discord.ui.View:
    if ui_type is ManagedUiType.REGISTER_PANEL:
        return RegisterPanelView(interaction_handler)
    if ui_type is ManagedUiType.INFO_CHANNEL:
        return InfoChannelView(interaction_handler)

    raise ValueError(f"Unsupported ui_type: {ui_type}")


def create_matchmaking_status_view(
    interaction_handler: MatchmakingStatusInteractionHandler,
) -> discord.ui.View:
    return MatchmakingStatusView(interaction_handler)


def is_valid_managed_ui_channel_name(channel_name: str) -> bool:
    return (
        channel_name == channel_name.strip()
        and 1 <= len(channel_name) <= MAX_MANAGED_UI_CHANNEL_NAME_LENGTH
        and "\n" not in channel_name
        and "\r" not in channel_name
    )


def build_managed_ui_channel_overwrites(
    guild: discord.Guild,
    ui_type: ManagedUiType,
    *,
    registered_player_role: discord.abc.Snowflake | None = None,
    private_channel: bool = False,
    visible_members: Sequence[discord.abc.Snowflake] = (),
) -> Mapping[discord.abc.Snowflake, discord.PermissionOverwrite]:
    if ui_type is ManagedUiType.REGISTER_PANEL:
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=not private_channel,
                read_messages=not private_channel,
                read_message_history=not private_channel,
                send_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                use_application_commands=not private_channel,
            )
        }
    elif ui_type in (
        ManagedUiType.MATCHMAKING_CHANNEL,
        ManagedUiType.MATCHMAKING_NEWS_CHANNEL,
        ManagedUiType.INFO_CHANNEL,
        ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL,
    ):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                read_messages=False,
                read_message_history=False,
                send_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                use_application_commands=False,
            )
        }
        if registered_player_role is not None:
            overwrites[registered_player_role] = discord.PermissionOverwrite(
                view_channel=True,
                read_messages=True,
                read_message_history=True,
                send_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                use_application_commands=True,
            )
    elif ui_type is ManagedUiType.ADMIN_CONTACT_CHANNEL:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=not private_channel,
                read_messages=not private_channel,
                read_message_history=not private_channel,
                send_messages=not private_channel,
                create_public_threads=False,
                create_private_threads=False,
                use_application_commands=not private_channel,
            )
        }
    elif ui_type is ManagedUiType.ADMIN_OPERATIONS_CHANNEL:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                read_messages=False,
                read_message_history=False,
                send_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                use_application_commands=False,
            )
        }
    else:
        raise ValueError(f"Unsupported ui_type: {ui_type}")

    if private_channel:
        for visible_member in visible_members:
            overwrites[visible_member] = _build_private_visible_overwrite(ui_type)

    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            read_messages=True,
            read_message_history=True,
            send_messages=True,
            create_private_threads=True,
            send_messages_in_threads=True,
            manage_channels=True,
            manage_messages=True,
            manage_threads=True,
        )
    return overwrites


def _build_private_visible_overwrite(ui_type: ManagedUiType) -> discord.PermissionOverwrite:
    if ui_type in (
        ManagedUiType.ADMIN_CONTACT_CHANNEL,
        ManagedUiType.ADMIN_OPERATIONS_CHANNEL,
    ):
        return discord.PermissionOverwrite(
            view_channel=True,
            read_messages=True,
            read_message_history=True,
            send_messages=True,
            create_public_threads=False,
            create_private_threads=False,
            use_application_commands=True,
        )

    if ui_type in (
        ManagedUiType.REGISTER_PANEL,
        ManagedUiType.MATCHMAKING_CHANNEL,
        ManagedUiType.MATCHMAKING_NEWS_CHANNEL,
        ManagedUiType.INFO_CHANNEL,
        ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL,
    ):
        return discord.PermissionOverwrite(
            view_channel=True,
            read_messages=True,
            read_message_history=True,
            send_messages=False,
            create_public_threads=False,
            create_private_threads=False,
            use_application_commands=True,
        )

    raise ValueError(f"Unsupported ui_type: {ui_type}")


async def send_initial_managed_ui_message(
    channel: discord.TextChannel,
    *,
    ui_type: ManagedUiType,
    interaction_handler: ManagedUiInteractionHandler,
    matchmaking_guide_url: str,
) -> InitialManagedUiMessages:
    if ui_type is ManagedUiType.REGISTER_PANEL:
        return InitialManagedUiMessages(
            primary_message=await channel.send(
                content=REGISTER_PANEL_MESSAGE,
                view=RegisterPanelView(interaction_handler),
            )
        )
    if ui_type is ManagedUiType.MATCHMAKING_CHANNEL:
        await channel.send(
            content=build_matchmaking_guide_message(matchmaking_guide_url),
            suppress_embeds=True,
        )
        status_message = await channel.send(
            content=MATCHMAKING_CHANNEL_STATUS_PLACEHOLDER_MESSAGE,
            view=create_matchmaking_status_view(interaction_handler),
        )
        panel_messages_by_format: dict[MatchFormat, discord.Message] = {}
        for definition in get_match_format_definitions():
            panel_messages_by_format[definition.match_format] = await channel.send(
                content=build_matchmaking_panel_message(definition.match_format),
                view=create_matchmaking_panel_view(
                    interaction_handler,
                    definition.match_format,
                ),
            )
        return InitialManagedUiMessages(
            status_message=status_message,
            matchmaking_one_v_one_message=panel_messages_by_format[MatchFormat.ONE_VS_ONE],
            matchmaking_two_v_two_message=panel_messages_by_format[MatchFormat.TWO_VS_TWO],
            matchmaking_three_v_three_message=panel_messages_by_format[
                MatchFormat.THREE_VS_THREE
            ],
        )
    if ui_type is ManagedUiType.MATCHMAKING_NEWS_CHANNEL:
        return InitialManagedUiMessages(
            primary_message=await channel.send(content=MATCHMAKING_NEWS_CHANNEL_MESSAGE)
        )
    if ui_type is ManagedUiType.INFO_CHANNEL:
        return InitialManagedUiMessages(
            primary_message=await channel.send(
                content=INFO_CHANNEL_MESSAGE,
                view=InfoChannelView(interaction_handler),
            )
        )
    if ui_type is ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL:
        return InitialManagedUiMessages(
            primary_message=await channel.send(content=SYSTEM_ANNOUNCEMENTS_CHANNEL_MESSAGE)
        )
    if ui_type is ManagedUiType.ADMIN_CONTACT_CHANNEL:
        return InitialManagedUiMessages(
            primary_message=await channel.send(content=ADMIN_CONTACT_CHANNEL_MESSAGE)
        )
    if ui_type is ManagedUiType.ADMIN_OPERATIONS_CHANNEL:
        return InitialManagedUiMessages(
            primary_message=await channel.send(content=ADMIN_OPERATIONS_CHANNEL_MESSAGE)
        )

    raise ValueError(f"Unsupported ui_type: {ui_type}")
