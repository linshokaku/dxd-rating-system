import logging

import discord
from discord import app_commands
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from bot.commands import BotCommandHandlers, register_app_commands
from bot.config import Settings
from bot.db.session import create_db_engine, create_session_factory
from bot.notifications import DiscordOutboxEventPublisher
from bot.runtime import BotRuntime, MatchRuntime, OutboxDispatcher

logger = logging.getLogger(__name__)


def _match_runtime_for(runtime: BotRuntime | None) -> MatchRuntime | None:
    return None if runtime is None else runtime.match_runtime


class BotClient(discord.Client):
    def __init__(
        self,
        settings: Settings,
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


def create_client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    bot_runtime: BotRuntime | None = None,
) -> BotClient:
    return BotClient(
        settings,
        session_factory,
        bot_runtime=bot_runtime,
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
    )
    match_runtime = MatchRuntime.create(
        session_factory=session_factory,
        admin_discord_user_ids=settings.super_admin_user_ids,
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
