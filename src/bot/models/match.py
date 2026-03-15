from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base

if TYPE_CHECKING:
    from bot.models.match_participant import MatchParticipant


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    participants: Mapped[list[MatchParticipant]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )
