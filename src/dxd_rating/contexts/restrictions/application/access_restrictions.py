from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application.errors import (
    InvalidPlayerAccessRestrictionDurationError,
    InvalidPlayerAccessRestrictionTypeError,
    PlayerAccessRestrictionAlreadyExistsError,
    PlayerNotRegisteredError,
)
from dxd_rating.contexts.restrictions.domain import (
    PlayerAccessRestrictionDuration,
    build_access_restriction_expires_at,
    normalize_access_restriction_reason,
)
from dxd_rating.platform.db.models import (
    Player,
    PlayerAccessRestriction,
    PlayerAccessRestrictionType,
)
from dxd_rating.platform.db.session import session_scope

QUEUE_JOIN_RESTRICTED_MESSAGE = "現在キュー参加を制限されています。"
SPECTATE_RESTRICTED_MESSAGE = "現在観戦を制限されています。"


@dataclass(frozen=True, slots=True)
class RestrictPlayerAccessResult:
    restriction_id: int
    player_id: int
    restriction_type: PlayerAccessRestrictionType
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class UnrestrictPlayerAccessResult:
    player_id: int
    restriction_type: PlayerAccessRestrictionType
    revoked: bool


def get_active_player_access_restriction(
    session: Session,
    *,
    player_id: int,
    restriction_type: PlayerAccessRestrictionType,
    for_update: bool = False,
) -> PlayerAccessRestriction | None:
    query = (
        select(PlayerAccessRestriction)
        .where(
            PlayerAccessRestriction.player_id == player_id,
            PlayerAccessRestriction.restriction_type == restriction_type,
            PlayerAccessRestriction.revoked_at.is_(None),
            or_(
                PlayerAccessRestriction.expires_at.is_(None),
                PlayerAccessRestriction.expires_at > func.now(),
            ),
        )
        .order_by(PlayerAccessRestriction.created_at.desc(), PlayerAccessRestriction.id.desc())
    )
    if for_update:
        query = query.with_for_update()
    return session.scalar(query)


class PlayerAccessRestrictionService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def restrict_player_access(
        self,
        player_id: int,
        restriction_type: PlayerAccessRestrictionType | str,
        duration: PlayerAccessRestrictionDuration | str,
        *,
        admin_discord_user_id: int,
        reason: str | None = None,
    ) -> RestrictPlayerAccessResult:
        resolved_restriction_type = self._resolve_restriction_type(restriction_type)
        resolved_duration = self._resolve_duration(duration)

        with session_scope(self.session_factory) as session:
            self._ensure_player_exists(session, player_id)
            self._acquire_player_lock(session, player_id)

            existing_restriction = get_active_player_access_restriction(
                session,
                player_id=player_id,
                restriction_type=resolved_restriction_type,
                for_update=True,
            )
            if existing_restriction is not None:
                raise PlayerAccessRestrictionAlreadyExistsError(
                    "指定したユーザーにはすでに同種別の制限が有効です。"
                )

            current_time = self._get_database_now(session)
            expires_at = build_access_restriction_expires_at(
                current_time=current_time,
                duration=resolved_duration,
            )
            restriction = PlayerAccessRestriction(
                player_id=player_id,
                restriction_type=resolved_restriction_type,
                created_at=current_time,
                expires_at=expires_at,
                created_by_admin_discord_user_id=admin_discord_user_id,
                reason=normalize_access_restriction_reason(reason),
            )
            session.add(restriction)
            session.flush()

            return RestrictPlayerAccessResult(
                restriction_id=restriction.id,
                player_id=player_id,
                restriction_type=restriction.restriction_type,
                expires_at=restriction.expires_at,
            )

    def unrestrict_player_access(
        self,
        player_id: int,
        restriction_type: PlayerAccessRestrictionType | str,
        *,
        admin_discord_user_id: int,
    ) -> UnrestrictPlayerAccessResult:
        resolved_restriction_type = self._resolve_restriction_type(restriction_type)

        with session_scope(self.session_factory) as session:
            self._ensure_player_exists(session, player_id)
            self._acquire_player_lock(session, player_id)

            restriction = get_active_player_access_restriction(
                session,
                player_id=player_id,
                restriction_type=resolved_restriction_type,
                for_update=True,
            )
            if restriction is None:
                return UnrestrictPlayerAccessResult(
                    player_id=player_id,
                    restriction_type=resolved_restriction_type,
                    revoked=False,
                )

            current_time = self._get_database_now(session)
            restriction.revoked_at = current_time
            restriction.revoked_by_admin_discord_user_id = admin_discord_user_id
            return UnrestrictPlayerAccessResult(
                player_id=player_id,
                restriction_type=resolved_restriction_type,
                revoked=True,
            )

    def _resolve_restriction_type(
        self,
        restriction_type: PlayerAccessRestrictionType | str,
    ) -> PlayerAccessRestrictionType:
        try:
            if isinstance(restriction_type, PlayerAccessRestrictionType):
                return restriction_type
            return PlayerAccessRestrictionType(restriction_type)
        except ValueError as exc:
            raise InvalidPlayerAccessRestrictionTypeError(
                f"Invalid restriction_type: {restriction_type}"
            ) from exc

    def _resolve_duration(
        self,
        duration: PlayerAccessRestrictionDuration | str,
    ) -> PlayerAccessRestrictionDuration:
        try:
            if isinstance(duration, PlayerAccessRestrictionDuration):
                return duration
            return PlayerAccessRestrictionDuration(duration)
        except ValueError as exc:
            raise InvalidPlayerAccessRestrictionDurationError(
                f"Invalid duration: {duration}"
            ) from exc

    def _ensure_player_exists(self, session: Session, player_id: int) -> Player:
        player = session.get(Player, player_id)
        if player is None:
            raise PlayerNotRegisteredError(f"Player is not registered: {player_id}")
        return player

    def _acquire_player_lock(self, session: Session, player_id: int) -> None:
        session.execute(select(func.pg_advisory_xact_lock(player_id)))

    def _get_database_now(self, session: Session) -> datetime:
        return session.execute(select(func.now())).scalar_one()
