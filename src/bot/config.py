from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_bot_token: str = Field(alias="DISCORD_BOT_TOKEN")
    database_url: str = Field(alias="DATABASE_URL")
