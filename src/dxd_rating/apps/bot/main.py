import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import discord
from discord import app_commands
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.seasons.application import ensure_active_and_upcoming_seasons
from dxd_rating.platform.config.bot import BotSettings
from dxd_rating.platform.config.common import configure_logging, raise_settings_load_error
from dxd_rating.platform.db.models import ManagedUiChannel, ManagedUiType, MatchFormat
from dxd_rating.platform.db.session import create_db_engine, create_session_factory, session_scope
from dxd_rating.platform.discord.copy.matchmaking import build_matchmaking_panel_message
from dxd_rating.platform.discord.gateway.commands import BotCommandHandlers, register_app_commands
from dxd_rating.platform.discord.message_embeds import (
    build_body_only_public_message_edit_kwargs,
)
from dxd_rating.platform.discord.rest import DiscordOutboxEventPublisher
from dxd_rating.platform.discord.ui import (
    create_info_thread_leaderboard_initial_view,
    create_info_thread_leaderboard_season_initial_view,
    create_info_thread_player_info_initial_view,
    create_info_thread_player_info_season_initial_view,
    create_managed_ui_view,
    create_matchmaking_panel_view,
    create_matchmaking_presence_thread_view,
    create_matchmaking_status_view,
    has_persistent_managed_ui_view,
    register_info_thread_dynamic_items,
    register_match_operation_thread_dynamic_items,
    register_matchmaking_news_match_announcement_dynamic_items,
)
from dxd_rating.platform.runtime import BotRuntime, MatchRuntime, OutboxDispatcher
from dxd_rating.shared.constants import get_match_format_definitions

logger = logging.getLogger(__name__)


def _match_runtime_for(runtime: BotRuntime | None) -> MatchRuntime | None:
    return None if runtime is None else runtime.match_runtime


def _serialize_message_components(message: object) -> list[dict[str, Any]] | None:
    components = getattr(message, "components", None)
    if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
        return None

    serialized_components: list[dict[str, Any]] = []
    for component in components:
        to_dict = getattr(component, "to_dict", None)
        if not callable(to_dict):
            return None

        payload = to_dict()
        if not isinstance(payload, dict):
            return None

        serialized_components.append(payload)

    return serialized_components


def _message_components_match_view(message: object, view: discord.ui.View) -> bool:
    serialized_components = _serialize_message_components(message)
    if serialized_components is None:
        return False

    return serialized_components == view.to_components()


class BotClient(discord.Client):
    def __init__(
        self,
        settings: BotSettings,
        session_factory: sessionmaker[Session],
        *,
        bot_runtime: BotRuntime | None = None,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.command_handlers = BotCommandHandlers(
            settings=settings,
            session_factory=session_factory,
            matching_queue_service=_match_runtime_for(bot_runtime),
            match_service=_match_runtime_for(bot_runtime),
            logger=logger,
        )
        register_app_commands(self.tree, self.command_handlers)
        self._persistent_views_registered = False
        self._bot_runtime = bot_runtime

    @property
    def bot_runtime(self) -> BotRuntime | None:
        return self._bot_runtime

    @bot_runtime.setter
    def bot_runtime(self, runtime: BotRuntime | None) -> None:
        self._bot_runtime = runtime
        self.command_handlers.matching_queue_service = _match_runtime_for(runtime)
        self.command_handlers.match_service = _match_runtime_for(runtime)

    async def setup_hook(self) -> None:
        await self._register_persistent_views()
        synced_commands = await self.tree.sync()
        logger.info(
            "Synced application commands: %s",
            [command.name for command in synced_commands],
        )

        if self.bot_runtime is not None:
            await self.bot_runtime.start()

    async def close(self) -> None:
        if self.bot_runtime is not None:
            await self.bot_runtime.stop()
        await super().close()

    async def on_ready(self) -> None:
        super_admin_user_ids = sorted(self.settings.super_admin_user_ids)
        logger.info("Super admin user IDs: %s", super_admin_user_ids)

    async def _register_persistent_views(self) -> None:
        if self._persistent_views_registered:
            return

        register_matchmaking_news_match_announcement_dynamic_items(
            self,
            self.command_handlers,
        )
        register_match_operation_thread_dynamic_items(
            self,
            self.command_handlers,
        )
        register_info_thread_dynamic_items(
            self,
            self.command_handlers,
        )
        self.add_view(create_matchmaking_presence_thread_view(self.command_handlers))
        self.add_view(create_info_thread_player_info_initial_view(self.command_handlers))
        self.add_view(create_info_thread_leaderboard_initial_view(self.command_handlers))
        season_views = await asyncio.to_thread(
            self.command_handlers.list_started_seasons_for_info_thread
        )
        self.add_view(
            create_info_thread_player_info_season_initial_view(
                self.command_handlers,
                season_views,
            )
        )
        self.add_view(
            create_info_thread_leaderboard_season_initial_view(
                self.command_handlers,
                season_views,
            )
        )

        managed_ui_channels = await asyncio.to_thread(
            self.command_handlers.managed_ui_service.list_managed_ui_channels
        )
        registered_message_ids: list[int] = []
        for managed_ui_channel in managed_ui_channels:
            if not has_persistent_managed_ui_view(managed_ui_channel.ui_type):
                continue
            if managed_ui_channel.ui_type is ManagedUiType.MATCHMAKING_CHANNEL:
                if managed_ui_channel.status_message_id is not None:
                    self.add_view(
                        create_matchmaking_status_view(self.command_handlers),
                        message_id=managed_ui_channel.status_message_id,
                    )
                    registered_message_ids.append(managed_ui_channel.status_message_id)
                panel_views_by_format: dict[MatchFormat, discord.ui.View] = {}
                for definition in get_match_format_definitions():
                    panel_message_id = managed_ui_channel.get_matchmaking_panel_message_id(
                        definition.match_format
                    )
                    if panel_message_id is None:
                        continue
                    panel_view = create_matchmaking_panel_view(
                        self.command_handlers,
                        definition.match_format,
                    )
                    self.add_view(
                        panel_view,
                        message_id=panel_message_id,
                    )
                    panel_views_by_format[definition.match_format] = panel_view
                    registered_message_ids.append(panel_message_id)
                await self._synchronize_matchmaking_panel_views(
                    managed_ui_channel,
                    panel_views_by_format,
                )
                continue

            if managed_ui_channel.message_id is None:
                continue

            self.add_view(
                create_managed_ui_view(
                    managed_ui_channel.ui_type,
                    self.command_handlers,
                ),
                message_id=managed_ui_channel.message_id,
            )
            registered_message_ids.append(managed_ui_channel.message_id)

        self._persistent_views_registered = True
        logger.info(
            "Registered persistent managed UI views count=%s message_ids=%s",
            len(registered_message_ids),
            registered_message_ids,
        )

    async def _synchronize_matchmaking_panel_views(
        self,
        managed_ui_channel: ManagedUiChannel,
        panel_views_by_format: Mapping[MatchFormat, discord.ui.View],
    ) -> None:
        if not panel_views_by_format:
            return

        try:
            channel = await self.fetch_channel(managed_ui_channel.channel_id)
        except Exception as exc:
            logger.warning(
                "Failed to fetch matchmaking managed UI channel for startup sync channel_id=%s",
                managed_ui_channel.channel_id,
                exc_info=exc,
            )
            return

        fetch_message = getattr(channel, "fetch_message", None)
        if not callable(fetch_message):
            logger.warning(
                "Skipping matchmaking panel startup sync because channel does not "
                "support message fetch channel_id=%s",
                managed_ui_channel.channel_id,
            )
            return

        synchronized_message_ids: list[int] = []
        for definition in get_match_format_definitions():
            panel_message_id = managed_ui_channel.get_matchmaking_panel_message_id(
                definition.match_format
            )
            if panel_message_id is None:
                continue

            panel_view = panel_views_by_format.get(definition.match_format)
            if panel_view is None:
                continue

            try:
                message = await fetch_message(panel_message_id)
            except Exception as exc:
                logger.warning(
                    "Failed to fetch matchmaking panel message for startup sync "
                    "channel_id=%s message_id=%s match_format=%s",
                    managed_ui_channel.channel_id,
                    panel_message_id,
                    definition.match_format.value,
                    exc_info=exc,
                )
                continue

            if _message_components_match_view(message, panel_view):
                continue

            edit = getattr(message, "edit", None)
            if not callable(edit):
                logger.warning(
                    "Skipping matchmaking panel startup sync because message is not "
                    "editable channel_id=%s message_id=%s match_format=%s",
                    managed_ui_channel.channel_id,
                    panel_message_id,
                    definition.match_format.value,
                )
                continue

            try:
                await edit(
                    **build_body_only_public_message_edit_kwargs(
                        build_matchmaking_panel_message(definition.match_format)
                    ),
                    view=panel_view,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to refresh matchmaking panel message for startup sync "
                    "channel_id=%s message_id=%s match_format=%s",
                    managed_ui_channel.channel_id,
                    panel_message_id,
                    definition.match_format.value,
                    exc_info=exc,
                )
                continue

            synchronized_message_ids.append(panel_message_id)

        if synchronized_message_ids:
            logger.info(
                "Refreshed matchmaking panel views on startup channel_id=%s message_ids=%s",
                managed_ui_channel.channel_id,
                synchronized_message_ids,
            )


def create_client(
    settings: BotSettings,
    session_factory: sessionmaker[Session],
    *,
    bot_runtime: BotRuntime | None = None,
) -> BotClient:
    return BotClient(
        settings,
        session_factory,
        bot_runtime=bot_runtime,
    )


def load_settings() -> BotSettings:
    try:
        return BotSettings()
    except ValidationError as exc:
        raise_settings_load_error(exc)


def initialize_seasons(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        season_pair = ensure_active_and_upcoming_seasons(session)
        logger.info(
            "Prepared seasons on bot startup active_season_id=%s upcoming_season_id=%s",
            season_pair.active.id,
            season_pair.upcoming.id,
        )


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    initialize_seasons(session_factory)
    client = create_client(settings, session_factory)
    outbox_publisher = DiscordOutboxEventPublisher(
        client=client,
        admin_discord_user_ids=settings.super_admin_user_ids,
        match_operation_thread_interaction_handler=client.command_handlers,
        matchmaking_news_match_announcement_interaction_handler=client.command_handlers,
        matchmaking_presence_interaction_handler=client.command_handlers,
    )
    match_runtime = MatchRuntime.create(
        session_factory=session_factory,
        admin_discord_user_ids=settings.super_admin_user_ids,
        development_mode=settings.development_mode,
    )
    outbox_dispatcher = OutboxDispatcher(
        session_factory=session_factory,
        publisher=outbox_publisher,
    )
    bot_runtime = BotRuntime(
        match_runtime=match_runtime,
        outbox_dispatcher=outbox_dispatcher,
    )
    client.bot_runtime = bot_runtime

    try:
        client.run(settings.discord_bot_token)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
