from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base

if TYPE_CHECKING:
    from bot.models.active_match_player_state import ActiveMatchPlayerState
    from bot.models.active_match_state import ActiveMatchState
    from bot.models.finalized_match_player_result import FinalizedMatchPlayerResult
    from bot.models.finalized_match_result import FinalizedMatchResult
    from bot.models.match_admin_override import MatchAdminOverride
    from bot.models.match_participant import MatchParticipant
    from bot.models.match_report import MatchReport
    from bot.models.player_penalty_adjustment import PlayerPenaltyAdjustment


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    participants: Mapped[list[MatchParticipant]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )
    active_state: Mapped[ActiveMatchState | None] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        uselist=False,
    )
    reports: Mapped[list[MatchReport]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
    )
    active_player_states: Mapped[list[ActiveMatchPlayerState]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        overlaps="active_state,player_states",
    )
    finalized_result: Mapped[FinalizedMatchResult | None] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        uselist=False,
    )
    finalized_player_results: Mapped[list[FinalizedMatchPlayerResult]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        overlaps="finalized_result,player_results",
    )
    penalty_adjustments: Mapped[list[PlayerPenaltyAdjustment]] = relationship(
        back_populates="match",
    )
    admin_overrides: Mapped[list[MatchAdminOverride]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
    )
