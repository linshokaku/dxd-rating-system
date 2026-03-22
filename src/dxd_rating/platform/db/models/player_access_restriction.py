from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.player import Player


class PlayerAccessRestrictionType(StrEnum):
    QUEUE_JOIN = "queue_join"
    SPECTATE = "spectate"


class PlayerAccessRestriction(Base):
    __tablename__ = "player_access_restrictions"
    __table_args__ = (
        Index(
            "ix_player_access_restrictions_player_type_created_at",
            "player_id",
            "restriction_type",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    restriction_type: Mapped[PlayerAccessRestrictionType] = mapped_column(
        SQLAlchemyEnum(
            PlayerAccessRestrictionType,
            name="player_access_restriction_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_admin_discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revoked_by_admin_discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    player: Mapped[Player] = relationship(back_populates="access_restrictions")
