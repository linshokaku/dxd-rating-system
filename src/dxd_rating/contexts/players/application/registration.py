from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from dxd_rating.contexts.common.application.errors import (
    PlayerAlreadyRegisteredError,
    PlayerNotRegisteredError,
)
from dxd_rating.contexts.players.domain import (
    build_dummy_player_display_name,
    format_player_display_name,
    resolve_player_display_name,
    resolve_registered_display_name,
)
from dxd_rating.platform.db.models import MatchFormat, Player, PlayerFormatStats
from dxd_rating.platform.db.session import session_scope
from dxd_rating.shared.constants import get_match_format_definitions


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
    display_name: str | None
    display_name_updated_at: datetime | None
    last_seen_at: datetime | None
    format_stats: tuple[PlayerFormatInfo, ...]

    @property
    def resolved_display_name(self) -> str:
        return format_player_display_name(
            discord_user_id=self.discord_user_id,
            display_name=self.display_name,
        )

def register_player(
    session: Session,
    discord_user_id: int,
    *,
    display_name: str | None = None,
    observed_at: datetime | None = None,
) -> Player:
    existing_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )
    if existing_player is not None:
        raise PlayerAlreadyRegisteredError(f"Player already registered: {discord_user_id}")

    player = Player(discord_user_id=discord_user_id)
    session.add(player)
    session.flush()

    resolved_display_name = resolve_registered_display_name(
        discord_user_id=discord_user_id,
        display_name=display_name,
    )

    if resolved_display_name is not None:
        resolved_observed_at = _utcnow() if observed_at is None else observed_at
        player.display_name = resolved_display_name
        player.display_name_updated_at = resolved_observed_at
        player.last_seen_at = resolved_observed_at

    player.format_stats.extend(
        [
            PlayerFormatStats(match_format=format_definition.match_format)
            for format_definition in get_match_format_definitions()
        ]
    )
    session.flush()
    return player


def update_player_identity(
    session: Session,
    *,
    discord_user_id: int,
    display_name: str | None = None,
    observed_at: datetime | None = None,
) -> bool:
    player = session.scalar(select(Player).where(Player.discord_user_id == discord_user_id))
    if player is None:
        return False

    resolved_display_name = resolve_registered_display_name(
        discord_user_id=discord_user_id,
        display_name=display_name,
    )
    if resolved_display_name is None:
        return False

    resolved_observed_at = _utcnow() if observed_at is None else observed_at
    player.display_name = resolved_display_name
    player.display_name_updated_at = resolved_observed_at
    player.last_seen_at = resolved_observed_at
    session.flush()
    return True


class PlayerIdentityService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def sync_discord_user(self, discord_user: Any) -> bool:
        discord_user_id, display_name = _resolve_discord_user_identity(discord_user)
        observed_at = _utcnow()
        with session_scope(self.session_factory) as session:
            return update_player_identity(
                session,
                discord_user_id=discord_user_id,
                display_name=display_name,
                observed_at=observed_at,
            )


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

        return cast(int, player_id)

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
            display_name=player.display_name,
            display_name_updated_at=player.display_name_updated_at,
            last_seen_at=player.last_seen_at,
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

def _resolve_discord_user_identity(discord_user: Any) -> tuple[int, str | None]:
    discord_user_id = getattr(discord_user, "id", None)
    if not isinstance(discord_user_id, int):
        raise ValueError("discord_user.id must be an integer")

    display_name = resolve_player_display_name(
        discord_user_id=discord_user_id,
        guild_display_name=getattr(discord_user, "nick", None),
        global_display_name=getattr(discord_user, "global_name", None),
        username=getattr(discord_user, "name", None),
    )
    return discord_user_id, display_name


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
