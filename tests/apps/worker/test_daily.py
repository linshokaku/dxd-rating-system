import logging

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.worker.daily import JobSettings, load_settings, run_daily_jobs
from dxd_rating.contexts.players.application import register_player
from dxd_rating.contexts.seasons.application import ensure_active_and_upcoming_seasons
from dxd_rating.platform.db.models import (
    LeaderboardSnapshot,
    MatchFormat,
    PlayerFormatStats,
    Season,
)


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
    season_pair = ensure_active_and_upcoming_seasons(session)
    player = register_player(session=session, discord_user_id=123_456_789_012_345_678)
    three_vs_three_stats = session.scalar(
        select(PlayerFormatStats).where(
            PlayerFormatStats.player_id == player.id,
            PlayerFormatStats.season_id == season_pair.active.id,
            PlayerFormatStats.match_format == MatchFormat.THREE_VS_THREE,
        )
    )
    assert three_vs_three_stats is not None
    three_vs_three_stats.rating = 1620
    three_vs_three_stats.games_played = 4
    session.commit()

    run_daily_jobs(session_factory)
    session.expire_all()
    seasons = session.scalars(select(Season).order_by(Season.start_at, Season.id)).all()
    snapshots = session.scalars(
        select(LeaderboardSnapshot).order_by(
            LeaderboardSnapshot.snapshot_date,
            LeaderboardSnapshot.match_format,
            LeaderboardSnapshot.rank,
        )
    ).all()

    assert "Database connectivity check succeeded" in caplog.text
    assert "Season maintenance completed" in caplog.text
    assert "Leaderboard snapshot maintenance completed" in caplog.text
    assert len(seasons) == 2
    assert seasons[0].end_at == seasons[1].start_at
    assert len(snapshots) == 1
    assert snapshots[0].player_id == player.id
    assert snapshots[0].match_format == MatchFormat.THREE_VS_THREE
    assert snapshots[0].rank == 1
