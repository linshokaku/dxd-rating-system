from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.worker.force_end_season import JobSettings, load_settings, run_force_end_season
from dxd_rating.contexts.seasons.application import ensure_active_and_upcoming_seasons
from dxd_rating.platform.db.models import Season


def test_load_settings_does_not_require_discord_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = load_settings()

    assert isinstance(settings, JobSettings)
    assert settings.database_url == "postgresql://example"
    assert settings.log_level == "DEBUG"


def test_run_force_end_season_updates_active_and_upcoming_boundaries(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    current_time = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    season_pair = ensure_active_and_upcoming_seasons(
        session,
        current_time=current_time,
    )
    previous_active_end_at = season_pair.active.end_at
    previous_upcoming_start_at = season_pair.upcoming.start_at
    session.commit()

    result = run_force_end_season(session_factory, current_time=current_time)

    session.expire_all()
    active_season = session.get(Season, season_pair.active.id)
    upcoming_season = session.get(Season, season_pair.upcoming.id)

    assert active_season is not None
    assert upcoming_season is not None
    assert result.active_season_id == season_pair.active.id
    assert result.upcoming_season_id == season_pair.upcoming.id
    assert result.previous_active_end_at == previous_active_end_at
    assert result.previous_upcoming_start_at == previous_upcoming_start_at
    assert active_season.end_at == result.forced_at
    assert upcoming_season.start_at == result.forced_at
    assert active_season.end_at < previous_active_end_at
    assert upcoming_season.start_at < previous_upcoming_start_at
    assert upcoming_season.end_at == datetime(2026, 5, 13, 15, 0, 0, tzinfo=timezone.utc)
