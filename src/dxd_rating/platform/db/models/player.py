from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dxd_rating.platform.db.models.base import Base

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.active_match_player_state import ActiveMatchPlayerState
    from dxd_rating.platform.db.models.finalized_match_player_result import (
        FinalizedMatchPlayerResult,
    )
    from dxd_rating.platform.db.models.match_participant import MatchParticipant
    from dxd_rating.platform.db.models.match_queue_entry import MatchQueueEntry
    from dxd_rating.platform.db.models.match_report import MatchReport
    from dxd_rating.platform.db.models.match_spectator import MatchSpectator
    from dxd_rating.platform.db.models.player_access_restriction import PlayerAccessRestriction
    from dxd_rating.platform.db.models.player_format_stats import PlayerFormatStats
    from dxd_rating.platform.db.models.player_info_thread_binding import PlayerInfoThreadBinding
    from dxd_rating.platform.db.models.player_penalty import PlayerPenalty
    from dxd_rating.platform.db.models.player_penalty_adjustment import PlayerPenaltyAdjustment


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    format_stats: Mapped[list[PlayerFormatStats]] = relationship(
        back_populates="player",
        cascade="all, delete-orphan",
    )
    match_queue_entries: Mapped[list[MatchQueueEntry]] = relationship(back_populates="player")
    match_participants: Mapped[list[MatchParticipant]] = relationship(back_populates="player")
    match_reports: Mapped[list[MatchReport]] = relationship(back_populates="player")
    match_spectators: Mapped[list[MatchSpectator]] = relationship(back_populates="player")
    active_match_player_states: Mapped[list[ActiveMatchPlayerState]] = relationship(
        back_populates="player"
    )
    finalized_match_player_results: Mapped[list[FinalizedMatchPlayerResult]] = relationship(
        back_populates="player"
    )
    access_restrictions: Mapped[list[PlayerAccessRestriction]] = relationship(
        back_populates="player"
    )
    penalties: Mapped[list[PlayerPenalty]] = relationship(back_populates="player")
    penalty_adjustments: Mapped[list[PlayerPenaltyAdjustment]] = relationship(
        back_populates="player"
    )
    info_thread_binding: Mapped[PlayerInfoThreadBinding | None] = relationship(
        back_populates="player",
        cascade="all, delete-orphan",
        uselist=False,
    )
