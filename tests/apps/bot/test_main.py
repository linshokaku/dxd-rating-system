import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.bot.main import create_client, initialize_seasons, load_settings
from dxd_rating.platform.config.bot import BotSettings
from dxd_rating.platform.db.models import ManagedUiChannel, ManagedUiType, Season
from dxd_rating.platform.discord.ui import (
    INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
    INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
    INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
    INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
    INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL,
    INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL,
    MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL,
    MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL,
    MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
    MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
    REGISTER_PANEL_BUTTON_LABEL,
    InfoThreadLeaderboardNextPageButton,
    InfoThreadLeaderboardSeasonNextPageButton,
    MatchmakingNewsMatchAnnouncementSpectateButton,
    MatchOperationThreadDrawButton,
    MatchOperationThreadLoseButton,
    MatchOperationThreadParentButton,
    MatchOperationThreadVoidButton,
    MatchOperationThreadWinButton,
)

DEFAULT_MATCHMAKING_GUIDE_URL = (
    "https://github.com/linshokaku/dxd-rating-system/blob/main/docs/README.md"
)


def find_button_labels(client: discord.Client) -> list[list[str | None]]:
    return [
        [
            child.label if isinstance(child, discord.ui.Button) else None
            for child in persistent_view.children
        ]
        for persistent_view in client.persistent_views
    ]


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
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example")
    monkeypatch.setenv("MATCHMAKING_GUIDE_URL", DEFAULT_MATCHMAKING_GUIDE_URL)

    settings = load_settings()

    assert isinstance(settings, BotSettings)
    assert settings.matchmaking_guide_url == DEFAULT_MATCHMAKING_GUIDE_URL


def test_load_settings_requires_matchmaking_guide_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bot_settings_dir = tmp_path / "bot-settings-missing-guide-url"
    bot_settings_dir.mkdir()
    monkeypatch.chdir(bot_settings_dir)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example")
    monkeypatch.delenv("MATCHMAKING_GUIDE_URL", raising=False)

    with pytest.raises(
        SystemExit,
        match="Missing required environment variables: MATCHMAKING_GUIDE_URL",
    ):
        load_settings()


def test_setup_hook_restores_persistent_register_panel_view(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
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
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
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
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
            channel_id=1003,
            message_id=2003,
            status_message_id=2004,
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
    assert [None, None, MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL] in button_labels_by_view
    assert registered_message_ids == {2003, 2004}


def test_setup_hook_skips_matchmaking_status_view_when_status_message_id_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        matchmaking_guide_url=DEFAULT_MATCHMAKING_GUIDE_URL,
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
            channel_id=1004,
            message_id=2005,
            status_message_id=None,
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
    assert [None, None, MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL] in button_labels_by_view
    assert registered_message_ids == {2005}
