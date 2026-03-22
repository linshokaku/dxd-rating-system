from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.models import INITIAL_RATING, MatchFormat, Player, PlayerFormatStats
from dxd_rating.services import (
    PlayerAlreadyRegisteredError,
    PlayerIdentityService,
    PlayerLookupService,
    PlayerNotRegisteredError,
    register_player,
    resolve_player_display_name,
)


@dataclass(frozen=True)
class FakeDiscordUser:
    id: int
    name: str | None = None
    global_name: str | None = None
    nick: str | None = None


def test_register_player_creates_player_with_initial_rating(session: Session) -> None:
    discord_user_id = 123456789012345678

    player = register_player(session=session, discord_user_id=discord_user_id)

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )

    assert player.id is not None
    assert player.discord_user_id == discord_user_id
    assert player.display_name is None
    assert player.display_name_updated_at is None
    assert player.last_seen_at is None
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
    assert all(format_stats.last_played_at is None for format_stats in persisted_format_stats)


def test_register_player_sets_fixed_display_name_for_dummy_user(session: Session) -> None:
    discord_user_id = 777

    player = register_player(session=session, discord_user_id=discord_user_id)

    assert player.display_name == "<dummy_777>"
    assert player.display_name_updated_at is not None
    assert player.last_seen_at == player.display_name_updated_at


def test_register_player_raises_for_duplicate_discord_user_id(session: Session) -> None:
    discord_user_id = 123456789012345678
    register_player(session=session, discord_user_id=discord_user_id)

    with pytest.raises(PlayerAlreadyRegisteredError):
        register_player(session=session, discord_user_id=discord_user_id)

    players = session.scalars(select(Player)).all()

    assert len(players) == 1
    assert players[0].discord_user_id == discord_user_id


def test_resolve_player_display_name_uses_expected_priority() -> None:
    discord_user_id = 123456789012345690

    assert (
        resolve_player_display_name(
            discord_user_id=discord_user_id,
            guild_display_name=" guild name ",
            global_display_name="global name",
            username="username",
        )
        == "guild name"
    )
    assert (
        resolve_player_display_name(
            discord_user_id=discord_user_id,
            global_display_name="global name",
            username="username",
        )
        == "global name"
    )
    assert (
        resolve_player_display_name(
            discord_user_id=discord_user_id,
            username="username",
        )
        == "username"
    )


def test_player_identity_service_updates_registered_player_identity(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123456789012345689
    register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    service = PlayerIdentityService(session_factory)

    updated = service.sync_discord_user(
        FakeDiscordUser(
            id=discord_user_id,
            name="username",
            global_name="global name",
            nick="guild name",
        )
    )

    session.expire_all()
    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )

    assert updated is True
    assert persisted_player is not None
    assert persisted_player.display_name == "guild name"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


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
    player.display_name = "Cached Name"
    player.display_name_updated_at = datetime(2026, 3, 19, 9, 30, 0, tzinfo=timezone.utc)
    player.last_seen_at = datetime(2026, 3, 20, 8, 0, 0, tzinfo=timezone.utc)
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
    three_vs_three_stats.last_played_at = datetime(2026, 3, 20, 12, 34, 56, tzinfo=timezone.utc)
    session.commit()
    service = PlayerLookupService(session_factory)

    player_info = service.get_player_info_by_discord_user_id(discord_user_id)
    format_stats_by_format = {
        format_stats.match_format: format_stats for format_stats in player_info.format_stats
    }

    assert player_info.player_id == player.id
    assert player_info.discord_user_id == discord_user_id
    assert player_info.display_name == "Cached Name"
    assert player_info.display_name_updated_at == datetime(
        2026,
        3,
        19,
        9,
        30,
        0,
        tzinfo=timezone.utc,
    )
    assert player_info.last_seen_at == datetime(
        2026,
        3,
        20,
        8,
        0,
        0,
        tzinfo=timezone.utc,
    )
    assert player_info.resolved_display_name == "Cached Name"
    assert format_stats_by_format[MatchFormat.ONE_VS_ONE].rating == INITIAL_RATING
    assert format_stats_by_format[MatchFormat.TWO_VS_TWO].rating == INITIAL_RATING
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].rating == 1523.75
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].games_played == 12
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].wins == 7
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].losses == 4
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].draws == 1
    assert format_stats_by_format[MatchFormat.THREE_VS_THREE].last_played_at == datetime(
        2026,
        3,
        20,
        12,
        34,
        56,
        tzinfo=timezone.utc,
    )


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


def test_player_info_resolved_display_name_falls_back_to_discord_user_id(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123456789012345683
    register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    service = PlayerLookupService(session_factory)

    player_info = service.get_player_info_by_discord_user_id(discord_user_id)

    assert player_info.display_name is None
    assert player_info.resolved_display_name == str(discord_user_id)
