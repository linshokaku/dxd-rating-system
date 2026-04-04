from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from dxd_rating.contexts.common.application import (
    InvalidLeaderboardPageError,
    InvalidMatchFormatError,
    LeaderboardPageNotFoundError,
)
from dxd_rating.contexts.leaderboard.application import get_current_leaderboard_page
from dxd_rating.platform.db.models import (
    LeaderboardSnapshot,
    MatchFormat,
    Player,
    PlayerFormatStats,
    Season,
)


def test_get_current_leaderboard_page_returns_ranked_entries_with_rank_changes(
    session: Session,
) -> None:
    current_time = datetime(2026, 3, 22, 3, 15, 0, tzinfo=timezone.utc)
    active_season, upcoming_season = create_active_and_upcoming_seasons(
        session,
        current_time=current_time,
    )
    players = create_players(
        session,
        display_names=("Alice", "Bob", "Carol", "Dave"),
        start_discord_user_id=123_456_789_012_345_600,
    )
    session.add_all(
        (
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
            LeaderboardSnapshot(
                snapshot_date=date(2026, 3, 21),
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=players[1].id,
                rank=2,
                rating=1590,
                games_played=4,
            ),
            LeaderboardSnapshot(
                snapshot_date=date(2026, 3, 21),
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=players[2].id,
                rank=1,
                rating=1610,
                games_played=4,
            ),
            LeaderboardSnapshot(
                snapshot_date=date(2026, 3, 19),
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=players[0].id,
                rank=1,
                rating=1650,
                games_played=1,
            ),
            LeaderboardSnapshot(
                snapshot_date=date(2026, 3, 19),
                season_id=active_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=players[1].id,
                rank=3,
                rating=1580,
                games_played=3,
            ),
            LeaderboardSnapshot(
                snapshot_date=date(2026, 3, 15),
                season_id=upcoming_season.id,
                match_format=MatchFormat.THREE_VS_THREE,
                player_id=players[0].id,
                rank=1,
                rating=1700,
                games_played=6,
            ),
        )
    )
    session.flush()

    result = get_current_leaderboard_page(
        session,
        match_format=MatchFormat.THREE_VS_THREE,
        page=1,
        current_time=current_time,
    )

    assert result.season_id == active_season.id
    assert result.season_name == active_season.name
    assert result.match_format == MatchFormat.THREE_VS_THREE
    assert result.page == 1
    assert result.page_size == 20
    assert [
        (
            entry.rank,
            entry.display_name,
            entry.rating,
            entry.games_played,
            entry.wins,
            entry.losses,
            entry.draws,
            entry.rank_change_1d,
            entry.rank_change_3d,
            entry.rank_change_7d,
        )
        for entry in result.entries
    ] == [
        (1, "Bob", 1600, 5, 4, 1, 0, 1, 2, None),
        (2, "Carol", 1600, 5, 3, 2, 0, -1, None, None),
        (3, "Alice", 1600, 2, 2, 0, 0, None, -2, None),
    ]


def test_get_current_leaderboard_page_returns_page_two_with_fallback_display_name(
    session: Session,
) -> None:
    current_time = datetime(2026, 3, 22, 3, 15, 0, tzinfo=timezone.utc)
    active_season, _ = create_active_and_upcoming_seasons(session, current_time=current_time)
    players = create_players(
        session,
        display_names=tuple(f"Player {index}" for index in range(1, 21)) + (None,),
        start_discord_user_id=223_456_789_012_345_600,
    )
    session.add_all(
        PlayerFormatStats(
            player_id=player.id,
            season_id=active_season.id,
            match_format=MatchFormat.THREE_VS_THREE,
            rating=2000 - index,
            games_played=1,
            wins=1,
        )
        for index, player in enumerate(players)
    )
    session.flush()

    result = get_current_leaderboard_page(
        session,
        match_format=MatchFormat.THREE_VS_THREE,
        page=2,
        current_time=current_time,
    )

    assert len(result.entries) == 1
    assert result.entries[0].rank == 21
    assert result.entries[0].display_name == str(players[20].discord_user_id)


def test_get_current_leaderboard_page_rejects_invalid_page(session: Session) -> None:
    with pytest.raises(InvalidLeaderboardPageError, match="page は 1 以上で指定してください。"):
        get_current_leaderboard_page(
            session,
            match_format=MatchFormat.THREE_VS_THREE,
            page=0,
            current_time=datetime(2026, 3, 22, 3, 15, 0, tzinfo=timezone.utc),
        )


def test_get_current_leaderboard_page_rejects_invalid_match_format(session: Session) -> None:
    with pytest.raises(InvalidMatchFormatError, match="指定したフォーマットは存在しません。"):
        get_current_leaderboard_page(
            session,
            match_format="invalid",
            page=1,
            current_time=datetime(2026, 3, 22, 3, 15, 0, tzinfo=timezone.utc),
        )


def test_get_current_leaderboard_page_raises_when_page_has_no_entries(
    session: Session,
) -> None:
    current_time = datetime(2026, 3, 22, 3, 15, 0, tzinfo=timezone.utc)
    active_season, _ = create_active_and_upcoming_seasons(session, current_time=current_time)
    player = create_players(
        session,
        display_names=("Solo",),
        start_discord_user_id=323_456_789_012_345_600,
    )[0]
    session.add(
        PlayerFormatStats(
            player_id=player.id,
            season_id=active_season.id,
            match_format=MatchFormat.THREE_VS_THREE,
            rating=1600,
            games_played=1,
            wins=1,
        )
    )
    session.flush()

    with pytest.raises(
        LeaderboardPageNotFoundError,
        match="指定したページにはランキングがありません。",
    ):
        get_current_leaderboard_page(
            session,
            match_format=MatchFormat.THREE_VS_THREE,
            page=2,
            current_time=current_time,
        )


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


def create_players(
    session: Session,
    *,
    display_names: tuple[str | None, ...],
    start_discord_user_id: int,
) -> tuple[Player, ...]:
    players = tuple(
        Player(
            discord_user_id=start_discord_user_id + index,
            display_name=display_name,
        )
        for index, display_name in enumerate(display_names, start=1)
    )
    session.add_all(players)
    session.flush()
    return players
