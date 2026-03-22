from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, func
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values
from dxd_rating.platform.db.models.match_format import MatchFormat


class LeaderboardSnapshot(Base):
    __tablename__ = "leaderboard_snapshots"

    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
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
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    games_played: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
