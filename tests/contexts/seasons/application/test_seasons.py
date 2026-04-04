from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from dxd_rating.contexts.players.application import register_player
from dxd_rating.contexts.seasons.application import (
    ensure_active_and_upcoming_seasons,
    list_started_seasons,
    resolve_player_format_stats_for_season,
    update_ended_season_completions,
)
from dxd_rating.platform.db.models import CarryoverStatus, MatchFormat, PlayerFormatStats, Season


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
