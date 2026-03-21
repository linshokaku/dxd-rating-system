from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values

if TYPE_CHECKING:
    from bot.models.match import Match
    from bot.models.player import Player


class MatchSpectatorStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class MatchSpectator(Base):
    __tablename__ = "match_spectators"
    __table_args__ = (
        Index(
            "uq_match_spectators_active_match_id_player_id",
            "match_id",
            "player_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_match_spectators_match_id_status", "match_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    status: Mapped[MatchSpectatorStatus] = mapped_column(
        SQLAlchemyEnum(
            MatchSpectatorStatus,
            name="match_spectator_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        server_default=text(f"'{MatchSpectatorStatus.ACTIVE.value}'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removal_reason: Mapped[str | None] = mapped_column(String(length=64), nullable=True)

    match: Mapped[Match] = relationship(back_populates="spectators")
    player: Mapped[Player] = relationship(back_populates="match_spectators")
