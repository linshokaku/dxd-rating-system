from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, Sequence

import discord

from dxd_rating.contexts.seasons.application import SeasonInfo
from dxd_rating.contexts.ui.application import InfoThreadCommandName
from dxd_rating.platform.db.models import MatchFormat
from dxd_rating.shared.constants import get_match_format_definitions

INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX = (
    "再度操作するには /info_thread を実行して新しい情報確認用スレッドを作成してください。"
)
INFO_THREAD_LEADERBOARD_MATCH_FORMAT_PLACEHOLDER = "試合形式を選択"
INFO_THREAD_LEADERBOARD_MATCH_FORMAT_SELECT_CUSTOM_ID = (
    "dxd_rating:info_thread:leaderboard:match_format"
)
INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL = "ランキングを表示"
INFO_THREAD_LEADERBOARD_SHOW_BUTTON_CUSTOM_ID = "dxd_rating:info_thread:leaderboard:show"
INFO_THREAD_LEADERBOARD_SELECT_MATCH_FORMAT_MESSAGE = (
    "試合形式を選択してください。"
    f"{INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX}"
)
INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_LABEL = "次のページ"
INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_CUSTOM_ID_PREFIX = (
    "dxd_rating:info_thread:leaderboard:next"
)
INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_TEMPLATE = (
    r"^dxd_rating:info_thread:leaderboard:next:(?P<match_format>[^:]+):(?P<target_page>\d+)$"
)
INFO_THREAD_LEADERBOARD_FALLBACK_ERROR_MESSAGE = (
    "ランキングの取得に失敗しました。管理者に確認してください。"
)
INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS = 25
INFO_THREAD_LEADERBOARD_SEASON_PLACEHOLDER = "シーズンを選択"
INFO_THREAD_LEADERBOARD_SEASON_SEASON_SELECT_CUSTOM_ID = (
    "dxd_rating:info_thread:leaderboard_season:season_id"
)
INFO_THREAD_LEADERBOARD_SEASON_MATCH_FORMAT_SELECT_CUSTOM_ID = (
    "dxd_rating:info_thread:leaderboard_season:match_format"
)
INFO_THREAD_LEADERBOARD_SEASON_SHOW_BUTTON_CUSTOM_ID = (
    "dxd_rating:info_thread:leaderboard_season:show"
)
INFO_THREAD_LEADERBOARD_SEASON_SELECT_SEASON_MESSAGE = (
    "シーズンを選択してください。"
    f"{INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX}"
)
INFO_THREAD_LEADERBOARD_SEASON_SELECT_BOTH_MESSAGE = (
    "シーズンと試合形式を選択してください。"
    f"{INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX}"
)
INFO_THREAD_LEADERBOARD_SEASON_NEXT_PAGE_BUTTON_CUSTOM_ID_PREFIX = (
    "dxd_rating:info_thread:leaderboard_season:next"
)
INFO_THREAD_LEADERBOARD_SEASON_NEXT_PAGE_BUTTON_TEMPLATE = (
    r"^dxd_rating:info_thread:leaderboard_season:next:"
    r"(?P<season_id>\d+):(?P<match_format>[^:]+):(?P<target_page>\d+)$"
)

logger = logging.getLogger(__name__)

INFO_THREAD_INITIAL_MESSAGES = {
    InfoThreadCommandName.PLAYER_INFO: "\n".join(
        [
            "このスレッドは現在シーズンのプレイヤー情報確認用です。",
            (
                "今後はこのスレッド内のボタンから /player_info "
                "と同等の操作を行えるようにする予定です。"
            ),
        ]
    ),
    InfoThreadCommandName.PLAYER_INFO_SEASON: "\n".join(
        [
            "このスレッドはシーズン別プレイヤー情報確認用です。",
            (
                "今後はこのスレッド内の season_id 選択と実行ボタンから "
                "/player_info_season と同等の操作を行えるようにする予定です。"
            ),
        ]
    ),
    InfoThreadCommandName.LEADERBOARD: "\n".join(
        [
            "このスレッドは現在シーズンのランキング確認用です。",
            "試合形式を選んで「ランキングを表示」を押してください。",
        ]
    ),
    InfoThreadCommandName.LEADERBOARD_SEASON: "\n".join(
        [
            "このスレッドはシーズン別ランキング確認用です。",
            "シーズンと試合形式を選んで「ランキングを表示」を押してください。",
        ]
    ),
}


class InfoThreadLeaderboardInteractionHandler(Protocol):
    async def leaderboard_from_info_thread(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
        page: int,
    ) -> None: ...

    async def leaderboard_season_from_info_thread(
        self,
        interaction: discord.Interaction[Any],
        season_id: int,
        match_format: str,
        page: int,
    ) -> None: ...


MessageComponentView = discord.ui.View | discord.ui.LayoutView


def _build_leaderboard_match_format_options() -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=definition.description, value=definition.match_format.value)
        for definition in get_match_format_definitions()
    ]


def _build_leaderboard_season_options(
    seasons: Sequence[SeasonInfo],
) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=season.name,
            value=str(season.season_id),
            description=f"season_id: {season.season_id}",
        )
        for season in seasons[:INFO_THREAD_LEADERBOARD_SEASON_MAX_OPTIONS]
    ]


async def _send_ephemeral_component_message(
    interaction: discord.Interaction[Any],
    message: str,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
        return

    await interaction.response.send_message(message, ephemeral=True)


async def _send_fallback_error_message(interaction: discord.Interaction[Any]) -> None:
    try:
        await _send_ephemeral_component_message(
            interaction,
            INFO_THREAD_LEADERBOARD_FALLBACK_ERROR_MESSAGE,
        )
    except Exception:
        logger.exception("Failed to send info thread leaderboard fallback response")


def _clone_message_component_item(
    item: discord.ui.Item[Any],
) -> discord.ui.Button[Any] | discord.ui.Select[Any]:
    source_item = getattr(item, "item", item)
    if isinstance(source_item, discord.ui.Button):
        return discord.ui.Button(
            style=source_item.style,
            label=source_item.label,
            disabled=source_item.disabled,
            custom_id=source_item.custom_id,
            url=source_item.url,
            emoji=source_item.emoji,
            row=source_item.row,
            sku_id=source_item.sku_id,
        )

    if isinstance(source_item, discord.ui.Select):
        custom_id = source_item.custom_id
        if custom_id is None:
            raise RuntimeError("Select component is missing custom_id")

        return discord.ui.Select(
            custom_id=custom_id,
            placeholder=source_item.placeholder,
            min_values=source_item.min_values,
            max_values=source_item.max_values,
            options=list(source_item.options),
            disabled=source_item.disabled,
            required=source_item.required,
            row=source_item.row,
        )

    raise RuntimeError(f"Unsupported info thread component item: {type(item)!r}")


def _clone_message_component_view(source_view: MessageComponentView) -> discord.ui.View:
    cloned_view = discord.ui.View(timeout=None)
    for child in source_view.children:
        cloned_view.add_item(_clone_message_component_item(child))
    return cloned_view


def _build_disabled_message_component_view(message: object) -> MessageComponentView:
    if isinstance(message, discord.Message):
        view = discord.ui.View.from_message(message, timeout=None)
    else:
        source_view = getattr(message, "view", None)
        if not isinstance(source_view, (discord.ui.View, discord.ui.LayoutView)):
            raise RuntimeError("Info thread interaction message does not have a view")
        view = _clone_message_component_view(source_view)

    for child in view.children:
        if hasattr(child, "disabled"):
            child.disabled = True

    return view


async def _disable_interaction_message_components(
    interaction: discord.Interaction[Any],
) -> None:
    message = getattr(interaction, "message", None)
    if message is None:
        raise RuntimeError("Info thread interaction is missing source message")

    await interaction.response.edit_message(
        view=_build_disabled_message_component_view(message)
    )


@dataclass(slots=True)
class LeaderboardSelectionState:
    match_format: str | None = None


@dataclass(slots=True)
class LeaderboardSeasonSelectionState:
    season_id: int | None = None
    match_format: str | None = None


class InfoThreadLeaderboardMatchFormatSelect(
    discord.ui.Select["InfoThreadLeaderboardInitialView"]
):
    def __init__(self) -> None:
        super().__init__(
            placeholder=INFO_THREAD_LEADERBOARD_MATCH_FORMAT_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_build_leaderboard_match_format_options(),
            custom_id=INFO_THREAD_LEADERBOARD_MATCH_FORMAT_SELECT_CUSTOM_ID,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Info thread leaderboard view is not attached")

        await view.select_match_format(interaction, self.values[0])


class InfoThreadLeaderboardShowButton(discord.ui.Button["InfoThreadLeaderboardInitialView"]):
    def __init__(self) -> None:
        super().__init__(
            label=INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id=INFO_THREAD_LEADERBOARD_SHOW_BUTTON_CUSTOM_ID,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Info thread leaderboard view is not attached")

        await view.show_leaderboard(interaction)


class InfoThreadLeaderboardInitialView(discord.ui.View):
    def __init__(
        self,
        interaction_handler: InfoThreadLeaderboardInteractionHandler,
    ) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler
        self._selection_state_by_key: dict[tuple[int, int], LeaderboardSelectionState] = {}
        self.match_format_select = InfoThreadLeaderboardMatchFormatSelect()
        self.show_button = InfoThreadLeaderboardShowButton()
        self.add_item(self.match_format_select)
        self.add_item(self.show_button)

    def _selection_key(self, interaction: discord.Interaction[Any]) -> tuple[int, int]:
        channel_id = interaction.channel_id if interaction.channel_id is not None else 0
        return interaction.user.id, channel_id

    async def select_match_format(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
    ) -> None:
        self._selection_state_by_key[self._selection_key(interaction)] = LeaderboardSelectionState(
            match_format=match_format
        )
        await interaction.response.defer()

    async def show_leaderboard(
        self,
        interaction: discord.Interaction[Any],
    ) -> None:
        await _disable_interaction_message_components(interaction)

        selection_state = self._selection_state_by_key.pop(
            self._selection_key(interaction),
            LeaderboardSelectionState(),
        )
        if selection_state.match_format is None:
            await _send_ephemeral_component_message(
                interaction,
                INFO_THREAD_LEADERBOARD_SELECT_MATCH_FORMAT_MESSAGE,
            )
            return

        await self._interaction_handler.leaderboard_from_info_thread(
            interaction,
            selection_state.match_format,
            1,
        )

    async def on_error(
        self,
        interaction: discord.Interaction[Any],
        error: Exception,
        _: discord.ui.Item[discord.ui.View],
    ) -> None:
        logger.exception("Info thread leaderboard interaction failed", exc_info=error)
        await _send_fallback_error_message(interaction)


class InfoThreadLeaderboardSeasonSelect(
    discord.ui.Select["InfoThreadLeaderboardSeasonInitialView"]
):
    def __init__(self, seasons: Sequence[SeasonInfo]) -> None:
        super().__init__(
            placeholder=INFO_THREAD_LEADERBOARD_SEASON_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_build_leaderboard_season_options(seasons),
            custom_id=INFO_THREAD_LEADERBOARD_SEASON_SEASON_SELECT_CUSTOM_ID,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Info thread leaderboard season view is not attached")

        await view.select_season(interaction, int(self.values[0]))


class InfoThreadLeaderboardSeasonMatchFormatSelect(
    discord.ui.Select["InfoThreadLeaderboardSeasonInitialView"]
):
    def __init__(self) -> None:
        super().__init__(
            placeholder=INFO_THREAD_LEADERBOARD_MATCH_FORMAT_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=_build_leaderboard_match_format_options(),
            custom_id=INFO_THREAD_LEADERBOARD_SEASON_MATCH_FORMAT_SELECT_CUSTOM_ID,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Info thread leaderboard season view is not attached")

        await view.select_match_format(interaction, self.values[0])


class InfoThreadLeaderboardSeasonShowButton(
    discord.ui.Button["InfoThreadLeaderboardSeasonInitialView"]
):
    def __init__(self) -> None:
        super().__init__(
            label=INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id=INFO_THREAD_LEADERBOARD_SEASON_SHOW_BUTTON_CUSTOM_ID,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is None:
            raise RuntimeError("Info thread leaderboard season view is not attached")

        await view.show_leaderboard(interaction)


class InfoThreadLeaderboardSeasonInitialView(discord.ui.View):
    def __init__(
        self,
        interaction_handler: InfoThreadLeaderboardInteractionHandler,
        seasons: Sequence[SeasonInfo],
    ) -> None:
        super().__init__(timeout=None)
        self._interaction_handler = interaction_handler
        self._selection_state_by_key: dict[tuple[int, int], LeaderboardSeasonSelectionState] = {}
        self.season_select = InfoThreadLeaderboardSeasonSelect(seasons)
        self.match_format_select = InfoThreadLeaderboardSeasonMatchFormatSelect()
        self.show_button = InfoThreadLeaderboardSeasonShowButton()
        self.add_item(self.season_select)
        self.add_item(self.match_format_select)
        self.add_item(self.show_button)

    def _selection_key(self, interaction: discord.Interaction[Any]) -> tuple[int, int]:
        channel_id = interaction.channel_id if interaction.channel_id is not None else 0
        return interaction.user.id, channel_id

    async def select_season(
        self,
        interaction: discord.Interaction[Any],
        season_id: int,
    ) -> None:
        key = self._selection_key(interaction)
        selection_state = self._selection_state_by_key.get(key, LeaderboardSeasonSelectionState())
        selection_state.season_id = season_id
        self._selection_state_by_key[key] = selection_state
        await interaction.response.defer()

    async def select_match_format(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
    ) -> None:
        key = self._selection_key(interaction)
        selection_state = self._selection_state_by_key.get(key, LeaderboardSeasonSelectionState())
        selection_state.match_format = match_format
        self._selection_state_by_key[key] = selection_state
        await interaction.response.defer()

    async def show_leaderboard(
        self,
        interaction: discord.Interaction[Any],
    ) -> None:
        await _disable_interaction_message_components(interaction)

        selection_state = self._selection_state_by_key.pop(
            self._selection_key(interaction),
            LeaderboardSeasonSelectionState(),
        )
        if selection_state.season_id is None and selection_state.match_format is None:
            await _send_ephemeral_component_message(
                interaction,
                INFO_THREAD_LEADERBOARD_SEASON_SELECT_BOTH_MESSAGE,
            )
            return

        if selection_state.season_id is None:
            await _send_ephemeral_component_message(
                interaction,
                INFO_THREAD_LEADERBOARD_SEASON_SELECT_SEASON_MESSAGE,
            )
            return

        if selection_state.match_format is None:
            await _send_ephemeral_component_message(
                interaction,
                INFO_THREAD_LEADERBOARD_SELECT_MATCH_FORMAT_MESSAGE,
            )
            return

        await self._interaction_handler.leaderboard_season_from_info_thread(
            interaction,
            selection_state.season_id,
            selection_state.match_format,
            1,
        )

    async def on_error(
        self,
        interaction: discord.Interaction[Any],
        error: Exception,
        _: discord.ui.Item[discord.ui.View],
    ) -> None:
        logger.exception("Info thread leaderboard season interaction failed", exc_info=error)
        await _send_fallback_error_message(interaction)


class InfoThreadLeaderboardNextPageButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_TEMPLATE,
):
    _registered_interaction_handler: ClassVar[
        InfoThreadLeaderboardInteractionHandler | None
    ] = None

    def __init__(
        self,
        match_format: MatchFormat | str,
        target_page: int,
        *,
        interaction_handler: InfoThreadLeaderboardInteractionHandler | None = None,
    ) -> None:
        resolved_match_format = (
            match_format if isinstance(match_format, MatchFormat) else MatchFormat(match_format)
        )
        self.match_format = resolved_match_format
        self.target_page = target_page
        self._interaction_handler = interaction_handler or self._registered_interaction_handler
        super().__init__(
            discord.ui.Button(
                label=INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_LABEL,
                style=discord.ButtonStyle.primary,
                custom_id=self._build_custom_id(
                    match_format=resolved_match_format,
                    target_page=target_page,
                ),
            )
        )

    @classmethod
    def bind_interaction_handler(
        cls,
        interaction_handler: InfoThreadLeaderboardInteractionHandler,
    ) -> None:
        cls._registered_interaction_handler = interaction_handler

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
    ) -> InfoThreadLeaderboardNextPageButton:
        del interaction, item
        return cls(
            match_format=MatchFormat(match.group("match_format")),
            target_page=int(match.group("target_page")),
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        if self._interaction_handler is None:
            logger.error("Info thread leaderboard interaction handler is not configured")
            await _send_fallback_error_message(interaction)
            return

        await _disable_interaction_message_components(interaction)
        await self._interaction_handler.leaderboard_from_info_thread(
            interaction,
            self.match_format.value,
            self.target_page,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(
        *,
        match_format: MatchFormat,
        target_page: int,
    ) -> str:
        return (
            f"{INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_CUSTOM_ID_PREFIX}:"
            f"{match_format.value}:{target_page}"
        )


class InfoThreadLeaderboardNextPageView(discord.ui.View):
    def __init__(
        self,
        match_format: MatchFormat | str,
        target_page: int,
        *,
        interaction_handler: InfoThreadLeaderboardInteractionHandler | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            InfoThreadLeaderboardNextPageButton(
                match_format=match_format,
                target_page=target_page,
                interaction_handler=interaction_handler,
            )
        )


class InfoThreadLeaderboardSeasonNextPageButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=INFO_THREAD_LEADERBOARD_SEASON_NEXT_PAGE_BUTTON_TEMPLATE,
):
    _registered_interaction_handler: ClassVar[
        InfoThreadLeaderboardInteractionHandler | None
    ] = None

    def __init__(
        self,
        season_id: int,
        match_format: MatchFormat | str,
        target_page: int,
        *,
        interaction_handler: InfoThreadLeaderboardInteractionHandler | None = None,
    ) -> None:
        resolved_match_format = (
            match_format if isinstance(match_format, MatchFormat) else MatchFormat(match_format)
        )
        self.season_id = season_id
        self.match_format = resolved_match_format
        self.target_page = target_page
        self._interaction_handler = interaction_handler or self._registered_interaction_handler
        super().__init__(
            discord.ui.Button(
                label=INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_LABEL,
                style=discord.ButtonStyle.primary,
                custom_id=self._build_custom_id(
                    season_id=season_id,
                    match_format=resolved_match_format,
                    target_page=target_page,
                ),
            )
        )

    @classmethod
    def bind_interaction_handler(
        cls,
        interaction_handler: InfoThreadLeaderboardInteractionHandler,
    ) -> None:
        cls._registered_interaction_handler = interaction_handler

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
    ) -> InfoThreadLeaderboardSeasonNextPageButton:
        del interaction, item
        return cls(
            season_id=int(match.group("season_id")),
            match_format=MatchFormat(match.group("match_format")),
            target_page=int(match.group("target_page")),
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        if self._interaction_handler is None:
            logger.error("Info thread leaderboard season interaction handler is not configured")
            await _send_fallback_error_message(interaction)
            return

        await _disable_interaction_message_components(interaction)
        await self._interaction_handler.leaderboard_season_from_info_thread(
            interaction,
            self.season_id,
            self.match_format.value,
            self.target_page,
        )

    @property
    def label(self) -> str | None:
        return self.item.label

    @staticmethod
    def _build_custom_id(
        *,
        season_id: int,
        match_format: MatchFormat,
        target_page: int,
    ) -> str:
        return (
            f"{INFO_THREAD_LEADERBOARD_SEASON_NEXT_PAGE_BUTTON_CUSTOM_ID_PREFIX}:"
            f"{season_id}:{match_format.value}:{target_page}"
        )


class InfoThreadLeaderboardSeasonNextPageView(discord.ui.View):
    def __init__(
        self,
        season_id: int,
        match_format: MatchFormat | str,
        target_page: int,
        *,
        interaction_handler: InfoThreadLeaderboardInteractionHandler | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            InfoThreadLeaderboardSeasonNextPageButton(
                season_id=season_id,
                match_format=match_format,
                target_page=target_page,
                interaction_handler=interaction_handler,
            )
        )


def build_info_thread_initial_message(command_name: InfoThreadCommandName) -> str:
    return INFO_THREAD_INITIAL_MESSAGES[command_name]


def create_info_thread_leaderboard_initial_view(
    interaction_handler: InfoThreadLeaderboardInteractionHandler,
) -> discord.ui.View:
    return InfoThreadLeaderboardInitialView(interaction_handler)


def create_info_thread_leaderboard_season_initial_view(
    interaction_handler: InfoThreadLeaderboardInteractionHandler,
    seasons: Sequence[SeasonInfo],
) -> discord.ui.View:
    return InfoThreadLeaderboardSeasonInitialView(interaction_handler, seasons)


def create_info_thread_leaderboard_next_page_view(
    *,
    match_format: MatchFormat | str,
    target_page: int,
    interaction_handler: InfoThreadLeaderboardInteractionHandler | None = None,
) -> discord.ui.View:
    return InfoThreadLeaderboardNextPageView(
        match_format=match_format,
        target_page=target_page,
        interaction_handler=interaction_handler,
    )


def create_info_thread_leaderboard_season_next_page_view(
    *,
    season_id: int,
    match_format: MatchFormat | str,
    target_page: int,
    interaction_handler: InfoThreadLeaderboardInteractionHandler | None = None,
) -> discord.ui.View:
    return InfoThreadLeaderboardSeasonNextPageView(
        season_id=season_id,
        match_format=match_format,
        target_page=target_page,
        interaction_handler=interaction_handler,
    )


def register_info_thread_dynamic_items(
    client: discord.Client,
    interaction_handler: InfoThreadLeaderboardInteractionHandler,
) -> None:
    InfoThreadLeaderboardNextPageButton.bind_interaction_handler(interaction_handler)
    InfoThreadLeaderboardSeasonNextPageButton.bind_interaction_handler(interaction_handler)
    client.add_dynamic_items(InfoThreadLeaderboardNextPageButton)
    client.add_dynamic_items(InfoThreadLeaderboardSeasonNextPageButton)
