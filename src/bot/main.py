import discord

from bot.config import Settings

settings = Settings()

intents = discord.Intents.default()
intents.message_content = True
client: discord.Client = discord.Client(intents=intents)


@client.event
async def on_ready() -> None:
    print("ログインしました")


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if message.content == "/neko":
        await message.channel.send("にゃーん")


client.run(settings.discord_bot_token)
