import logging

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.worker.daily import JobSettings, load_settings, run_daily_jobs
from dxd_rating.platform.db.models import Season


def test_load_settings_does_not_require_discord_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = load_settings()

    assert isinstance(settings, JobSettings)
    assert settings.database_url == "postgresql+psycopg://example"
    assert settings.log_level == "DEBUG"


def test_run_daily_jobs_runs_season_maintenance(
    session: Session,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    run_daily_jobs(session_factory)
    session.expire_all()
    seasons = session.scalars(select(Season).order_by(Season.start_at, Season.id)).all()

    assert "Database connectivity check succeeded" in caplog.text
    assert "Season maintenance completed" in caplog.text
    assert len(seasons) == 2
    assert seasons[0].end_at == seasons[1].start_at
