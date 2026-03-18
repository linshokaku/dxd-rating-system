from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Integer, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.models.base import Base

if TYPE_CHECKING:
    from bot.models.match_participant import MatchParticipant
    from bot.models.match_queue_entry import MatchQueueEntry
    from bot.models.player_penalty import PlayerPenalty

INITIAL_RATING = 1500


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    rating: Mapped[int] = mapped_column(
        Integer, default=INITIAL_RATING, server_default=text(str(INITIAL_RATING))
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    match_queue_entries: Mapped[list[MatchQueueEntry]] = relationship(back_populates="player")
    match_participants: Mapped[list[MatchParticipant]] = relationship(back_populates="player")
    penalties: Mapped[list[PlayerPenalty]] = relationship(
        back_populates="player",
        cascade="all, delete-orphan",
    )
