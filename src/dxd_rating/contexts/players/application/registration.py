from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application.errors import (
    PlayerAlreadyRegisteredError,
    PlayerNotRegisteredError,
)
from dxd_rating.contexts.players.domain import (
    resolve_player_display_name,
    resolve_registered_display_name,
)
from dxd_rating.contexts.seasons.application import (
    PlayerSeasonInfo as PlayerInfo,
)
from dxd_rating.contexts.seasons.application import (
    ensure_player_stats_for_current_and_future_seasons,
    get_current_player_season_info_by_discord_user_id,
    get_player_season_info_by_discord_user_id,
)
from dxd_rating.platform.db.models import Player
from dxd_rating.platform.db.session import session_scope


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

    ensure_player_stats_for_current_and_future_seasons(session, player_id=player.id)
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
            return get_current_player_season_info_by_discord_user_id(
                session,
                discord_user_id=discord_user_id,
            )

    def get_player_info_by_discord_user_id_and_season_id(
        self,
        discord_user_id: int,
        season_id: int,
    ) -> PlayerInfo:
        with session_scope(self.session_factory) as session:
            return get_player_season_info_by_discord_user_id(
                session,
                discord_user_id=discord_user_id,
                season_id=season_id,
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
