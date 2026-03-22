from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from dxd_rating.contexts.leaderboard.application import maintain_leaderboard_snapshots
from dxd_rating.platform.db.models import (
    LeaderboardSnapshot,
    MatchFormat,
    Player,
    PlayerFormatStats,
    Season,
)


def test_maintain_leaderboard_snapshots_creates_ranked_rows_for_each_format(
    session: Session,
) -> None:
    current_time = datetime(2026, 3, 22, 3, 15, 0, tzinfo=timezone.utc)
    active_season, upcoming_season = create_active_and_upcoming_seasons(
        session,
        current_time=current_time,
    )
    players = create_players(session, count=4)
    session.add_all(
        (
            PlayerFormatStats(
                player_id=players[0].id,
                season_id=active_season.id,
                match_format=MatchFormat.ONE_VS_ONE,
                rating=1700,
                games_played=2,
                wins=2,
            ),
            PlayerFormatStats(
                player_id=players[0].id,
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1600,
                games_played=2,
                wins=2,
            ),
            PlayerFormatStats(
                player_id=players[1].id,
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1600,
                games_played=5,
                wins=4,
                losses=1,
            ),
            PlayerFormatStats(
                player_id=players[2].id,
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1600,
                games_played=5,
                wins=3,
                losses=2,
            ),
            PlayerFormatStats(
                player_id=players[3].id,
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=1800,
                games_played=0,
            ),
            PlayerFormatStats(
                player_id=players[1].id,
                season_id=upcoming_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                rating=2500,
                games_played=10,
                wins=10,
            ),
        )
    )

    result = maintain_leaderboard_snapshots(session, current_time=current_time)

    snapshots = session.scalars(
        select(LeaderboardSnapshot).order_by(
            LeaderboardSnapshot.match_format,
            LeaderboardSnapshot.rank,
        )
    ).all()

    assert result.snapshot_date == date(2026, 3, 22)
    assert result.season_id == active_season.id
    assert result.created_count == 4
    assert result.deleted_count == 0
    assert result.skipped_creation is False
    assert [
        (snapshot.match_format, snapshot.player_id, snapshot.rank) for snapshot in snapshots
    ] == [
        (MatchFormat.ONE_VS_ONE, players[0].id, 1),
        (MatchFormat.THREE_VS_THREE, players[1].id, 1),
        (MatchFormat.THREE_VS_THREE, players[2].id, 2),
        (MatchFormat.THREE_VS_THREE, players[0].id, 3),
    ]
    assert all(snapshot.season_id == active_season.id for snapshot in snapshots)
    assert all(snapshot.snapshot_date == date(2026, 3, 22) for snapshot in snapshots)


def test_maintain_leaderboard_snapshots_skips_existing_snapshot_date_and_season(
    session: Session,
) -> None:
    current_time = datetime(2026, 3, 22, 3, 15, 0, tzinfo=timezone.utc)
    active_season, _ = create_active_and_upcoming_seasons(session, current_time=current_time)
    player = create_players(session, count=1)[0]
    session.add(
        PlayerFormatStats(
            player_id=player.id,
            season_id=active_season.id,
            match_format=MatchFormat.THREE_VS_THREE,
            rating=1550,
            games_played=1,
            wins=1,
        )
    )

    first_result = maintain_leaderboard_snapshots(session, current_time=current_time)
    active_stats = session.scalar(
        select(PlayerFormatStats).where(
            PlayerFormatStats.player_id == player.id,
            PlayerFormatStats.season_id == active_season.id,
            PlayerFormatStats.match_format == MatchFormat.THREE_VS_THREE,
        )
    )
    assert active_stats is not None
    active_stats.rating = 1800
    active_stats.games_played = 2

    second_result = maintain_leaderboard_snapshots(session, current_time=current_time)
    snapshots = session.scalars(select(LeaderboardSnapshot)).all()

    assert first_result.created_count == 1
    assert second_result.created_count == 0
    assert second_result.skipped_creation is True
    assert len(snapshots) == 1
    assert snapshots[0].rating == 1550
    assert snapshots[0].games_played == 1


def test_maintain_leaderboard_snapshots_deletes_snapshots_older_than_retention_window(
    session: Session,
) -> None:
    current_time = datetime(2026, 3, 22, 3, 15, 0, tzinfo=timezone.utc)
    active_season, _ = create_active_and_upcoming_seasons(session, current_time=current_time)
    player = create_players(session, count=1)[0]
    session.add_all(
        (
            LeaderboardSnapshot(
                snapshot_date=date(2025, 9, 23),
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=player.id,
                rank=1,
                rating=1500,
                games_played=1,
            ),
            LeaderboardSnapshot(
                snapshot_date=date(2025, 9, 24),
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=player.id,
                rank=1,
                rating=1500,
                games_played=1,
            ),
        )
    )

    result = maintain_leaderboard_snapshots(session, current_time=current_time)
    remaining_snapshot_dates = session.scalars(
        select(LeaderboardSnapshot.snapshot_date).order_by(LeaderboardSnapshot.snapshot_date)
    ).all()

    assert result.deleted_count == 1
    assert remaining_snapshot_dates == [date(2025, 9, 24)]


def create_active_and_upcoming_seasons(
    session: Session,
    *,
    current_time: datetime,
) -> tuple[Season, Season]:
    active_season = Season(
        name="active-season",
        start_at=current_time - timedelta(days=1),
        end_at=current_time + timedelta(days=1),
        completed=False,
        completed_at=None,
    )
    upcoming_season = Season(
        name="upcoming-season",
        start_at=active_season.end_at,
        end_at=active_season.end_at + timedelta(days=30),
        completed=False,
        completed_at=None,
    )
    session.add_all((active_season, upcoming_season))
    session.flush()
    return active_season, upcoming_season


def create_players(session: Session, *, count: int) -> tuple[Player, ...]:
    players = tuple(
        Player(discord_user_id=123_456_789_012_345_600 + index) for index in range(1, count + 1)
    )
    session.add_all(players)
    session.flush()
    return players
