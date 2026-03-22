from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values
from dxd_rating.platform.db.models.match_format import MatchFormat

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.active_match_player_state import ActiveMatchPlayerState
    from dxd_rating.platform.db.models.active_match_state import ActiveMatchState
    from dxd_rating.platform.db.models.finalized_match_player_result import (
        FinalizedMatchPlayerResult,
    )
    from dxd_rating.platform.db.models.finalized_match_result import FinalizedMatchResult
    from dxd_rating.platform.db.models.match_admin_override import MatchAdminOverride
    from dxd_rating.platform.db.models.match_participant import MatchParticipant
    from dxd_rating.platform.db.models.match_report import MatchReport
    from dxd_rating.platform.db.models.match_spectator import MatchSpectator
    from dxd_rating.platform.db.models.player_penalty_adjustment import PlayerPenaltyAdjustment
    from dxd_rating.platform.db.models.season import Season


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    match_format: Mapped[MatchFormat] = mapped_column(
        SQLAlchemyEnum(
            MatchFormat,
            name="match_format",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        index=True,
    )
    queue_class_id: Mapped[str] = mapped_column(String(length=64), nullable=False, index=True)
    started_season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id"),
        nullable=False,
        index=True,
    )
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
    spectators: Mapped[list[MatchSpectator]] = relationship(
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
    started_season: Mapped[Season] = relationship(back_populates="started_matches")
