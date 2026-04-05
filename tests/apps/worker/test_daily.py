import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.worker.daily import JobSettings, load_settings, run_daily_jobs
from dxd_rating.contexts.players.application import register_player
from dxd_rating.contexts.seasons.application import ensure_active_and_upcoming_seasons
from dxd_rating.platform.db.models import (
    LeaderboardSnapshot,
    ManagedUiChannel,
    ManagedUiType,
    MatchFormat,
    OutboxEvent,
    OutboxEventType,
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
    started_at = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.ADMIN_OPERATIONS_CHANNEL,
            channel_id=900_001,
            message_id=900_101,
            created_by_discord_user_id=900_201,
        )
    )
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

    run_daily_jobs(session_factory, started_at=started_at)
    session.expire_all()
    seasons = session.scalars(select(Season).order_by(Season.start_at, Season.id)).all()
    outbox_events = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()
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
    assert "Processed daily worker startup notification enqueue" in caplog.text
    assert len(seasons) == 2
    assert seasons[0].end_at == seasons[1].start_at
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == OutboxEventType.ADMIN_OPERATIONS_NOTIFICATION
    assert outbox_events[0].dedupe_key == (
        "admin_operations_notification:daily_worker_started:daily_worker:2026-04-05T00:00:00+00:00"
    )
    assert outbox_events[0].payload == {
        "notification_kind": "daily_worker_started",
        "worker_name": "daily_worker",
        "occurred_at": "2026-04-05T00:00:00+00:00",
        "destination": {
            "kind": "channel",
            "channel_id": 900_001,
            "guild_id": None,
        },
    }
    assert len(snapshots) == 1
    assert snapshots[0].player_id == player.id
    assert snapshots[0].match_format == MatchFormat.THREE_VS_THREE
    assert snapshots[0].rank == 1


def test_run_daily_jobs_skips_startup_notification_when_admin_operations_channel_is_missing(
    session: Session,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    run_daily_jobs(
        session_factory,
        started_at=datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc),
    )

    session.expire_all()
    seasons = session.scalars(select(Season).order_by(Season.start_at, Season.id)).all()
    outbox_events = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()

    assert (
        "Skipping daily worker startup notification because "
        "admin_operations_channel is not configured" in caplog.text
    )
    assert "Database connectivity check succeeded" in caplog.text
    assert "Season maintenance completed" in caplog.text
    assert "Leaderboard snapshot maintenance completed" in caplog.text
    assert len(seasons) == 2
    assert outbox_events == []


def test_run_daily_jobs_enqueues_season_completed_notification_for_past_season(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL,
            channel_id=901_001,
            message_id=901_101,
            created_by_discord_user_id=901_201,
        )
    )
    past_season = Season(
        name="past-worker-season",
        start_at=datetime(2025, 1, 13, 15, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2025, 2, 13, 15, 0, 0, tzinfo=timezone.utc),
        completed=False,
        completed_at=None,
    )
    session.add(past_season)
    session.commit()

    run_daily_jobs(
        session_factory,
        started_at=datetime(2026, 4, 5, 0, 0, 0, tzinfo=timezone.utc),
    )

    session.expire_all()
    outbox_events = session.scalars(
        select(OutboxEvent).order_by(OutboxEvent.id)
    ).all()
    persisted_past_season = session.get(Season, past_season.id)
    target_events = [
        event
        for event in outbox_events
        if event.dedupe_key == f"season_completed:{past_season.id}"
        or event.dedupe_key == f"season_top_rankings:{past_season.id}:1v1"
        or event.dedupe_key == f"season_top_rankings:{past_season.id}:2v2"
        or event.dedupe_key == f"season_top_rankings:{past_season.id}:3v3"
    ]

    assert persisted_past_season is not None
    assert persisted_past_season.completed is True
    assert persisted_past_season.completed_at is not None
    assert [event.event_type for event in target_events] == [
        OutboxEventType.SEASON_COMPLETED,
        OutboxEventType.SEASON_TOP_RANKINGS,
        OutboxEventType.SEASON_TOP_RANKINGS,
        OutboxEventType.SEASON_TOP_RANKINGS,
    ]
    assert target_events[0].payload == {
        "season_id": past_season.id,
        "season_name": "past-worker-season",
        "completed_at": persisted_past_season.completed_at.isoformat(),
        "destination": {
            "kind": "channel",
            "channel_id": 901_001,
            "guild_id": None,
        },
    }
    assert [event.payload["match_format"] for event in target_events[1:]] == ["1v1", "2v2", "3v3"]
    assert [event.payload["entries"] for event in target_events[1:]] == [[], [], []]
