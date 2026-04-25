import asyncio
import importlib
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.bot.main import create_client, initialize_seasons, load_settings
from dxd_rating.platform.config.bot import BotSettings
from dxd_rating.platform.db.models import ManagedUiChannel, ManagedUiType, MatchFormat, Season
from dxd_rating.platform.discord.copy.info import (
    INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
    INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
    INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
    INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
    INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL,
    INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL,
)
from dxd_rating.platform.discord.copy.matchmaking import (
    MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL,
    MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL,
    MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
    MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
)
from dxd_rating.platform.discord.copy.registration import REGISTER_PANEL_BUTTON_LABEL
from dxd_rating.platform.discord.ui import (
    InfoThreadLeaderboardNextPageButton,
    InfoThreadLeaderboardSeasonNextPageButton,
    MatchmakingNewsMatchAnnouncementSpectateButton,
    MatchOperationThreadDrawButton,
    MatchOperationThreadLoseButton,
    MatchOperationThreadParentButton,
    MatchOperationThreadVoidButton,
    MatchOperationThreadWinButton,
    create_matchmaking_panel_view,
)

DEFAULT_MATCHMAKING_GUIDE_URL = (
    "https://github.com/linshokaku/dxd-rating-system/blob/main/docs/README.md"
)
DEFAULT_TERMS_URL = "https://github.com/linshokaku/dxd-rating-system/blob/main/docs/users/terms.md"
bot_main_module = importlib.import_module("dxd_rating.apps.bot.main")


def find_button_labels(client: discord.Client) -> list[list[str | None]]:
    return [
        [
            child.label if isinstance(child, discord.ui.Button) else None
            for child in persistent_view.children
        ]
        for persistent_view in client.persistent_views
    ]


def serialize_component_rows(
    components: list[discord.ActionRow],
) -> list[dict[str, object]]:
    return [cast(dict[str, object], component.to_dict()) for component in components]


def build_component_rows_from_view(view: discord.ui.View) -> list[discord.ActionRow]:
    return [discord.ActionRow(payload) for payload in view.to_components()]


class FakeFetchedMessage:
    def __init__(
        self,
        message_id: int,
        *,
        components: list[discord.ActionRow] | None = None,
    ) -> None:
        self.id = message_id
        self.components = [] if components is None else list(components)
        self.edit_calls: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> discord.Message:
        self.edit_calls.append(dict(kwargs))
        view = kwargs.get("view")
        if isinstance(view, discord.ui.View):
            self.components = build_component_rows_from_view(view)
        return cast(discord.Message, self)


class FakeFetchedChannel:
    def __init__(self, *messages: FakeFetchedMessage) -> None:
        self._messages = {message.id: message for message in messages}
        self.fetched_message_ids: list[int] = []

    async def fetch_message(self, message_id: int) -> discord.Message:
        self.fetched_message_ids.append(message_id)
        return cast(discord.Message, self._messages[message_id])


def test_initialize_seasons_creates_active_and_upcoming_seasons(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    session.expire_all()
    seasons = session.scalars(select(Season).order_by(Season.start_at, Season.id)).all()

    assert len(seasons) == 2
    assert seasons[0].end_at == seasons[1].start_at


def test_load_settings_reads_matchmaking_guide_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bot_settings_dir = tmp_path / "bot-settings"
    bot_settings_dir.mkdir()
    monkeypatch.chdir(bot_settings_dir)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("MATCHMAKING_GUIDE_URL", DEFAULT_MATCHMAKING_GUIDE_URL)
    monkeypatch.setenv("TERMS_URL", DEFAULT_TERMS_URL)

    settings = load_settings()

    assert isinstance(settings, BotSettings)
    assert settings.matchmaking_guide_url == DEFAULT_MATCHMAKING_GUIDE_URL
    assert settings.terms_url == DEFAULT_TERMS_URL


def test_load_settings_requires_matchmaking_guide_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bot_settings_dir = tmp_path / "bot-settings-missing-guide-url"
    bot_settings_dir.mkdir()
    monkeypatch.chdir(bot_settings_dir)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TERMS_URL", DEFAULT_TERMS_URL)
    monkeypatch.delenv("MATCHMAKING_GUIDE_URL", raising=False)

    with pytest.raises(
        SystemExit,
        match="Missing required environment variables: MATCHMAKING_GUIDE_URL",
    ):
        load_settings()


def test_load_settings_requires_terms_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bot_settings_dir = tmp_path / "bot-settings-missing-terms-url"
    bot_settings_dir.mkdir()
    monkeypatch.chdir(bot_settings_dir)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("MATCHMAKING_GUIDE_URL", DEFAULT_MATCHMAKING_GUIDE_URL)
    monkeypatch.delenv("TERMS_URL", raising=False)

    with pytest.raises(
        SystemExit,
        match="Missing required environment variables: TERMS_URL",
    ):
        load_settings()


def test_main_passes_development_mode_to_match_runtime_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        terms_url=DEFAULT_TERMS_URL,
        development_mode=True,
        super_admin_user_ids=frozenset({123}),
    )
    engine = Mock()
    session_factory = Mock()
    client = Mock()
    client.command_handlers = Mock()
    client.run = Mock()
    match_runtime = Mock()
    match_runtime_create = Mock(return_value=match_runtime)

    monkeypatch.setattr(bot_main_module, "load_settings", Mock(return_value=settings))
    monkeypatch.setattr(bot_main_module, "configure_logging", Mock())
    monkeypatch.setattr(bot_main_module, "create_db_engine", Mock(return_value=engine))
    monkeypatch.setattr(
        bot_main_module, "create_session_factory", Mock(return_value=session_factory)
    )
    monkeypatch.setattr(bot_main_module, "initialize_seasons", Mock())
    monkeypatch.setattr(bot_main_module, "create_client", Mock(return_value=client))
    monkeypatch.setattr(bot_main_module, "DiscordOutboxEventPublisher", Mock())
    monkeypatch.setattr(bot_main_module.MatchRuntime, "create", match_runtime_create)
    monkeypatch.setattr(bot_main_module, "OutboxDispatcher", Mock())
    monkeypatch.setattr(bot_main_module, "BotRuntime", Mock(return_value=Mock()))

    bot_main_module.main()

    match_runtime_create.assert_called_once_with(
        session_factory=session_factory,
        admin_discord_user_ids=settings.super_admin_user_ids,
        development_mode=True,
    )
    engine.dispose.assert_called_once_with()


def test_setup_hook_restores_persistent_register_panel_view(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        terms_url=DEFAULT_TERMS_URL,
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.REGISTER_PANEL,
            channel_id=1001,
            message_id=2001,
            created_by_discord_user_id=3001,
        )
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.INFO_CHANNEL,
            channel_id=1002,
            message_id=2002,
            created_by_discord_user_id=3002,
        )
    )
    session.commit()

    client = create_client(settings, session_factory)
    client.tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]

    asyncio.run(client.setup_hook())

    button_labels_by_view = find_button_labels(client)

    assert [
        MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
        MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
    ] in button_labels_by_view
    assert [REGISTER_PANEL_BUTTON_LABEL] in button_labels_by_view
    assert [
        INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
        INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
        INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
        INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
    ] in button_labels_by_view
    assert [INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL] in button_labels_by_view
    assert [None, INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL] in button_labels_by_view
    assert [None, INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL] in button_labels_by_view
    assert [None, None, INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL] in button_labels_by_view
    dynamic_item_classes = set(client._connection._view_store._dynamic_items.values())
    assert MatchOperationThreadWinButton in dynamic_item_classes
    assert MatchOperationThreadDrawButton in dynamic_item_classes
    assert MatchOperationThreadLoseButton in dynamic_item_classes
    assert MatchOperationThreadParentButton in dynamic_item_classes
    assert MatchOperationThreadVoidButton in dynamic_item_classes
    assert MatchmakingNewsMatchAnnouncementSpectateButton in dynamic_item_classes
    assert InfoThreadLeaderboardNextPageButton in dynamic_item_classes
    assert InfoThreadLeaderboardSeasonNextPageButton in dynamic_item_classes


def test_setup_hook_skips_managed_channels_without_persistent_view(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        terms_url=DEFAULT_TERMS_URL,
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.ADMIN_OPERATIONS_CHANNEL,
            channel_id=1002,
            message_id=2002,
            created_by_discord_user_id=3002,
        )
    )
    session.commit()

    client = create_client(settings, session_factory)
    client.tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]

    asyncio.run(client.setup_hook())

    button_labels_by_view = find_button_labels(client)

    assert [
        MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
        MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
    ] in button_labels_by_view
    assert [INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL] in button_labels_by_view
    assert [None, INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL] in button_labels_by_view
    assert [None, INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL] in button_labels_by_view
    assert [None, None, INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL] in button_labels_by_view
    dynamic_item_classes = set(client._connection._view_store._dynamic_items.values())
    assert MatchOperationThreadWinButton in dynamic_item_classes
    assert MatchOperationThreadDrawButton in dynamic_item_classes
    assert MatchOperationThreadLoseButton in dynamic_item_classes
    assert MatchOperationThreadParentButton in dynamic_item_classes
    assert MatchOperationThreadVoidButton in dynamic_item_classes
    assert MatchmakingNewsMatchAnnouncementSpectateButton in dynamic_item_classes
    assert InfoThreadLeaderboardNextPageButton in dynamic_item_classes
    assert InfoThreadLeaderboardSeasonNextPageButton in dynamic_item_classes


def test_setup_hook_restores_matchmaking_status_and_panel_views(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        terms_url=DEFAULT_TERMS_URL,
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
            channel_id=1003,
            status_message_id=2004,
            matchmaking_one_v_one_message_id=2005,
            matchmaking_two_v_two_message_id=2006,
            matchmaking_three_v_three_message_id=2007,
            created_by_discord_user_id=3003,
        )
    )
    session.commit()

    client = create_client(settings, session_factory)
    client.tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]

    asyncio.run(client.setup_hook())

    button_labels_by_view = find_button_labels(client)
    registered_message_ids = set(client._connection._view_store._synced_message_views)

    assert [MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL] in button_labels_by_view
    assert button_labels_by_view.count([None, MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL]) == 3
    assert registered_message_ids == {2004, 2005, 2006, 2007}


def test_setup_hook_skips_matchmaking_status_view_when_status_message_id_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        terms_url=DEFAULT_TERMS_URL,
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
            channel_id=1004,
            status_message_id=None,
            matchmaking_one_v_one_message_id=2008,
            matchmaking_two_v_two_message_id=2009,
            matchmaking_three_v_three_message_id=2010,
            created_by_discord_user_id=3004,
        )
    )
    session.commit()

    client = create_client(settings, session_factory)
    client.tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]

    asyncio.run(client.setup_hook())

    button_labels_by_view = find_button_labels(client)
    registered_message_ids = set(client._connection._view_store._synced_message_views)

    assert [MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL] not in button_labels_by_view
    assert button_labels_by_view.count([None, MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL]) == 3
    assert registered_message_ids == {2008, 2009, 2010}


def test_setup_hook_refreshes_outdated_matchmaking_panel_views(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        terms_url=DEFAULT_TERMS_URL,
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
            channel_id=1011,
            status_message_id=2011,
            matchmaking_one_v_one_message_id=2012,
            matchmaking_two_v_two_message_id=2013,
            matchmaking_three_v_three_message_id=2014,
            created_by_discord_user_id=3011,
        )
    )
    session.commit()

    client = create_client(settings, session_factory)
    client.tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]
    fetched_channel = FakeFetchedChannel(
        FakeFetchedMessage(2012),
        FakeFetchedMessage(2013),
        FakeFetchedMessage(2014),
    )
    client.fetch_channel = AsyncMock(return_value=fetched_channel)  # type: ignore[method-assign]

    asyncio.run(client.setup_hook())

    client.fetch_channel.assert_awaited_once_with(1011)  # type: ignore[attr-defined]
    assert fetched_channel.fetched_message_ids == [2012, 2013, 2014]

    expected_components_by_message_id = {
        2012: serialize_component_rows(
            build_component_rows_from_view(
                create_matchmaking_panel_view(
                    client.command_handlers,
                    MatchFormat.ONE_VS_ONE,
                )
            )
        ),
        2013: serialize_component_rows(
            build_component_rows_from_view(
                create_matchmaking_panel_view(
                    client.command_handlers,
                    MatchFormat.TWO_VS_TWO,
                )
            )
        ),
        2014: serialize_component_rows(
            build_component_rows_from_view(
                create_matchmaking_panel_view(
                    client.command_handlers,
                    MatchFormat.THREE_VS_THREE,
                )
            )
        ),
    }

    for message_id, expected_components in expected_components_by_message_id.items():
        message = fetched_channel._messages[message_id]
        assert len(message.edit_calls) == 1
        assert serialize_component_rows(message.components) == expected_components


def test_setup_hook_keeps_matchmaking_panel_views_when_already_current(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        terms_url=DEFAULT_TERMS_URL,
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
            channel_id=1012,
            status_message_id=2015,
            matchmaking_one_v_one_message_id=2016,
            matchmaking_two_v_two_message_id=2017,
            matchmaking_three_v_three_message_id=2018,
            created_by_discord_user_id=3012,
        )
    )
    session.commit()

    client = create_client(settings, session_factory)
    client.tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]
    fetched_channel = FakeFetchedChannel(
        FakeFetchedMessage(
            2016,
            components=build_component_rows_from_view(
                create_matchmaking_panel_view(
                    client.command_handlers,
                    MatchFormat.ONE_VS_ONE,
                )
            ),
        ),
        FakeFetchedMessage(
            2017,
            components=build_component_rows_from_view(
                create_matchmaking_panel_view(
                    client.command_handlers,
                    MatchFormat.TWO_VS_TWO,
                )
            ),
        ),
        FakeFetchedMessage(
            2018,
            components=build_component_rows_from_view(
                create_matchmaking_panel_view(
                    client.command_handlers,
                    MatchFormat.THREE_VS_THREE,
                )
            ),
        ),
    )
    client.fetch_channel = AsyncMock(return_value=fetched_channel)  # type: ignore[method-assign]

    asyncio.run(client.setup_hook())

    client.fetch_channel.assert_awaited_once_with(1012)  # type: ignore[attr-defined]
    assert fetched_channel.fetched_message_ids == [2016, 2017, 2018]
    assert all(
        len(fetched_channel._messages[message_id].edit_calls) == 0
        for message_id in (2016, 2017, 2018)
    )
