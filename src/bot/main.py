import logging

import discord
from pydantic import ValidationError

from bot.config import Settings
from bot.db.session import create_db_engine, create_session_factory
from bot.runtime import MatchingQueueRuntime

logger = logging.getLogger(__name__)


class BotClient(discord.Client):
    def __init__(
        self,
        settings: Settings,
        *,
        matching_queue_runtime: MatchingQueueRuntime | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.matching_queue_runtime = matching_queue_runtime

    async def setup_hook(self) -> None:
        if self.matching_queue_runtime is not None:
            await self.matching_queue_runtime.start()

    async def close(self) -> None:
        if self.matching_queue_runtime is not None:
            await self.matching_queue_runtime.stop()
        await super().close()

    async def on_ready(self) -> None:
        super_admin_user_ids = sorted(self.settings.super_admin_user_ids)
        logger.info("Super admin user IDs: %s", super_admin_user_ids)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.content == "/dev_is_admin":
            try:
                reply = "はい" if is_super_admin(message.author.id, self.settings) else "いいえ"
                await message.channel.send(reply)
            except Exception:
                logger.exception("Failed to execute /dev_is_admin command")
                await message.channel.send("エラーが発生しました。管理者に確認してください。")
            return
        if message.content == "/neko":
            await message.channel.send("にゃーん")


def is_super_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.super_admin_user_ids


def create_client(
    settings: Settings,
    *,
    matching_queue_runtime: MatchingQueueRuntime | None = None,
) -> BotClient:
    return BotClient(settings, matching_queue_runtime=matching_queue_runtime)


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
    matching_queue_runtime = MatchingQueueRuntime.create(session_factory=session_factory)
    client = create_client(settings, matching_queue_runtime=matching_queue_runtime)

    try:
        client.run(settings.discord_bot_token)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
