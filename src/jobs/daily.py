from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from bot.db.session import create_db_engine, create_session_factory, session_scope

logger = logging.getLogger(__name__)


class JobSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


DailyJobHandler = Callable[[Session], None]


@dataclass(frozen=True, slots=True)
class RegisteredDailyJob:
    name: str
    handler: DailyJobHandler


def configure_logging(log_level: str) -> None:
    level_name = log_level.upper()
    level = logging.getLevelName(level_name)
    if isinstance(level, str):
        level = logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")


def load_settings() -> JobSettings:
    try:
        return JobSettings()
    except ValidationError as exc:
        missing_fields = [
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors()
            if error["type"] == "missing"
        ]
        if missing_fields:
            fields = ", ".join(missing_fields)
            raise SystemExit(f"Missing required environment variables: {fields}") from exc
        raise SystemExit(f"Failed to load job settings: {exc}") from exc


def verify_database_connection(session: Session) -> None:
    session.execute(text("SELECT 1"))
    logger.info("Database connectivity check succeeded")


def log_placeholder_message(_: Session) -> None:
    logger.info("No domain-specific daily jobs are registered yet")


def registered_daily_jobs() -> tuple[RegisteredDailyJob, ...]:
    return (
        RegisteredDailyJob(
            name="database_healthcheck",
            handler=verify_database_connection,
        ),
        RegisteredDailyJob(
            name="daily_job_placeholder",
            handler=log_placeholder_message,
        ),
    )


def run_daily_jobs(session_factory: sessionmaker[Session]) -> None:
    jobs = registered_daily_jobs()
    logger.info("Starting daily cron job run with %d registered jobs", len(jobs))

    for job in jobs:
        logger.info("Running daily job: %s", job.name)
        with session_scope(session_factory) as session:
            job.handler(session)

    logger.info("Completed daily cron job run")


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    try:
        run_daily_jobs(session_factory)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
