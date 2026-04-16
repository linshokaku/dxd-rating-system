from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, Protocol

import discord

from dxd_rating.platform.discord.copy.match import (
    MATCH_OPERATION_THREAD_APPROVE_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_DRAW_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
    MATCH_OPERATION_THREAD_LOSE_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_PARENT_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_WIN_BUTTON_LABEL,
)

MATCH_OPERATION_THREAD_WIN_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:match_operation_thread:win"
MATCH_OPERATION_THREAD_WIN_BUTTON_TEMPLATE = (
    r"^dxd_rating:match_operation_thread:win:(?P<match_id>\d+)$"
)
MATCH_OPERATION_THREAD_DRAW_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:match_operation_thread:draw"
MATCH_OPERATION_THREAD_DRAW_BUTTON_TEMPLATE = (
    r"^dxd_rating:match_operation_thread:draw:(?P<match_id>\d+)$"
)
MATCH_OPERATION_THREAD_LOSE_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:match_operation_thread:lose"
MATCH_OPERATION_THREAD_LOSE_BUTTON_TEMPLATE = (
    r"^dxd_rating:match_operation_thread:lose:(?P<match_id>\d+)$"
)
MATCH_OPERATION_THREAD_VOID_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:match_operation_thread:void"
MATCH_OPERATION_THREAD_VOID_BUTTON_TEMPLATE = (
    r"^dxd_rating:match_operation_thread:void:(?P<match_id>\d+)$"
)
MATCH_OPERATION_THREAD_PARENT_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:match_operation_thread:parent"
MATCH_OPERATION_THREAD_PARENT_BUTTON_TEMPLATE = (
    r"^dxd_rating:match_operation_thread:parent:(?P<match_id>\d+)$"
)
MATCH_OPERATION_THREAD_APPROVE_BUTTON_CUSTOM_ID_PREFIX = "dxd_rating:match_operation_thread:approve"
MATCH_OPERATION_THREAD_APPROVE_BUTTON_TEMPLATE = (
    r"^dxd_rating:match_operation_thread:approve:(?P<match_id>\d+)$"
)

logger = logging.getLogger(__name__)


class _ComponentInteractionHandler(Protocol):
    async def run_component_interaction(
        self,
        interaction: discord.Interaction[Any],
        interaction_name: str,
        callback: Callable[[], Awaitable[None]],
        *,
        fallback_message: str,
    ) -> None: ...


class MatchOperationThreadInteractionHandler(_ComponentInteractionHandler, Protocol):
    async def win_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None: ...

    async def draw_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None: ...

    async def lose_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None: ...

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

    async def approve_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None: ...


class MatchOperationThreadWinButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=MATCH_OPERATION_THREAD_WIN_BUTTON_TEMPLATE,
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
                label=MATCH_OPERATION_THREAD_WIN_BUTTON_LABEL,
                style=discord.ButtonStyle.success,
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
    ) -> MatchOperationThreadWinButton:
        del interaction, item
        return cls(match_id=int(match.group("match_id")))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        interaction_handler = self._interaction_handler
        if interaction_handler is None:
            logger.error("Match operation thread interaction handler is not configured")
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _send_fallback_error_message(interaction)
            return

        await interaction_handler.run_component_interaction(
            interaction,
            "match_operation_thread:win",
            lambda: interaction_handler.win_from_match_operation_thread(
                interaction,
                self.match_id,
            ),
            fallback_message=MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(match_id: int) -> str:
        return f"{MATCH_OPERATION_THREAD_WIN_BUTTON_CUSTOM_ID_PREFIX}:{match_id}"


class MatchOperationThreadDrawButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=MATCH_OPERATION_THREAD_DRAW_BUTTON_TEMPLATE,
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
                label=MATCH_OPERATION_THREAD_DRAW_BUTTON_LABEL,
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
    ) -> MatchOperationThreadDrawButton:
        del interaction, item
        return cls(match_id=int(match.group("match_id")))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        interaction_handler = self._interaction_handler
        if interaction_handler is None:
            logger.error("Match operation thread interaction handler is not configured")
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _send_fallback_error_message(interaction)
            return

        await interaction_handler.run_component_interaction(
            interaction,
            "match_operation_thread:draw",
            lambda: interaction_handler.draw_from_match_operation_thread(
                interaction,
                self.match_id,
            ),
            fallback_message=MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(match_id: int) -> str:
        return f"{MATCH_OPERATION_THREAD_DRAW_BUTTON_CUSTOM_ID_PREFIX}:{match_id}"


class MatchOperationThreadLoseButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=MATCH_OPERATION_THREAD_LOSE_BUTTON_TEMPLATE,
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
                label=MATCH_OPERATION_THREAD_LOSE_BUTTON_LABEL,
                style=discord.ButtonStyle.secondary,
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
    ) -> MatchOperationThreadLoseButton:
        del interaction, item
        return cls(match_id=int(match.group("match_id")))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        interaction_handler = self._interaction_handler
        if interaction_handler is None:
            logger.error("Match operation thread interaction handler is not configured")
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _send_fallback_error_message(interaction)
            return

        await interaction_handler.run_component_interaction(
            interaction,
            "match_operation_thread:lose",
            lambda: interaction_handler.lose_from_match_operation_thread(
                interaction,
                self.match_id,
            ),
            fallback_message=MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(match_id: int) -> str:
        return f"{MATCH_OPERATION_THREAD_LOSE_BUTTON_CUSTOM_ID_PREFIX}:{match_id}"


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
        interaction_handler = self._interaction_handler
        if interaction_handler is None:
            logger.error("Match operation thread interaction handler is not configured")
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _send_fallback_error_message(interaction)
            return

        await interaction_handler.run_component_interaction(
            interaction,
            "match_operation_thread:void",
            lambda: interaction_handler.void_from_match_operation_thread(
                interaction,
                self.match_id,
            ),
            fallback_message=MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
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
        interaction_handler = self._interaction_handler
        if interaction_handler is None:
            logger.error("Match operation thread interaction handler is not configured")
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _send_fallback_error_message(interaction)
            return

        await interaction_handler.run_component_interaction(
            interaction,
            "match_operation_thread:parent",
            lambda: interaction_handler.parent_from_match_operation_thread(
                interaction,
                self.match_id,
            ),
            fallback_message=MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(match_id: int) -> str:
        return f"{MATCH_OPERATION_THREAD_PARENT_BUTTON_CUSTOM_ID_PREFIX}:{match_id}"


class MatchOperationThreadApproveButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=MATCH_OPERATION_THREAD_APPROVE_BUTTON_TEMPLATE,
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
                label=MATCH_OPERATION_THREAD_APPROVE_BUTTON_LABEL,
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
    ) -> MatchOperationThreadApproveButton:
        del interaction, item
        return cls(match_id=int(match.group("match_id")))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        interaction_handler = self._interaction_handler
        if interaction_handler is None:
            logger.error("Match operation thread interaction handler is not configured")
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _send_fallback_error_message(interaction)
            return

        await interaction_handler.run_component_interaction(
            interaction,
            "match_operation_thread:approve",
            lambda: interaction_handler.approve_from_match_operation_thread(
                interaction,
                self.match_id,
            ),
            fallback_message=MATCH_OPERATION_THREAD_FALLBACK_ERROR_MESSAGE,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(match_id: int) -> str:
        return f"{MATCH_OPERATION_THREAD_APPROVE_BUTTON_CUSTOM_ID_PREFIX}:{match_id}"


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


class MatchOperationThreadReportView(discord.ui.View):
    def __init__(
        self,
        *,
        match_id: int,
        interaction_handler: MatchOperationThreadInteractionHandler | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            MatchOperationThreadWinButton(
                match_id,
                interaction_handler=interaction_handler,
            )
        )
        self.add_item(
            MatchOperationThreadDrawButton(
                match_id,
                interaction_handler=interaction_handler,
            )
        )
        self.add_item(
            MatchOperationThreadLoseButton(
                match_id,
                interaction_handler=interaction_handler,
            )
        )
        self.add_item(
            MatchOperationThreadVoidButton(
                match_id,
                interaction_handler=interaction_handler,
            )
        )


class MatchOperationThreadApprovalView(discord.ui.View):
    def __init__(
        self,
        *,
        match_id: int,
        interaction_handler: MatchOperationThreadInteractionHandler | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            MatchOperationThreadApproveButton(
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


def create_match_operation_thread_report_view(
    *,
    match_id: int,
    interaction_handler: MatchOperationThreadInteractionHandler | None = None,
) -> discord.ui.View:
    return MatchOperationThreadReportView(
        match_id=match_id,
        interaction_handler=interaction_handler,
    )


def create_match_operation_thread_approval_view(
    *,
    match_id: int,
    interaction_handler: MatchOperationThreadInteractionHandler | None = None,
) -> discord.ui.View:
    return MatchOperationThreadApprovalView(
        match_id=match_id,
        interaction_handler=interaction_handler,
    )


def register_match_operation_thread_dynamic_items(
    client: discord.Client,
    interaction_handler: MatchOperationThreadInteractionHandler,
) -> None:
    MatchOperationThreadWinButton.bind_interaction_handler(interaction_handler)
    MatchOperationThreadDrawButton.bind_interaction_handler(interaction_handler)
    MatchOperationThreadLoseButton.bind_interaction_handler(interaction_handler)
    MatchOperationThreadParentButton.bind_interaction_handler(interaction_handler)
    MatchOperationThreadVoidButton.bind_interaction_handler(interaction_handler)
    MatchOperationThreadApproveButton.bind_interaction_handler(interaction_handler)
    client.add_dynamic_items(MatchOperationThreadWinButton)
    client.add_dynamic_items(MatchOperationThreadDrawButton)
    client.add_dynamic_items(MatchOperationThreadLoseButton)
    client.add_dynamic_items(MatchOperationThreadParentButton)
    client.add_dynamic_items(MatchOperationThreadVoidButton)
    client.add_dynamic_items(MatchOperationThreadApproveButton)
