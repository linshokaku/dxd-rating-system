from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values
from dxd_rating.platform.db.models.match_result_enums import MatchResult

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.finalized_match_player_result import (
        FinalizedMatchPlayerResult,
    )
    from dxd_rating.platform.db.models.match import Match
    from dxd_rating.platform.db.models.player import Player


class FinalizedMatchResult(Base):
    __tablename__ = "finalized_match_results"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    team_a_player_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    team_b_player_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    parent_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id"),
        nullable=True,
        index=True,
    )
    parent_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    provisional_result: Mapped[MatchResult | None] = mapped_column(
        SQLAlchemyEnum(
            MatchResult,
            name="finalized_match_provisional_result",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    final_result: Mapped[MatchResult] = mapped_column(
        SQLAlchemyEnum(
            MatchResult,
            name="finalized_match_final_result",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    admin_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    admin_review_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finalized_by_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    match: Mapped[Match] = relationship(back_populates="finalized_result")
    parent_player: Mapped[Player | None] = relationship()
    player_results: Mapped[list[FinalizedMatchPlayerResult]] = relationship(
        back_populates="finalized_result",
        cascade="all, delete-orphan",
        overlaps="match,finalized_player_results",
    )
