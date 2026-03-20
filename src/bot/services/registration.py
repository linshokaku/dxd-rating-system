from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.db.session import session_scope
from bot.models import Player
from bot.services.errors import PlayerAlreadyRegisteredError, PlayerNotRegisteredError


@dataclass(frozen=True, slots=True)
class PlayerInfo:
    player_id: int
    discord_user_id: int
    rating: float
    games_played: int
    wins: int
    losses: int
    draws: int


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

    def get_player_info_by_discord_user_id(self, discord_user_id: int) -> PlayerInfo:
        with session_scope(self.session_factory) as session:
            player = session.scalar(select(Player).where(Player.discord_user_id == discord_user_id))

        if player is None:
            raise PlayerNotRegisteredError(
                f"Player is not registered for discord_user_id: {discord_user_id}"
            )

        return PlayerInfo(
            player_id=player.id,
            discord_user_id=player.discord_user_id,
            rating=player.rating,
            games_played=player.games_played,
            wins=player.wins,
            losses=player.losses,
            draws=player.draws,
        )
