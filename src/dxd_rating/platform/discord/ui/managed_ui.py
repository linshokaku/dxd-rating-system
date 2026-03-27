from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import discord

from dxd_rating.contexts.ui.application import ManagedUiType
from dxd_rating.platform.db.models import MatchFormat
from dxd_rating.shared.constants import (
    get_match_format_definitions,
    get_match_queue_class_definitions,
    normalize_match_queue_name,
)

REGISTER_PANEL_MESSAGE = "\n".join(
    [
        "プレイヤー登録はこちらから行えます。",
        "ボタンを押すと利用規約に同意したものとして扱います。",
        "登録後は Bot の各種機能を利用できます。",
        "登録後はマッチング関連チャンネルとシステムアナウンスを閲覧できます。",
    ]
)
REGISTER_PANEL_BUTTON_LABEL = "利用規約に同意して登録"
REGISTER_PANEL_BUTTON_CUSTOM_ID = "dxd_rating:register_panel:register"
MATCHMAKING_CHANNEL_MESSAGE = "\n".join(
    [
        "この UI はマッチングキュー参加用です。",
        "試合形式と階級を選んでから参加ボタンを押してください。",
        "在席更新とキュー退出は /present と /leave を使ってください。",
    ]
)
MATCHMAKING_CHANNEL_MATCH_FORMAT_PLACEHOLDER = "試合形式を選択"
MATCHMAKING_CHANNEL_QUEUE_NAME_PLACEHOLDER = "階級を選択"
MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL = "参加"
MATCHMAKING_CHANNEL_MATCH_FORMAT_SELECT_CUSTOM_ID = "dxd_rating:matchmaking_channel:match_format"
MATCHMAKING_CHANNEL_QUEUE_NAME_SELECT_CUSTOM_ID = "dxd_rating:matchmaking_channel:queue_name"
MATCHMAKING_CHANNEL_JOIN_BUTTON_CUSTOM_ID = "dxd_rating:matchmaking_channel:join"
MATCHMAKING_CHANNEL_SELECT_MATCH_FORMAT_MESSAGE = "試合形式を選択してください。"
MATCHMAKING_CHANNEL_SELECT_QUEUE_NAME_MESSAGE = "階級を選択してください。"
MATCHMAKING_NEWS_CHANNEL_MESSAGE = "\n".join(
    [
        "このチャンネルにはマッチ成立時のアナウンスが投稿されます。",
        "観戦ボタンもこのチャンネルのアナウンスメッセージに表示されます。",
    ]
)
SYSTEM_ANNOUNCEMENTS_CHANNEL_MESSAGE = "このチャンネルは運営からのシステムアナウンス専用です。"
ADMIN_CONTACT_CHANNEL_MESSAGE = "運営への連絡やフィードバックはこちらへどうぞ。"
MAX_MANAGED_UI_CHANNEL_NAME_LENGTH = 100
REGISTER_PANEL_FALLBACK_ERROR_MESSAGE = "登録に失敗しました。管理者に確認してください。"
MATCHMAKING_CHANNEL_FALLBACK_ERROR_MESSAGE = "操作に失敗しました。管理者に確認してください。"

logger = logging.getLogger(__name__)


class RegisterPanelInteractionHandler(Protocol):
    async def register_from_ui(self, interaction: discord.Interaction[Any]) -> None: ...


class MatchmakingPanelInteractionHandler(Protocol):
    async def join(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
        queue_name: str,
    ) -> None: ...


class ManagedUiInteractionHandler(
    RegisterPanelInteractionHandler,
    MatchmakingPanelInteractionHandler,
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
        await self._interaction_handler.register_from_ui(interaction)

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
    match_format: str | None = None
    queue_name: str | None = None


def _build_matchmaking_match_format_options() -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=definition.description, value=definition.match_format.value)
        for definition in get_match_format_definitions()
    ]


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


async def _send_ephemeral_component_message(
    interaction: discord.Interaction[Any],
    message: str,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
        return

    await interaction.response.send_message(message, ephemeral=True)


class MatchmakingMatchFormatSelect(discord.ui.Select["MatchmakingPanelView"]):
    def __init__(self) -> None:
        super().__init__(
            placeholder=MATCHMAKING_CHANNEL_MATCH_FORMAT_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_build_matchmaking_match_format_options(),
            custom_id=MATCHMAKING_CHANNEL_MATCH_FORMAT_SELECT_CUSTOM_ID,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Matchmaking panel view is not attached")

        await view.select_match_format(interaction, self.values[0])


class MatchmakingQueueNameSelect(discord.ui.Select["MatchmakingPanelView"]):
    def __init__(self) -> None:
        super().__init__(
            placeholder=MATCHMAKING_CHANNEL_QUEUE_NAME_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_build_matchmaking_queue_options(MatchFormat.THREE_VS_THREE),
            custom_id=MATCHMAKING_CHANNEL_QUEUE_NAME_SELECT_CUSTOM_ID,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Matchmaking panel view is not attached")

        await view.select_queue_name(interaction, self.values[0])


class MatchmakingJoinButton(discord.ui.Button["MatchmakingPanelView"]):
    def __init__(self) -> None:
        super().__init__(
            label=MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id=MATCHMAKING_CHANNEL_JOIN_BUTTON_CUSTOM_ID,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Matchmaking panel view is not attached")

        await view.join_queue(interaction)


class MatchmakingPanelView(discord.ui.View):
    def __init__(self, interaction_handler: MatchmakingPanelInteractionHandler) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler
        self._selection_state_by_user_id: dict[int, MatchmakingPanelSelectionState] = {}
        self.match_format_select = MatchmakingMatchFormatSelect()
        self.queue_name_select = MatchmakingQueueNameSelect()
        self.join_button = MatchmakingJoinButton()
        self.add_item(self.match_format_select)
        self.add_item(self.queue_name_select)
        self.add_item(self.join_button)

    async def select_match_format(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
    ) -> None:
        selection_state = self._selection_state_by_user_id.setdefault(
            interaction.user.id,
            MatchmakingPanelSelectionState(),
        )
        selection_state.match_format = match_format

        try:
            selected_match_format = MatchFormat(match_format)
        except ValueError:
            selection_state.queue_name = None
            await interaction.response.defer()
            return

        valid_queue_names = set(_get_matchmaking_queue_names_for_format(selected_match_format))
        if selection_state.queue_name not in valid_queue_names:
            selection_state.queue_name = None

        await interaction.response.defer()

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
        if selection_state.match_format is None:
            await _send_ephemeral_component_message(
                interaction,
                MATCHMAKING_CHANNEL_SELECT_MATCH_FORMAT_MESSAGE,
            )
            return

        if selection_state.queue_name is None:
            await _send_ephemeral_component_message(
                interaction,
                MATCHMAKING_CHANNEL_SELECT_QUEUE_NAME_MESSAGE,
            )
            return

        await self._interaction_handler.join(
            interaction,
            selection_state.match_format,
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


def create_persistent_views(
    interaction_handler: ManagedUiInteractionHandler,
) -> tuple[discord.ui.View, ...]:
    return (
        RegisterPanelView(interaction_handler),
        MatchmakingPanelView(interaction_handler),
    )


def has_persistent_managed_ui_view(ui_type: ManagedUiType) -> bool:
    return ui_type in (
        ManagedUiType.REGISTER_PANEL,
        ManagedUiType.MATCHMAKING_CHANNEL,
    )


def create_managed_ui_view(
    ui_type: ManagedUiType,
    interaction_handler: ManagedUiInteractionHandler,
) -> discord.ui.View:
    if ui_type is ManagedUiType.REGISTER_PANEL:
        return RegisterPanelView(interaction_handler)
    if ui_type is ManagedUiType.MATCHMAKING_CHANNEL:
        return MatchmakingPanelView(interaction_handler)

    raise ValueError(f"Unsupported ui_type: {ui_type}")


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
    visible_member: discord.abc.Snowflake | None = None,
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
    else:
        raise ValueError(f"Unsupported ui_type: {ui_type}")

    if private_channel and visible_member is not None:
        overwrites[visible_member] = _build_private_visible_overwrite(ui_type)

    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            read_messages=True,
            read_message_history=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            manage_threads=True,
        )
    return overwrites


def _build_private_visible_overwrite(ui_type: ManagedUiType) -> discord.PermissionOverwrite:
    if ui_type is ManagedUiType.ADMIN_CONTACT_CHANNEL:
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
) -> discord.Message:
    if ui_type is ManagedUiType.REGISTER_PANEL:
        return await channel.send(
            content=REGISTER_PANEL_MESSAGE,
            view=RegisterPanelView(interaction_handler),
        )
    if ui_type is ManagedUiType.MATCHMAKING_CHANNEL:
        return await channel.send(
            content=MATCHMAKING_CHANNEL_MESSAGE,
            view=MatchmakingPanelView(interaction_handler),
        )
    if ui_type is ManagedUiType.MATCHMAKING_NEWS_CHANNEL:
        return await channel.send(content=MATCHMAKING_NEWS_CHANNEL_MESSAGE)
    if ui_type is ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL:
        return await channel.send(content=SYSTEM_ANNOUNCEMENTS_CHANNEL_MESSAGE)
    if ui_type is ManagedUiType.ADMIN_CONTACT_CHANNEL:
        return await channel.send(content=ADMIN_CONTACT_CHANNEL_MESSAGE)

    raise ValueError(f"Unsupported ui_type: {ui_type}")
