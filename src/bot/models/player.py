from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, func, text
from sqlalchemy.orm import Mapped, mapped_column

from bot.models.base import Base

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
