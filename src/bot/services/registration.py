from sqlalchemy import select
from sqlalchemy.orm import Session

from bot.models import Player
from bot.services.errors import PlayerAlreadyRegisteredError


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
