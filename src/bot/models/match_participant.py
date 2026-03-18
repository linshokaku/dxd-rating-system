from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values

if TYPE_CHECKING:
    from bot.models.match import Match
    from bot.models.match_queue_entry import MatchQueueEntry
    from bot.models.player import Player


class MatchParticipantTeam(StrEnum):
    TEAM_A = "team_a"
    TEAM_B = "team_b"


class MatchParticipantReportStatus(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    NOT_REPORTED = "not_reported"


class MatchParticipantApprovalStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    NOT_APPROVED = "not_approved"


class MatchParticipant(Base):
    __tablename__ = "match_participants"
    __table_args__ = (
        UniqueConstraint("queue_entry_id"),
        UniqueConstraint("match_id", "player_id"),
        UniqueConstraint("match_id", "team", "slot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False, index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    queue_entry_id: Mapped[int] = mapped_column(
        ForeignKey("match_queue_entries.id"), nullable=False, index=True
    )
    team: Mapped[MatchParticipantTeam] = mapped_column(
        SQLAlchemyEnum(
            MatchParticipantTeam,
            name="match_participant_team",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    report_status: Mapped[MatchParticipantReportStatus | None] = mapped_column(
        SQLAlchemyEnum(
            MatchParticipantReportStatus,
            name="match_participant_report_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    report_status_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_status: Mapped[MatchParticipantApprovalStatus | None] = mapped_column(
        SQLAlchemyEnum(
            MatchParticipantApprovalStatus,
            name="match_participant_approval_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_incorrect_penalty_applied: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    auto_not_reported_penalty_applied: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    match: Mapped[Match] = relationship(back_populates="participants")
    player: Mapped[Player] = relationship(back_populates="match_participants")
    queue_entry: Mapped[MatchQueueEntry] = relationship(back_populates="match_participant")
