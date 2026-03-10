import discord
from pydantic import ValidationError

from bot.config import Settings


def create_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client: discord.Client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        pass

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.content == "/neko":
            await message.channel.send("にゃーん")

    return client


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
    client = create_client()
    client.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
