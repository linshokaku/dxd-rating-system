import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.models import INITIAL_RATING, Player
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
    assert player.rating == INITIAL_RATING
    assert player.games_played == 0
    assert player.wins == 0
    assert player.losses == 0
    assert player.draws == 0
    assert player.created_at is not None
    assert persisted_player is not None
    assert persisted_player.id == player.id


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


def test_player_lookup_service_raises_for_unregistered_discord_user_id(
    session_factory: sessionmaker[Session],
) -> None:
    service = PlayerLookupService(session_factory)

    with pytest.raises(PlayerNotRegisteredError):
        service.get_player_id_by_discord_user_id(123456789012345680)
