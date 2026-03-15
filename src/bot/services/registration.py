from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.db.session import session_scope
from bot.models import Player
from bot.services.errors import PlayerAlreadyRegisteredError, PlayerNotRegisteredError


def register_player(session: Session, discord_user_id: int) -> Player:
    existing_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )
    if existing_player is not None:
        raise PlayerAlreadyRegisteredError(f"Player already registered: {discord_user_id}")

    player = Player(discord_user_id=discord_user_id)
    session.add(player)
    session.flush()
    return player


class PlayerLookupService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def get_player_id_by_discord_user_id(self, discord_user_id: int) -> int:
        with session_scope(self.session_factory) as session:
            player_id = session.scalar(
                select(Player.id).where(Player.discord_user_id == discord_user_id)
            )

        if player_id is None:
            raise PlayerNotRegisteredError(
                f"Player is not registered for discord_user_id: {discord_user_id}"
            )

        return player_id
