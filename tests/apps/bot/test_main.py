import asyncio
from unittest.mock import AsyncMock

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.bot.main import create_client, initialize_seasons
from dxd_rating.platform.config.bot import BotSettings
from dxd_rating.platform.db.models import ManagedUiChannel, ManagedUiType, Season
from dxd_rating.platform.discord.ui import (
    INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL,
    INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL,
    INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL,
    INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL,
    INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL,
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


def test_setup_hook_restores_persistent_register_panel_view(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
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
        development_mode=False,
        super_admin_user_ids=frozenset(),
    )
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.ADMIN_CONTACT_CHANNEL,
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
