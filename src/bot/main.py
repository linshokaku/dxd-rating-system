import logging

import discord
from pydantic import ValidationError

from bot.config import Settings

logger = logging.getLogger(__name__)


def is_super_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.super_admin_user_ids


def create_client(settings: Settings) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client: discord.Client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        super_admin_user_ids = sorted(settings.super_admin_user_ids)
        logger.info("Super admin user IDs: %s", super_admin_user_ids)

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.content == "/dev_is_admin":
            try:
                reply = "はい" if is_super_admin(message.author.id, settings) else "いいえ"
                await message.channel.send(reply)
            except Exception:
                logger.exception("Failed to execute /dev_is_admin command")
                await message.channel.send("エラーが発生しました。管理者に確認してください。")
            return
        if message.content == "/neko":
            await message.channel.send("にゃーん")

    return client


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
    client = create_client(settings)
    client.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
