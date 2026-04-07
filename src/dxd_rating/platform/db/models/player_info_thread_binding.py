from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dxd_rating.platform.db.models.base import Base

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.player import Player


class PlayerInfoThreadBinding(Base):
    __tablename__ = "player_info_thread_bindings"

    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    thread_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
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

    player: Mapped[Player] = relationship(back_populates="info_thread_binding")
