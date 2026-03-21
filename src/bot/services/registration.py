from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from bot.constants import get_match_format_definitions
from bot.db.session import session_scope
from bot.models import MatchFormat, Player, PlayerFormatStats
from bot.services.errors import PlayerAlreadyRegisteredError, PlayerNotRegisteredError


@dataclass(frozen=True, slots=True)
class PlayerFormatInfo:
    match_format: MatchFormat
    rating: float
    games_played: int
    wins: int
    losses: int
    draws: int
    last_played_at: datetime | None


@dataclass(frozen=True, slots=True)
class PlayerInfo:
    player_id: int
    discord_user_id: int
    format_stats: tuple[PlayerFormatInfo, ...]


def register_player(session: Session, discord_user_id: int) -> Player:
    existing_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )
    if existing_player is not None:
        raise PlayerAlreadyRegisteredError(f"Player already registered: {discord_user_id}")

    player = Player(discord_user_id=discord_user_id)
    session.add(player)
    session.flush()
    player.format_stats.extend(
        [
            PlayerFormatStats(match_format=format_definition.match_format)
            for format_definition in get_match_format_definitions()
        ]
    )
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
            player = session.scalar(
                select(Player)
                .options(selectinload(Player.format_stats))
                .where(Player.discord_user_id == discord_user_id)
            )

        if player is None:
            raise PlayerNotRegisteredError(
                f"Player is not registered for discord_user_id: {discord_user_id}"
            )

        format_stats_by_format = {
            format_stats.match_format: format_stats for format_stats in player.format_stats
        }
        return PlayerInfo(
            player_id=player.id,
            discord_user_id=player.discord_user_id,
            format_stats=tuple(
                PlayerFormatInfo(
                    match_format=format_definition.match_format,
                    rating=format_stats_by_format[format_definition.match_format].rating,
                    games_played=format_stats_by_format[
                        format_definition.match_format
                    ].games_played,
                    wins=format_stats_by_format[format_definition.match_format].wins,
                    losses=format_stats_by_format[format_definition.match_format].losses,
                    draws=format_stats_by_format[format_definition.match_format].draws,
                    last_played_at=format_stats_by_format[
                        format_definition.match_format
                    ].last_played_at,
                )
                for format_definition in get_match_format_definitions()
            ),
        )
