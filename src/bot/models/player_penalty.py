from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.models.base import Base
from bot.models.enum_utils import enum_values

if TYPE_CHECKING:
    from bot.models.player import Player


class PlayerPenaltyType(StrEnum):
    INCORRECT_REPORT = "incorrect_report"
    NOT_REPORTED = "not_reported"
    ROOM_DELAY = "room_delay"
    MATCH_MISTAKE = "match_mistake"
    LATE = "late"
    DISCONNECT = "disconnect"


class PlayerPenalty(Base):
    __tablename__ = "player_penalties"
    __table_args__ = (UniqueConstraint("player_id", "penalty_type"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    penalty_type: Mapped[PlayerPenaltyType] = mapped_column(
        SQLAlchemyEnum(
            PlayerPenaltyType,
            name="player_penalty_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    player: Mapped[Player] = relationship(back_populates="penalties")
