from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values
from bot.models.match_result_enums import MatchResult, MatchState

if TYPE_CHECKING:
    from bot.models.active_match_player_state import ActiveMatchPlayerState
    from bot.models.match import Match
    from bot.models.player import Player


class ActiveMatchState(Base):
    __tablename__ = "active_match_states"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    parent_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parent_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id"),
        nullable=True,
        index=True,
    )
    parent_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    report_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reporting_opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    report_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provisional_result: Mapped[MatchResult | None] = mapped_column(
        SQLAlchemyEnum(
            MatchResult,
            name="active_match_state_provisional_result",
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
    admin_review_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
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
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_by_admin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    match: Mapped[Match] = relationship(back_populates="active_state")
    parent_player: Mapped[Player | None] = relationship()
    player_states: Mapped[list[ActiveMatchPlayerState]] = relationship(
        back_populates="active_state",
        cascade="all, delete-orphan",
        overlaps="match,active_player_states",
    )
