from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

import discord

from dxd_rating.contexts.ui.application import ManagedUiType

REGISTER_PANEL_MESSAGE = "\n".join(
    [
        "プレイヤー登録はこちらから行えます。",
        "ボタンを押すと利用規約に同意したものとして扱います。",
        "登録後は Bot の各種機能を利用できます。",
    ]
)
REGISTER_PANEL_BUTTON_LABEL = "利用規約に同意して登録"
REGISTER_PANEL_BUTTON_CUSTOM_ID = "dxd_rating:register_panel:register"
MAX_MANAGED_UI_CHANNEL_NAME_LENGTH = 100
REGISTER_PANEL_FALLBACK_ERROR_MESSAGE = "登録に失敗しました。管理者に確認してください。"

logger = logging.getLogger(__name__)


class RegisterPanelInteractionHandler(Protocol):
    async def register_from_ui(self, interaction: discord.Interaction[Any]) -> None: ...


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


def create_persistent_views(
    interaction_handler: RegisterPanelInteractionHandler,
) -> tuple[discord.ui.View, ...]:
    return (RegisterPanelView(interaction_handler),)


def create_managed_ui_view(
    ui_type: ManagedUiType,
    interaction_handler: RegisterPanelInteractionHandler,
) -> discord.ui.View:
    if ui_type is ManagedUiType.REGISTER_PANEL:
        return RegisterPanelView(interaction_handler)

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
) -> Mapping[discord.abc.Snowflake, discord.PermissionOverwrite]:
    if ui_type is not ManagedUiType.REGISTER_PANEL:
        raise ValueError(f"Unsupported ui_type: {ui_type}")

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            read_messages=True,
            read_message_history=True,
            send_messages=False,
            use_application_commands=True,
        )
    }
    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            read_messages=True,
            read_message_history=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
        )
    return overwrites


async def send_initial_managed_ui_message(
    channel: discord.TextChannel,
    *,
    ui_type: ManagedUiType,
    interaction_handler: RegisterPanelInteractionHandler,
) -> discord.Message:
    if ui_type is not ManagedUiType.REGISTER_PANEL:
        raise ValueError(f"Unsupported ui_type: {ui_type}")

    return await channel.send(
        content=REGISTER_PANEL_MESSAGE,
        view=RegisterPanelView(interaction_handler),
    )
