from __future__ import annotations

import logging
import re
from typing import Any, ClassVar, Protocol

import discord

MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_LABEL = "観戦する"
MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_CUSTOM_ID_PREFIX = (
    "dxd_rating:matchmaking_news:spectate"
)
MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_TEMPLATE = (
    r"^dxd_rating:matchmaking_news:spectate:(?P<match_id>\d+)$"
)
MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_GUIDE_MESSAGE = (
    "観戦希望者は下の「観戦する」ボタンから応募してください。"
)
MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_FALLBACK_ERROR_MESSAGE = (
    "観戦応募に失敗しました。管理者に確認してください。"
)

logger = logging.getLogger(__name__)


class MatchmakingNewsMatchAnnouncementInteractionHandler(Protocol):
    async def spectate_from_matchmaking_news_match_announcement(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None: ...


class MatchmakingNewsMatchAnnouncementSpectateButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_TEMPLATE,
):
    _registered_interaction_handler: ClassVar[
        MatchmakingNewsMatchAnnouncementInteractionHandler | None
    ] = None

    def __init__(
        self,
        match_id: int,
        *,
        interaction_handler: MatchmakingNewsMatchAnnouncementInteractionHandler | None = None,
    ) -> None:
        self.match_id = match_id
        self._interaction_handler = interaction_handler or self._registered_interaction_handler
        super().__init__(
            discord.ui.Button(
                label=MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_LABEL,
                style=discord.ButtonStyle.primary,
                custom_id=self._build_custom_id(match_id),
            )
        )

    @classmethod
    def bind_interaction_handler(
        cls,
        interaction_handler: MatchmakingNewsMatchAnnouncementInteractionHandler,
    ) -> None:
        cls._registered_interaction_handler = interaction_handler

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
    ) -> MatchmakingNewsMatchAnnouncementSpectateButton:
        del interaction, item
        return cls(match_id=int(match.group("match_id")))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        if self._interaction_handler is None:
            logger.error(
                "Matchmaking news match announcement interaction handler is not configured"
            )
            await _send_fallback_error_message(interaction)
            return

        await self._interaction_handler.spectate_from_matchmaking_news_match_announcement(
            interaction,
            self.match_id,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(match_id: int) -> str:
        return f"{MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_CUSTOM_ID_PREFIX}:{match_id}"


class MatchmakingNewsMatchAnnouncementView(discord.ui.View):
    def __init__(
        self,
        interaction_handler: MatchmakingNewsMatchAnnouncementInteractionHandler,
        *,
        match_id: int,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            MatchmakingNewsMatchAnnouncementSpectateButton(
                match_id,
                interaction_handler=interaction_handler,
            )
        )


async def _send_fallback_error_message(interaction: discord.Interaction[Any]) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_FALLBACK_ERROR_MESSAGE,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_FALLBACK_ERROR_MESSAGE,
                ephemeral=True,
            )
    except Exception:
        logger.exception("Failed to send matchmaking news match announcement fallback response")


def create_matchmaking_news_match_announcement_view(
    interaction_handler: MatchmakingNewsMatchAnnouncementInteractionHandler,
    *,
    match_id: int,
) -> discord.ui.View:
    return MatchmakingNewsMatchAnnouncementView(
        interaction_handler,
        match_id=match_id,
    )


def register_matchmaking_news_match_announcement_dynamic_items(
    client: discord.Client,
    interaction_handler: MatchmakingNewsMatchAnnouncementInteractionHandler,
) -> None:
    MatchmakingNewsMatchAnnouncementSpectateButton.bind_interaction_handler(interaction_handler)
    client.add_dynamic_items(MatchmakingNewsMatchAnnouncementSpectateButton)
