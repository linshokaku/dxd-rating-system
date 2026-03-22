from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.leaderboard.application import maintain_leaderboard_snapshots
from dxd_rating.contexts.seasons.application import (
    ensure_active_and_upcoming_seasons,
    get_database_now,
    update_ended_season_completions,
)
from dxd_rating.platform.config.common import configure_logging, raise_settings_load_error
from dxd_rating.platform.config.worker import WorkerSettings
from dxd_rating.platform.db.session import create_db_engine, create_session_factory, session_scope

logger = logging.getLogger(__name__)


JobSettings = WorkerSettings


DailyJobHandler = Callable[[Session], None]


@dataclass(frozen=True, slots=True)
class RegisteredDailyJob:
    name: str
    handler: DailyJobHandler


def load_settings() -> JobSettings:
    try:
        return JobSettings()
    except ValidationError as exc:
        raise_settings_load_error(exc)


def verify_database_connection(session: Session) -> None:
    session.execute(text("SELECT 1"))
    logger.info("Database connectivity check succeeded")


def maintain_seasons(session: Session) -> None:
    current_time = get_database_now(session)
    season_pair = ensure_active_and_upcoming_seasons(session, current_time=current_time)
    completed_season_ids = update_ended_season_completions(
        session,
        current_time=current_time,
    )
    logger.info(
        (
            "Season maintenance completed active_season_id=%s "
            "upcoming_season_id=%s completed_season_ids=%s"
        ),
        season_pair.active.id,
        season_pair.upcoming.id,
        list(completed_season_ids),
    )


def maintain_daily_leaderboard_snapshots(session: Session) -> None:
    result = maintain_leaderboard_snapshots(session)
    logger.info(
        (
            "Leaderboard snapshot maintenance completed snapshot_date=%s "
            "season_id=%s created_count=%s deleted_count=%s skipped_creation=%s"
        ),
        result.snapshot_date,
        result.season_id,
        result.created_count,
        result.deleted_count,
        result.skipped_creation,
    )


def registered_daily_jobs() -> tuple[RegisteredDailyJob, ...]:
    return (
        RegisteredDailyJob(
            name="database_healthcheck",
            handler=verify_database_connection,
        ),
        RegisteredDailyJob(
            name="season_maintenance",
            handler=maintain_seasons,
        ),
        RegisteredDailyJob(
            name="leaderboard_snapshot_maintenance",
            handler=maintain_daily_leaderboard_snapshots,
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
