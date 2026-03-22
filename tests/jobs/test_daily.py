import logging

import pytest
from sqlalchemy.orm import Session, sessionmaker

from jobs.daily import JobSettings, load_settings, run_daily_jobs


def test_load_settings_does_not_require_discord_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = load_settings()

    assert isinstance(settings, JobSettings)
    assert settings.database_url == "postgresql+psycopg://example"
    assert settings.log_level == "DEBUG"


def test_run_daily_jobs_logs_placeholder_message(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    run_daily_jobs(session_factory)

    assert "Database connectivity check succeeded" in caplog.text
    assert "No domain-specific daily jobs are registered yet" in caplog.text
