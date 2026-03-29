from __future__ import annotations

import logging
import re
from typing import Any, ClassVar, Protocol

import discord

MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL = "無効試合申請"
MATCH_OPERATION_THREAD_VOID_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:match_operation_thread:void"
MATCH_OPERATION_THREAD_VOID_BUTTON_TEMPLATE = (
    r"^dxd_rating:match_operation_thread:void:(?P<match_id>\d+)$"
)
MATCH_OPERATION_THREAD_PARENT_BUTTON_LABEL = "親に立候補する"
MATCH_OPERATION_THREAD_PARENT_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:match_operation_thread:parent"
MATCH_OPERATION_THREAD_PARENT_BUTTON_TEMPLATE = (
    r"^dxd_rating:match_operation_thread:parent:(?P<match_id>\d+)$"
)
MATCH_OPERATION_THREAD_VOID_GUIDE_MESSAGE = (
    "無効試合とする必要がある場合は下の「無効試合申請」ボタンを押してください。"
)
MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE = "試合操作に失敗しました。管理者に確認してください。"

logger = logging.getLogger(__name__)


class MatchOperationThreadInteractionHandler(Protocol):
    async def parent_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None: ...

    async def void_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None: ...


class MatchOperationThreadVoidButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=MATCH_OPERATION_THREAD_VOID_BUTTON_TEMPLATE,
):
    _registered_interaction_handler: ClassVar[MatchOperationThreadInteractionHandler | None] = None

    def __init__(
        self,
        match_id: int,
        *,
        interaction_handler: MatchOperationThreadInteractionHandler | None = None,
    ) -> None:
        self.match_id = match_id
        self._interaction_handler = interaction_handler or self._registered_interaction_handler
        super().__init__(
            discord.ui.Button(
                label=MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL,
                style=discord.ButtonStyle.danger,
                custom_id=self._build_custom_id(match_id),
            )
        )

    @classmethod
    def bind_interaction_handler(
        cls,
        interaction_handler: MatchOperationThreadInteractionHandler,
    ) -> None:
        cls._registered_interaction_handler = interaction_handler

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
    ) -> MatchOperationThreadVoidButton:
        del interaction, item
        return cls(match_id=int(match.group("match_id")))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        if self._interaction_handler is None:
            logger.error("Match operation thread interaction handler is not configured")
            await _send_fallback_error_message(interaction)
            return

        await self._interaction_handler.void_from_match_operation_thread(
            interaction,
            self.match_id,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(match_id: int) -> str:
        return f"{MATCH_OPERATION_THREAD_VOID_BUTTON_CUSTOM_ID_PREFIX}:{match_id}"


class MatchOperationThreadParentButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=MATCH_OPERATION_THREAD_PARENT_BUTTON_TEMPLATE,
):
    _registered_interaction_handler: ClassVar[MatchOperationThreadInteractionHandler | None] = None

    def __init__(
        self,
        match_id: int,
        *,
        interaction_handler: MatchOperationThreadInteractionHandler | None = None,
    ) -> None:
        self.match_id = match_id
        self._interaction_handler = interaction_handler or self._registered_interaction_handler
        super().__init__(
            discord.ui.Button(
                label=MATCH_OPERATION_THREAD_PARENT_BUTTON_LABEL,
                style=discord.ButtonStyle.primary,
                custom_id=self._build_custom_id(match_id),
            )
        )

    @classmethod
    def bind_interaction_handler(
        cls,
        interaction_handler: MatchOperationThreadInteractionHandler,
    ) -> None:
        cls._registered_interaction_handler = interaction_handler

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
    ) -> MatchOperationThreadParentButton:
        del interaction, item
        return cls(match_id=int(match.group("match_id")))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        if self._interaction_handler is None:
            logger.error("Match operation thread interaction handler is not configured")
            await _send_fallback_error_message(interaction)
            return

        await self._interaction_handler.parent_from_match_operation_thread(
            interaction,
            self.match_id,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(match_id: int) -> str:
        return f"{MATCH_OPERATION_THREAD_PARENT_BUTTON_CUSTOM_ID_PREFIX}:{match_id}"


class MatchOperationThreadInitialView(discord.ui.View):
    def __init__(
        self,
        *,
        match_id: int,
        interaction_handler: MatchOperationThreadInteractionHandler | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            MatchOperationThreadVoidButton(
                match_id,
                interaction_handler=interaction_handler,
            )
        )


class MatchOperationThreadParentRecruitmentView(discord.ui.View):
    def __init__(
        self,
        *,
        match_id: int,
        interaction_handler: MatchOperationThreadInteractionHandler | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            MatchOperationThreadParentButton(
                match_id,
                interaction_handler=interaction_handler,
            )
        )


async def _send_fallback_error_message(interaction: discord.Interaction[Any]) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
                ephemeral=True,
            )
    except Exception:
        logger.exception("Failed to send match operation thread fallback response")


def create_match_operation_thread_initial_view(
    *,
    match_id: int,
    interaction_handler: MatchOperationThreadInteractionHandler | None = None,
) -> discord.ui.View:
    return MatchOperationThreadInitialView(
        match_id=match_id,
        interaction_handler=interaction_handler,
    )


def create_match_operation_thread_parent_recruitment_view(
    *,
    match_id: int,
    interaction_handler: MatchOperationThreadInteractionHandler | None = None,
) -> discord.ui.View:
    return MatchOperationThreadParentRecruitmentView(
        match_id=match_id,
        interaction_handler=interaction_handler,
    )


def register_match_operation_thread_dynamic_items(
    client: discord.Client,
    interaction_handler: MatchOperationThreadInteractionHandler,
) -> None:
    MatchOperationThreadParentButton.bind_interaction_handler(interaction_handler)
    MatchOperationThreadVoidButton.bind_interaction_handler(interaction_handler)
    client.add_dynamic_items(MatchOperationThreadParentButton)
    client.add_dynamic_items(MatchOperationThreadVoidButton)
