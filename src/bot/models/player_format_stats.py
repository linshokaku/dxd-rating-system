from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, func, text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.models.base import Base
from bot.models.enum_utils import enum_values
from bot.models.match_format import MatchFormat

if TYPE_CHECKING:
    from bot.models.player import Player

INITIAL_RATING = 1500


class PlayerFormatStats(Base):
    __tablename__ = "player_format_stats"

    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
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
