from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values
from dxd_rating.platform.db.models.match_result_enums import PenaltyAdjustmentSource, PenaltyType

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.match import Match
    from dxd_rating.platform.db.models.player import Player


class PlayerPenaltyAdjustment(Base):
    __tablename__ = "player_penalty_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("matches.id"),
        nullable=True,
        index=True,
    )
    penalty_type: Mapped[PenaltyType] = mapped_column(
        SQLAlchemyEnum(
            PenaltyType,
            name="penalty_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        index=True,
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[PenaltyAdjustmentSource] = mapped_column(
        SQLAlchemyEnum(
            PenaltyAdjustmentSource,
            name="penalty_adjustment_source",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    admin_discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    player: Mapped[Player] = relationship(back_populates="penalty_adjustments")
    match: Mapped[Match | None] = relationship(back_populates="penalty_adjustments")
