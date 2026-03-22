from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dxd_rating.platform.db.models.base import Base

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.match import Match
    from dxd_rating.platform.db.models.player_format_stats import PlayerFormatStats


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (
        UniqueConstraint("name"),
        UniqueConstraint("start_at"),
        UniqueConstraint("end_at"),
        CheckConstraint("start_at < end_at", name="ck_seasons_start_before_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(length=64), nullable=False, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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

    started_matches: Mapped[list[Match]] = relationship(back_populates="started_season")
    player_format_stats: Mapped[list[PlayerFormatStats]] = relationship(
        back_populates="season",
        foreign_keys="PlayerFormatStats.season_id",
    )
