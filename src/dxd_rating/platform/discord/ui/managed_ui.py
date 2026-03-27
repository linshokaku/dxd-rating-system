from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

import discord

from dxd_rating.contexts.ui.application import ManagedUiType
from dxd_rating.platform.db.models import MatchFormat
from dxd_rating.shared.constants import REGULAR_QUEUE_BASELINE_RATING

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
        "3v3 のマッチングキュー参加、在席更新、退出はこちらから行えます。",
        (
            f"beginner はレート {int(REGULAR_QUEUE_BASELINE_RATING)} 未満、"
            f"master は {int(REGULAR_QUEUE_BASELINE_RATING)} 以上、"
            "regular は誰でも参加できます。"
        ),
        (
            "通常メッセージは送信できません。"
            "連絡が必要な場合は Bot が作成する private thread を利用します。"
        ),
    ]
)
MATCHMAKING_CHANNEL_JOIN_BEGINNER_BUTTON_LABEL = "3v3 beginner に参加"
MATCHMAKING_CHANNEL_JOIN_REGULAR_BUTTON_LABEL = "3v3 regular に参加"
MATCHMAKING_CHANNEL_JOIN_MASTER_BUTTON_LABEL = "3v3 master に参加"
MATCHMAKING_CHANNEL_PRESENT_BUTTON_LABEL = "在席更新"
MATCHMAKING_CHANNEL_LEAVE_BUTTON_LABEL = "キュー退出"
MATCHMAKING_CHANNEL_JOIN_BEGINNER_BUTTON_CUSTOM_ID = (
    "dxd_rating:matchmaking_channel:join:3v3:beginner"
)
MATCHMAKING_CHANNEL_JOIN_REGULAR_BUTTON_CUSTOM_ID = (
    "dxd_rating:matchmaking_channel:join:3v3:regular"
)
MATCHMAKING_CHANNEL_JOIN_MASTER_BUTTON_CUSTOM_ID = "dxd_rating:matchmaking_channel:join:3v3:master"
MATCHMAKING_CHANNEL_PRESENT_BUTTON_CUSTOM_ID = "dxd_rating:matchmaking_channel:present"
MATCHMAKING_CHANNEL_LEAVE_BUTTON_CUSTOM_ID = "dxd_rating:matchmaking_channel:leave"
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

    async def present(self, interaction: discord.Interaction[Any]) -> None: ...

    async def leave(self, interaction: discord.Interaction[Any]) -> None: ...


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


class MatchmakingPanelView(discord.ui.View):
    def __init__(self, interaction_handler: MatchmakingPanelInteractionHandler) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler

    @discord.ui.button(
        label=MATCHMAKING_CHANNEL_JOIN_BEGINNER_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id=MATCHMAKING_CHANNEL_JOIN_BEGINNER_BUTTON_CUSTOM_ID,
        row=0,
    )
    async def join_beginner_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.join(
            interaction,
            MatchFormat.THREE_VS_THREE.value,
            "beginner",
        )

    @discord.ui.button(
        label=MATCHMAKING_CHANNEL_JOIN_REGULAR_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id=MATCHMAKING_CHANNEL_JOIN_REGULAR_BUTTON_CUSTOM_ID,
        row=0,
    )
    async def join_regular_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.join(
            interaction,
            MatchFormat.THREE_VS_THREE.value,
            "regular",
        )

    @discord.ui.button(
        label=MATCHMAKING_CHANNEL_JOIN_MASTER_BUTTON_LABEL,
        style=discord.ButtonStyle.primary,
        custom_id=MATCHMAKING_CHANNEL_JOIN_MASTER_BUTTON_CUSTOM_ID,
        row=0,
    )
    async def join_master_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.join(
            interaction,
            MatchFormat.THREE_VS_THREE.value,
            "master",
        )

    @discord.ui.button(
        label=MATCHMAKING_CHANNEL_PRESENT_BUTTON_LABEL,
        style=discord.ButtonStyle.success,
        custom_id=MATCHMAKING_CHANNEL_PRESENT_BUTTON_CUSTOM_ID,
        row=1,
    )
    async def present_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.present(interaction)

    @discord.ui.button(
        label=MATCHMAKING_CHANNEL_LEAVE_BUTTON_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id=MATCHMAKING_CHANNEL_LEAVE_BUTTON_CUSTOM_ID,
        row=1,
    )
    async def leave_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.leave(interaction)

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
) -> Mapping[discord.abc.Snowflake, discord.PermissionOverwrite]:
    if ui_type is ManagedUiType.REGISTER_PANEL:
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                read_messages=True,
                read_message_history=True,
                send_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                use_application_commands=True,
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
                view_channel=True,
                read_messages=True,
                read_message_history=True,
                send_messages=True,
                create_public_threads=False,
                create_private_threads=False,
                use_application_commands=True,
            )
        }
    else:
        raise ValueError(f"Unsupported ui_type: {ui_type}")

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
