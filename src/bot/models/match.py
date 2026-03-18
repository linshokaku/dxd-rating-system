from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values

if TYPE_CHECKING:
    from bot.models.match_participant import MatchParticipant
    from bot.models.match_report import MatchReport
    from bot.models.match_result import FinalizedMatchResult
    from bot.models.player import Player


class MatchState(StrEnum):
    WAITING_FOR_PARENT = "waiting_for_parent"
    WAITING_FOR_RESULT_REPORTS = "waiting_for_result_reports"
    AWAITING_RESULT_APPROVALS = "awaiting_result_approvals"
    FINALIZED = "finalized"


class MatchResultType(StrEnum):
    TEAM_A_WIN = "team_a_win"
    TEAM_B_WIN = "team_b_win"
    DRAW = "draw"
    VOID = "void"


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    state: Mapped[MatchState] = mapped_column(
        SQLAlchemyEnum(
            MatchState,
            name="match_state",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        default=MatchState.WAITING_FOR_PARENT,
    )
    parent_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id"),
        nullable=True,
        index=True,
    )
    parent_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisional_result: Mapped[MatchResultType | None] = mapped_column(
        SQLAlchemyEnum(
            MatchResultType,
            name="match_result_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    admin_review_required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    parent_player: Mapped[Player | None] = relationship(foreign_keys=[parent_player_id])
    participants: Mapped[list[MatchParticipant]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )
    reports: Mapped[list[MatchReport]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )
    finalized_result: Mapped[FinalizedMatchResult | None] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        uselist=False,
    )
