from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, ForeignKeyConstraint, Integer
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.models.base import Base
from bot.models.enum_utils import enum_values
from bot.models.match_participant import MatchParticipantTeam
from bot.models.match_result_enums import (
    MatchApprovalStatus,
    MatchReportInputResult,
    MatchReportStatus,
    MatchResult,
    PenaltyType,
)

if TYPE_CHECKING:
    from bot.models.finalized_match_result import FinalizedMatchResult
    from bot.models.match import Match
    from bot.models.match_report import MatchReport
    from bot.models.player import Player


class FinalizedMatchPlayerResult(Base):
    __tablename__ = "finalized_match_player_results"
    __table_args__ = (ForeignKeyConstraint(["match_id"], ["finalized_match_results.match_id"]),)

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
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
    rating_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    games_played_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wins_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    losses_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draws_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_report_id: Mapped[int | None] = mapped_column(
        ForeignKey("match_reports.id"),
        nullable=True,
        index=True,
    )
    last_reported_input_result: Mapped[MatchReportInputResult | None] = mapped_column(
        SQLAlchemyEnum(
            MatchReportInputResult,
            name="match_report_input_result",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    last_normalized_result: Mapped[MatchResult | None] = mapped_column(
        SQLAlchemyEnum(
            MatchResult,
            name="finalized_match_player_last_normalized_result",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    last_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    report_status: Mapped[MatchReportStatus] = mapped_column(
        SQLAlchemyEnum(
            MatchReportStatus,
            name="match_report_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    approval_status: Mapped[MatchApprovalStatus] = mapped_column(
        SQLAlchemyEnum(
            MatchApprovalStatus,
            name="match_approval_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_penalty_type: Mapped[PenaltyType | None] = mapped_column(
        SQLAlchemyEnum(
            PenaltyType,
            name="penalty_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    auto_penalty_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    match: Mapped[Match] = relationship(
        back_populates="finalized_player_results",
        overlaps="finalized_result,player_results",
    )
    player: Mapped[Player] = relationship(back_populates="finalized_match_player_results")
    finalized_result: Mapped[FinalizedMatchResult] = relationship(
        back_populates="player_results",
        overlaps="match,finalized_player_results",
    )
    latest_report: Mapped[MatchReport | None] = relationship()
