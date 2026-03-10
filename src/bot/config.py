from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_bot_token: str = Field(alias="DISCORD_BOT_TOKEN")
    database_url: str = Field(alias="DATABASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    super_admin_user_ids: frozenset[int] = Field(
        default_factory=frozenset, alias="SUPER_ADMIN_USER_IDS"
    )

    @field_validator("super_admin_user_ids", mode="before")
    @classmethod
    def parse_super_admin_user_ids(cls, value: Any) -> frozenset[int]:
        if value is None:
            return frozenset()
        if isinstance(value, str):
            if not value.strip():
                return frozenset()
            items = [item.strip() for item in value.split(",") if item.strip()]
            return frozenset(int(item) for item in items)
        if isinstance(value, int):
            return frozenset(int(item) for item in [value])
        if isinstance(value, (set, frozenset, list, tuple)):
            return frozenset(int(item) for item in value)
        raise ValueError("SUPER_ADMIN_USER_IDS must be a comma-separated list of numeric IDs")

    @field_validator("super_admin_user_ids")
    @classmethod
    def validate_super_admin_user_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if any(user_id < 0 for user_id in value):
            raise ValueError("SUPER_ADMIN_USER_IDS must contain only positive integers")
        return value
