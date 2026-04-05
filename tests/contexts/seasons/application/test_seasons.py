import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from dxd_rating.contexts.common.application.errors import SeasonStateError
from dxd_rating.contexts.players.application import register_player
from dxd_rating.contexts.seasons.application import (
    ensure_active_and_upcoming_seasons,
    force_end_active_season,
    list_started_seasons,
    resolve_player_format_stats_for_season,
    update_season_completion,
    update_ended_season_completions,
)
from dxd_rating.platform.db.models import (
    CarryoverStatus,
    ManagedUiChannel,
    ManagedUiType,
    MatchFormat,
    OutboxEvent,
    OutboxEventType,
    PlayerFormatStats,
    Season,
)


def test_resolve_player_format_stats_for_season_applies_carryover_only_once(
    session: Session,
) -> None:
    ensure_active_and_upcoming_seasons(session)
    session.commit()
    player = register_player(session=session, discord_user_id=123_456_789_012_345_699)
    season_pair = ensure_active_and_upcoming_seasons(session)
    previous_stats = session.scalar(
        select(PlayerFormatStats).where(
            PlayerFormatStats.player_id == player.id,
            PlayerFormatStats.season_id == season_pair.active.id,
            PlayerFormatStats.match_format == MatchFormat.THREE_VS_THREE,
        )
    )
    assert previous_stats is not None
    previous_stats.rating = 1800
    previous_stats.games_played = 10
    previous_stats.carryover_status = CarryoverStatus.NOT_APPLIED
    session.flush()

    resolved_stats = resolve_player_format_stats_for_season(
        session,
        player_ids=(player.id,),
        season_id=season_pair.upcoming.id,
        match_format=MatchFormat.THREE_VS_THREE,
    )[player.id]

    assert resolved_stats.rating == 1605
    assert resolved_stats.carryover_status == CarryoverStatus.APPLIED
    assert resolved_stats.carryover_source_season_id == season_pair.active.id
    assert resolved_stats.carryover_source_rating == 1800

    previous_stats.rating = 2000
    session.flush()
    resolved_again = resolve_player_format_stats_for_season(
        session,
        player_ids=(player.id,),
        season_id=season_pair.upcoming.id,
        match_format=MatchFormat.THREE_VS_THREE,
    )[player.id]

    assert resolved_again.rating == 1605
    assert resolved_again.carryover_status == CarryoverStatus.APPLIED
    assert resolved_again.carryover_source_rating == 1800


def test_update_ended_season_completions_marks_matchless_past_season_completed(
    session: Session,
) -> None:
    past_season = Season(
        name="past-cup",
        start_at=datetime(2025, 1, 13, 15, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2025, 2, 13, 15, 0, 0, tzinfo=timezone.utc),
        completed=False,
        completed_at=None,
    )
    session.add(past_season)
    session.flush()

    completed_season_ids = update_ended_season_completions(
        session,
        current_time=datetime(2026, 3, 22, 0, 0, 0, tzinfo=timezone.utc),
    )

    assert completed_season_ids == (past_season.id,)
    assert past_season.completed is True
    assert past_season.completed_at == datetime(2026, 3, 22, 0, 0, 0, tzinfo=timezone.utc)


def test_update_season_completion_enqueues_system_announcement_notification(
    session: Session,
) -> None:
    session.add(
        ManagedUiChannel(
            ui_type=ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL,
            channel_id=910_001,
            message_id=910_101,
            created_by_discord_user_id=910_201,
        )
    )
    past_season = Season(
        name="past-summary",
        start_at=datetime(2025, 1, 13, 15, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2025, 2, 13, 15, 0, 0, tzinfo=timezone.utc),
        completed=False,
        completed_at=None,
    )
    session.add(past_season)
    session.flush()

    updated = update_season_completion(
        session,
        season_id=past_season.id,
        current_time=datetime(2026, 3, 22, 0, 0, 0, tzinfo=timezone.utc),
    )

    outbox_events = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()

    assert updated is True
    assert past_season.completed is True
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == OutboxEventType.SEASON_COMPLETED
    assert outbox_events[0].dedupe_key == f"season_completed:{past_season.id}"
    assert outbox_events[0].payload == {
        "season_id": past_season.id,
        "season_name": "past-summary",
        "completed_at": "2026-03-22T00:00:00+00:00",
        "destination": {
            "kind": "channel",
            "channel_id": 910_001,
            "guild_id": None,
        },
    }


def test_update_season_completion_skips_notification_when_channel_is_missing(
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    past_season = Season(
        name="past-no-channel",
        start_at=datetime(2025, 1, 13, 15, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2025, 2, 13, 15, 0, 0, tzinfo=timezone.utc),
        completed=False,
        completed_at=None,
    )
    session.add(past_season)
    session.flush()

    updated = update_season_completion(
        session,
        season_id=past_season.id,
        current_time=datetime(2026, 3, 22, 0, 0, 0, tzinfo=timezone.utc),
    )

    outbox_events = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()

    assert updated is True
    assert past_season.completed is True
    assert outbox_events == []
    assert (
        "Skipping season completed notification because system_announcements_channel "
        "is not configured season_id="
    ) in caplog.text


def test_list_started_seasons_returns_latest_started_25_in_desc_order(
    session: Session,
) -> None:
    current_time = datetime(2026, 3, 22, 0, 0, 0, tzinfo=timezone.utc)
    latest_started_season = Season(
        name="latest-started",
        start_at=current_time - timedelta(hours=12),
        end_at=current_time + timedelta(days=29),
        completed=False,
        completed_at=None,
    )
    second_latest_started_season = Season(
        name="second-latest-started",
        start_at=current_time - timedelta(days=1),
        end_at=current_time + timedelta(days=30),
        completed=False,
        completed_at=None,
    )
    older_started_seasons = [
        Season(
            name=f"archive-{index:02d}",
            start_at=current_time - timedelta(days=index + 2),
            end_at=current_time - timedelta(days=index + 1),
            completed=True,
            completed_at=current_time - timedelta(days=index + 1),
        )
        for index in range(25)
    ]
    future_season = Season(
        name="future-season",
        start_at=current_time + timedelta(days=1),
        end_at=current_time + timedelta(days=31),
        completed=False,
        completed_at=None,
    )
    session.add_all(
        [
            latest_started_season,
            second_latest_started_season,
            *older_started_seasons,
            future_season,
        ]
    )
    session.flush()

    started_seasons = list_started_seasons(session, current_time=current_time, limit=25)

    assert len(started_seasons) == 25
    assert [season.name for season in started_seasons] == [
        "latest-started",
        "second-latest-started",
        *(f"archive-{index:02d}" for index in range(23)),
    ]
    assert all(season.start_at <= current_time for season in started_seasons)
    assert all(season.name != "future-season" for season in started_seasons)


def test_force_end_active_season_updates_only_active_end_and_upcoming_start(
    session: Session,
) -> None:
    archive_season = Season(
        name="archive-season",
        start_at=datetime(2026, 1, 13, 15, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 2, 13, 15, 0, 0, tzinfo=timezone.utc),
        completed=True,
        completed_at=datetime(2026, 2, 13, 15, 0, 0, tzinfo=timezone.utc),
    )
    session.add(archive_season)
    session.flush()

    forced_at = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    season_pair = ensure_active_and_upcoming_seasons(
        session,
        current_time=forced_at,
    )
    active_completed = season_pair.active.completed
    active_completed_at = season_pair.active.completed_at
    upcoming_end_at = season_pair.upcoming.end_at
    session.flush()

    result = force_end_active_season(session, current_time=forced_at)

    assert result.active_season_id == season_pair.active.id
    assert result.upcoming_season_id == season_pair.upcoming.id
    assert result.forced_at == forced_at
    assert result.previous_active_end_at == datetime(2026, 4, 13, 15, 0, 0, tzinfo=timezone.utc)
    assert result.previous_upcoming_start_at == datetime(2026, 4, 13, 15, 0, 0, tzinfo=timezone.utc)
    assert season_pair.active.end_at == forced_at
    assert season_pair.upcoming.start_at == forced_at
    assert season_pair.upcoming.end_at == upcoming_end_at
    assert season_pair.active.completed is active_completed
    assert season_pair.active.completed_at == active_completed_at
    session.expire_all()
    persisted_archive = session.get(Season, archive_season.id)
    assert persisted_archive is not None
    assert persisted_archive.end_at == datetime(2026, 2, 13, 15, 0, 0, tzinfo=timezone.utc)


def test_force_end_active_season_rejects_active_start_timestamp(
    session: Session,
) -> None:
    current_time = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    season_pair = ensure_active_and_upcoming_seasons(
        session,
        current_time=current_time,
    )

    with pytest.raises(
        SeasonStateError,
        match="稼働中シーズンの開始時刻以前には強制終了できません。",
    ):
        force_end_active_season(session, current_time=season_pair.active.start_at)
