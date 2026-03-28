from __future__ import annotations

import logging
from typing import Any, Protocol

import discord

MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL = "在席"
MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL = "マッチングキャンセル"
MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_CUSTOM_ID = "dxd_rating:matchmaking_presence:present"
MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_CUSTOM_ID = "dxd_rating:matchmaking_presence:leave"
MATCHMAKING_PRESENCE_THREAD_FALLBACK_ERROR_MESSAGE = (
    "操作に失敗しました。管理者に確認してください。"
)

logger = logging.getLogger(__name__)


class MatchmakingPresenceThreadInteractionHandler(Protocol):
    async def present_from_matchmaking_presence_thread(
        self, interaction: discord.Interaction[Any]
    ) -> None: ...

    async def leave_from_matchmaking_presence_thread(
        self, interaction: discord.Interaction[Any]
    ) -> None: ...


class MatchmakingPresenceThreadView(discord.ui.View):
    def __init__(self, interaction_handler: MatchmakingPresenceThreadInteractionHandler) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler

    @discord.ui.button(
        label=MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
        style=discord.ButtonStyle.success,
        custom_id=MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_CUSTOM_ID,
    )
    async def present_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.present_from_matchmaking_presence_thread(interaction)

    @discord.ui.button(
        label=MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
        style=discord.ButtonStyle.danger,
        custom_id=MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_CUSTOM_ID,
    )
    async def leave_button(
        self,
        interaction: discord.Interaction[Any],
        _: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self._interaction_handler.leave_from_matchmaking_presence_thread(interaction)

    async def on_error(
        self,
        interaction: discord.Interaction[Any],
        error: Exception,
        _: discord.ui.Item[discord.ui.View],
    ) -> None:
        logger.exception("Matchmaking presence thread interaction failed", exc_info=error)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    MATCHMAKING_PRESENCE_THREAD_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    MATCHMAKING_PRESENCE_THREAD_FALLBACK_ERROR_MESSAGE,
                    ephemeral=True,
                )
        except Exception:
            logger.exception("Failed to send matchmaking presence thread fallback error response")


def create_matchmaking_presence_thread_view(
    interaction_handler: MatchmakingPresenceThreadInteractionHandler,
) -> discord.ui.View:
    return MatchmakingPresenceThreadView(interaction_handler)
