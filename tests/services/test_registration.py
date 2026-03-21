import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.models import INITIAL_RATING, MatchFormat, Player, PlayerFormatStats
from bot.services import (
    PlayerAlreadyRegisteredError,
    PlayerLookupService,
    PlayerNotRegisteredError,
    register_player,
)


def test_register_player_creates_player_with_initial_rating(session: Session) -> None:
    discord_user_id = 123456789012345678

    player = register_player(session=session, discord_user_id=discord_user_id)

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )

    assert player.id is not None
    assert player.discord_user_id == discord_user_id
    assert player.created_at is not None
    assert persisted_player is not None
    assert persisted_player.id == player.id
    persisted_format_stats = session.scalars(
        select(PlayerFormatStats).where(PlayerFormatStats.player_id == player.id)
    ).all()
    assert len(persisted_format_stats) == 3
    assert {format_stats.match_format for format_stats in persisted_format_stats} == {
        MatchFormat.ONE_VS_ONE,
        MatchFormat.TWO_VS_TWO,
        MatchFormat.THREE_VS_THREE,
    }
    assert all(format_stats.rating == INITIAL_RATING for format_stats in persisted_format_stats)
    assert all(format_stats.games_played == 0 for format_stats in persisted_format_stats)
    assert all(format_stats.wins == 0 for format_stats in persisted_format_stats)
    assert all(format_stats.losses == 0 for format_stats in persisted_format_stats)
    assert all(format_stats.draws == 0 for format_stats in persisted_format_stats)


def test_register_player_raises_for_duplicate_discord_user_id(session: Session) -> None:
    discord_user_id = 123456789012345678
    register_player(session=session, discord_user_id=discord_user_id)

    with pytest.raises(PlayerAlreadyRegisteredError):
        register_player(session=session, discord_user_id=discord_user_id)

    players = session.scalars(select(Player)).all()

    assert len(players) == 1
    assert players[0].discord_user_id == discord_user_id


def test_player_lookup_service_returns_player_id_for_registered_discord_user_id(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123456789012345679
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    service = PlayerLookupService(session_factory)

    player_id = service.get_player_id_by_discord_user_id(discord_user_id)

    assert player_id == player.id


def test_player_lookup_service_returns_player_info_for_registered_discord_user_id(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123456789012345681
    player = register_player(session=session, discord_user_id=discord_user_id)
    three_vs_three_stats = session.get(
        PlayerFormatStats,
        {"player_id": player.id, "match_format": MatchFormat.THREE_VS_THREE},
    )
    assert three_vs_three_stats is not None
    three_vs_three_stats.rating = 1523.75
    three_vs_three_stats.games_played = 12
    three_vs_three_stats.wins = 7
    three_vs_three_stats.losses = 4
    three_vs_three_stats.draws = 1
    session.commit()
    service = PlayerLookupService(session_factory)

    player_info = service.get_player_info_by_discord_user_id(discord_user_id)
    format_stats_by_format = {
        format_stats.match_format: format_stats for format_stats in player_info.format_stats
    }

    assert player_info.player_id == player.id
    assert player_info.discord_user_id == discord_user_id
    assert format_stats_by_format[MatchFormat.ONE_VS_ONE].rating == INITIAL_RATING
    assert format_stats_by_format[MatchFormat.TWO_VS_TWO].rating == INITIAL_RATING
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].rating == 1523.75
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].games_played == 12
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].wins == 7
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].losses == 4
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].draws == 1


def test_player_lookup_service_raises_for_unregistered_discord_user_id(
    session_factory: sessionmaker[Session],
) -> None:
    service = PlayerLookupService(session_factory)

    with pytest.raises(PlayerNotRegisteredError):
        service.get_player_id_by_discord_user_id(123456789012345680)


def test_player_lookup_service_raises_for_unregistered_player_info_lookup(
    session_factory: sessionmaker[Session],
) -> None:
    service = PlayerLookupService(session_factory)

    with pytest.raises(PlayerNotRegisteredError):
        service.get_player_info_by_discord_user_id(123456789012345682)
