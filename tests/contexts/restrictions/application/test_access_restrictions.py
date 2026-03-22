from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application import PlayerAccessRestrictionAlreadyExistsError
from dxd_rating.contexts.matches.application import MatchFlowService
from dxd_rating.contexts.matchmaking.application import (
    MatchingQueueNotificationContext,
    MatchingQueueService,
)
from dxd_rating.contexts.players.application import register_player
from dxd_rating.contexts.restrictions.application import (
    PlayerAccessRestrictionDuration,
    PlayerAccessRestrictionService,
)
from dxd_rating.platform.db.models import (
    MatchFormat,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchSpectator,
    MatchSpectatorStatus,
    Player,
    PlayerAccessRestriction,
    PlayerAccessRestrictionType,
)

DEFAULT_MATCH_FORMAT = MatchFormat.THREE_VS_THREE
DEFAULT_QUEUE_NAME = "low"


def get_database_now(session: Session) -> datetime:
    return session.execute(select(func.now())).scalar_one()


def create_player(session: Session, discord_user_id: int) -> Player:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player


def create_match(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    start_discord_user_id: int,
) -> tuple[int, list[Player]]:
    players = [create_player(session, start_discord_user_id + offset) for offset in range(6)]
    queue_service = MatchingQueueService(session_factory)
    for player in players:
        queue_service.join_queue(
            player.id,
            DEFAULT_MATCH_FORMAT,
            DEFAULT_QUEUE_NAME,
            notification_context=MatchingQueueNotificationContext(
                channel_id=90_000 + player.id,
                guild_id=91_000 + player.id,
                mention_discord_user_id=player.discord_user_id,
            ),
        )

    created_matches = queue_service.try_create_matches()

    assert len(created_matches) == 1
    return created_matches[0].match_id, players


def test_restrict_player_access_creates_queue_join_restriction_without_closing_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 70_001)
    queue_service = MatchingQueueService(session_factory)
    queue_service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)
    restriction_service = PlayerAccessRestrictionService(session_factory)

    restriction_service.restrict_player_access(
        player.id,
        PlayerAccessRestrictionType.QUEUE_JOIN,
        PlayerAccessRestrictionDuration.SEVEN_DAYS,
        admin_discord_user_id=80_001,
        reason="queue abuse",
    )

    session.expire_all()
    restriction = session.scalar(
        select(PlayerAccessRestriction).where(PlayerAccessRestriction.player_id == player.id)
    )
    queue_entry = session.scalar(
        select(MatchQueueEntry).where(MatchQueueEntry.player_id == player.id)
    )

    assert restriction is not None
    assert restriction.restriction_type == PlayerAccessRestrictionType.QUEUE_JOIN
    assert restriction.created_by_admin_discord_user_id == 80_001
    assert restriction.revoked_at is None
    assert restriction.reason == "queue abuse"
    assert restriction.expires_at is not None
    assert restriction.expires_at - restriction.created_at == timedelta(days=7)
    assert queue_entry is not None
    assert queue_entry.status == MatchQueueEntryStatus.WAITING
    assert queue_entry.removal_reason is None
    assert queue_entry.removed_at is None


def test_restrict_player_access_keeps_active_match_spectators_and_unrestricts(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=70_100,
    )
    spectator = create_player(session, 70_200)
    match_service = MatchFlowService(session_factory)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    match_service.spectate_match(match_id, spectator.id)

    restriction_service.restrict_player_access(
        spectator.id,
        PlayerAccessRestrictionType.SPECTATE,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=80_002,
        reason="spectate abuse",
    )
    unrestrict_result = restriction_service.unrestrict_player_access(
        spectator.id,
        PlayerAccessRestrictionType.SPECTATE,
        admin_discord_user_id=80_003,
    )

    session.expire_all()
    spectator_row = session.scalar(
        select(MatchSpectator).where(
            MatchSpectator.match_id == match_id,
            MatchSpectator.player_id == spectator.id,
        )
    )
    restriction = session.scalar(
        select(PlayerAccessRestriction).where(PlayerAccessRestriction.player_id == spectator.id)
    )

    assert unrestrict_result.revoked is True
    assert spectator_row is not None
    assert spectator_row.status == MatchSpectatorStatus.ACTIVE
    assert spectator_row.removed_at is None
    assert spectator_row.removal_reason is None
    assert restriction is not None
    assert restriction.restriction_type == PlayerAccessRestrictionType.SPECTATE
    assert restriction.expires_at is None
    assert restriction.revoked_at is not None
    assert restriction.revoked_by_admin_discord_user_id == 80_003


def test_restrict_player_access_rejects_duplicate_active_restriction(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 70_300)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        player.id,
        PlayerAccessRestrictionType.QUEUE_JOIN,
        PlayerAccessRestrictionDuration.ONE_DAY,
        admin_discord_user_id=80_004,
    )

    with pytest.raises(PlayerAccessRestrictionAlreadyExistsError):
        restriction_service.restrict_player_access(
            player.id,
            PlayerAccessRestrictionType.QUEUE_JOIN,
            PlayerAccessRestrictionDuration.THREE_DAYS,
            admin_discord_user_id=80_005,
        )
