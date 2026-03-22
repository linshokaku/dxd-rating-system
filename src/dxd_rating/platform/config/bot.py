from typing import Any

from pydantic import Field, field_validator

from dxd_rating.platform.config.common import (
    DatabaseSettings,
    parse_super_admin_user_ids,
    validate_super_admin_user_ids,
)


class BotSettings(DatabaseSettings):
    discord_bot_token: str = Field(alias="DISCORD_BOT_TOKEN")
    super_admin_user_ids: frozenset[int] = Field(
        default_factory=frozenset, alias="SUPER_ADMIN_USER_IDS"
    )

    @field_validator("super_admin_user_ids", mode="before")
    @classmethod
    def parse_ids(cls, value: Any) -> frozenset[int]:
        return parse_super_admin_user_ids(value)

    @field_validator("super_admin_user_ids")
    @classmethod
    def validate_ids(cls, value: frozenset[int]) -> frozenset[int]:
        return validate_super_admin_user_ids(value)


Settings = BotSettings
