from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, func, text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values
from dxd_rating.platform.db.models.match_format import MatchFormat

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.player import Player
    from dxd_rating.platform.db.models.season import Season

INITIAL_RATING = 1500


class CarryoverStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"


class PlayerFormatStats(Base):
    __tablename__ = "player_format_stats"

    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), primary_key=True)
    match_format: Mapped[MatchFormat] = mapped_column(
        SQLAlchemyEnum(
            MatchFormat,
            name="match_format",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        primary_key=True,
    )
    rating: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=INITIAL_RATING,
        server_default=text(str(INITIAL_RATING)),
    )
    games_played: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    wins: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    losses: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    draws: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    carryover_status: Mapped[CarryoverStatus] = mapped_column(
        SQLAlchemyEnum(
            CarryoverStatus,
            name="carryover_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        default=CarryoverStatus.PENDING,
        server_default=text(f"'{CarryoverStatus.PENDING.value}'"),
    )
    carryover_source_season_id: Mapped[int | None] = mapped_column(
        ForeignKey("seasons.id"),
        nullable=True,
    )
    carryover_source_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    player: Mapped[Player] = relationship(back_populates="format_stats")
    season: Mapped[Season] = relationship(
        back_populates="player_format_stats",
        foreign_keys=[season_id],
    )
    carryover_source_season: Mapped[Season | None] = relationship(
        foreign_keys=[carryover_source_season_id],
    )
