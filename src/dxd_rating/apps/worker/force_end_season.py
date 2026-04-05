from __future__ import annotations

import logging
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.seasons.application import ForceEndSeasonResult, force_end_active_season
from dxd_rating.platform.config.common import configure_logging, raise_settings_load_error
from dxd_rating.platform.config.worker import WorkerSettings
from dxd_rating.platform.db.session import create_db_engine, create_session_factory, session_scope

logger = logging.getLogger(__name__)


JobSettings = WorkerSettings


def load_settings() -> JobSettings:
    try:
        return JobSettings()
    except ValidationError as exc:
        raise_settings_load_error(exc)


def run_force_end_season(
    session_factory: sessionmaker[Session],
    *,
    current_time: datetime | None = None,
) -> ForceEndSeasonResult:
    with session_scope(session_factory) as session:
        result = force_end_active_season(session, current_time=current_time)

    logger.info(
        (
            "Season force-end completed active_season_id=%s upcoming_season_id=%s "
            "forced_at=%s previous_active_end_at=%s previous_upcoming_start_at=%s"
        ),
        result.active_season_id,
        result.upcoming_season_id,
        result.forced_at.isoformat(),
        result.previous_active_end_at.isoformat(),
        result.previous_upcoming_start_at.isoformat(),
    )
    return result


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    try:
        run_force_end_season(session_factory)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
