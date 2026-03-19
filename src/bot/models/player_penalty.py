from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values
from bot.models.match_result_enums import PenaltyType

if TYPE_CHECKING:
    from bot.models.player import Player


class PlayerPenalty(Base):
    __tablename__ = "player_penalties"

    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    penalty_type: Mapped[PenaltyType] = mapped_column(
        SQLAlchemyEnum(
            PenaltyType,
            name="penalty_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        primary_key=True,
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    player: Mapped[Player] = relationship(back_populates="penalties")
