from __future__ import annotations

import logging
from typing import Any, NoReturn

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RAILWAY_DATABASE_URL_PREFIX = "postgresql://"
DATABASE_URL_FORMAT_ERROR = (
    "DATABASE_URL must start with 'postgresql://' to match Railway configuration"
)


def validate_database_url(value: str) -> str:
    if not value.startswith(RAILWAY_DATABASE_URL_PREFIX):
        raise ValueError(DATABASE_URL_FORMAT_ERROR)
    return value


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("database_url")
    @classmethod
    def validate_database_url_value(cls, value: str) -> str:
        return validate_database_url(value)


def configure_logging(log_level: str) -> None:
    level_name = log_level.upper()
    level = logging.getLevelName(level_name)
    if isinstance(level, str):
        level = logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")


def raise_settings_load_error(exc: ValidationError) -> NoReturn:
    missing_fields = [
        ".".join(str(part) for part in error["loc"])
        for error in exc.errors()
        if error["type"] == "missing"
    ]
    if missing_fields:
        fields = ", ".join(missing_fields)
        raise SystemExit(f"Missing required environment variables: {fields}") from exc
    raise SystemExit(f"Failed to load settings: {exc}") from exc


def parse_super_admin_user_ids(value: Any) -> frozenset[int]:
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


def validate_super_admin_user_ids(value: frozenset[int]) -> frozenset[int]:
    if any(user_id < 0 for user_id in value):
        raise ValueError("SUPER_ADMIN_USER_IDS must contain only positive integers")
    return value
