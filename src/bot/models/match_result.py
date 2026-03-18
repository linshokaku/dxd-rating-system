from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.models.base import Base
from bot.models.enum_utils import enum_values
from bot.models.match import MatchResultType

if TYPE_CHECKING:
    from bot.models.match import Match


class FinalizedMatchResult(Base):
    __tablename__ = "finalized_match_results"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    team_a_player_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    team_b_player_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    parent_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id"),
        nullable=True,
        index=True,
    )
    parent_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_result: Mapped[MatchResultType] = mapped_column(
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
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    match: Mapped[Match] = relationship(back_populates="finalized_result")
