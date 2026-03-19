from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values
from bot.models.match_result_enums import MatchReportInputResult, MatchResult

if TYPE_CHECKING:
    from bot.models.match import Match
    from bot.models.player import Player


class MatchReport(Base):
    __tablename__ = "match_reports"
    __table_args__ = (
        Index(
            "uq_match_reports_latest_per_player",
            "match_id",
            "player_id",
            unique=True,
            postgresql_where=text("is_latest"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False, index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    reported_input_result: Mapped[MatchReportInputResult] = mapped_column(
        SQLAlchemyEnum(
            MatchReportInputResult,
            name="match_report_input_result",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    normalized_result: Mapped[MatchResult] = mapped_column(
        SQLAlchemyEnum(
            MatchResult,
            name="match_report_normalized_result",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_latest: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    match: Mapped[Match] = relationship(back_populates="reports")
    player: Mapped[Player] = relationship(back_populates="match_reports")
