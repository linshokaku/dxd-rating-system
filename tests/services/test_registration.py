import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bot.models import INITIAL_RATING, Player
from bot.services import PlayerAlreadyRegisteredError, register_player


def test_register_player_creates_player_with_initial_rating(session: Session) -> None:
    discord_user_id = 123456789012345678

    player = register_player(session=session, discord_user_id=discord_user_id)

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )

    assert player.id is not None
    assert player.discord_user_id == discord_user_id
    assert player.rating == INITIAL_RATING
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
