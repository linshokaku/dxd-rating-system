import asyncio
from unittest.mock import AsyncMock

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.bot.main import create_client, initialize_seasons
from dxd_rating.platform.config.bot import BotSettings
from dxd_rating.platform.db.models import ManagedUiChannel, ManagedUiType, Season
from dxd_rating.platform.discord.ui import REGISTER_PANEL_BUTTON_LABEL


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
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
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
    session.commit()

    client = create_client(settings, session_factory)
    client.tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]

    asyncio.run(client.setup_hook())

    persistent_views = client.persistent_views

    assert len(persistent_views) == 1
    button = persistent_views[0].children[0]
    assert isinstance(button, discord.ui.Button)
    assert button.label == REGISTER_PANEL_BUTTON_LABEL


def test_setup_hook_skips_managed_channels_without_persistent_view(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    settings = BotSettings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
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

    assert client.persistent_views == []
