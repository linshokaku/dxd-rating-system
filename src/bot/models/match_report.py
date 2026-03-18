from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values
from bot.models.match import MatchResultType

if TYPE_CHECKING:
    from bot.models.match import Match
    from bot.models.player import Player


class MatchReportInput(StrEnum):
    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"
    VOID = "void"


class MatchReport(Base):
    __tablename__ = "match_reports"
    __table_args__ = (
        Index("ix_match_reports_match_player_latest", "match_id", "player_id", "is_latest"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False, index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    reported_input_result: Mapped[MatchReportInput] = mapped_column(
        SQLAlchemyEnum(
            MatchReportInput,
            name="match_report_input",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    normalized_result: Mapped[MatchResultType] = mapped_column(
        SQLAlchemyEnum(
            MatchResultType,
            name="match_result_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    is_latest: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    match: Mapped[Match] = relationship(back_populates="reports")
    player: Mapped[Player] = relationship()
