from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values
from dxd_rating.platform.db.models.match_result_enums import (
    MatchApprovalStatus,
    MatchReportInputResult,
    MatchReportStatus,
    MatchResult,
)

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.active_match_state import ActiveMatchState
    from dxd_rating.platform.db.models.match import Match
    from dxd_rating.platform.db.models.match_report import MatchReport
    from dxd_rating.platform.db.models.player import Player


class ActiveMatchPlayerState(Base):
    __tablename__ = "active_match_player_states"
    __table_args__ = (ForeignKeyConstraint(["match_id"], ["active_match_states.match_id"]),)

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
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
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_report_id: Mapped[int | None] = mapped_column(
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
            name="active_match_player_last_normalized_result",
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

    match: Mapped[Match] = relationship(
        back_populates="active_player_states",
        overlaps="active_state,player_states",
    )
    active_state: Mapped[ActiveMatchState] = relationship(
        back_populates="player_states",
        overlaps="match,active_player_states",
    )
    player: Mapped[Player] = relationship(back_populates="active_match_player_states")
    locked_report: Mapped[MatchReport | None] = relationship()
