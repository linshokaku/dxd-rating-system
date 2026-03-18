import logging

import discord
from discord import app_commands
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from bot.commands import BotCommandHandlers, register_app_commands
from bot.config import Settings
from bot.db.session import create_db_engine, create_session_factory
from bot.runtime import DiscordOutboxEventPublisher, MatchingQueueRuntime

logger = logging.getLogger(__name__)


class BotClient(discord.Client):
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        matching_queue_runtime: MatchingQueueRuntime | None = None,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.command_handlers = BotCommandHandlers(
            settings=settings,
            session_factory=session_factory,
            matching_queue_service=(
                matching_queue_runtime.service if matching_queue_runtime else None
            ),
            match_service=(
                matching_queue_runtime.match_service if matching_queue_runtime else None
            ),
            logger=logger,
        )
        register_app_commands(self.tree, self.command_handlers)
        self._matching_queue_runtime = matching_queue_runtime

    @property
    def matching_queue_runtime(self) -> MatchingQueueRuntime | None:
        return self._matching_queue_runtime

    @matching_queue_runtime.setter
    def matching_queue_runtime(self, runtime: MatchingQueueRuntime | None) -> None:
        self._matching_queue_runtime = runtime
        self.command_handlers.matching_queue_service = None if runtime is None else runtime.service
        self.command_handlers.match_service = None if runtime is None else runtime.match_service

    async def setup_hook(self) -> None:
        synced_commands = await self.tree.sync()
        logger.info(
            "Synced application commands: %s",
            [command.name for command in synced_commands],
        )

        if self.matching_queue_runtime is not None:
            await self.matching_queue_runtime.start()

    async def close(self) -> None:
        if self.matching_queue_runtime is not None:
            await self.matching_queue_runtime.stop()
        await super().close()

    async def on_ready(self) -> None:
        super_admin_user_ids = sorted(self.settings.super_admin_user_ids)
        logger.info("Super admin user IDs: %s", super_admin_user_ids)


def create_client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    matching_queue_runtime: MatchingQueueRuntime | None = None,
) -> BotClient:
    return BotClient(
        settings,
        session_factory,
        matching_queue_runtime=matching_queue_runtime,
    )


def configure_logging(log_level: str) -> None:
    level_name = log_level.upper()
    level = logging.getLevelName(level_name)
    if isinstance(level, str):
        level = logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing_fields = [
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors()
            if error["type"] == "missing"
        ]
        if missing_fields:
            fields = ", ".join(missing_fields)
            raise SystemExit(f"Missing required environment variables: {fields}") from exc
        raise SystemExit(f"Failed to load settings: {exc}") from exc


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    client = create_client(settings, session_factory)
    outbox_publisher = DiscordOutboxEventPublisher(
        client=client,
        session_factory=session_factory,
        super_admin_user_ids=settings.super_admin_user_ids,
    )
    matching_queue_runtime = MatchingQueueRuntime.create(
        session_factory=session_factory,
        outbox_publisher=outbox_publisher,
    )
    client.matching_queue_runtime = matching_queue_runtime

    try:
        client.run(settings.discord_bot_token)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
