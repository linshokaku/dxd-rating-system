from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values
from dxd_rating.platform.db.models.match_result_enums import MatchResult

if TYPE_CHECKING:
    from dxd_rating.platform.db.models.match import Match


class MatchAdminOverride(Base):
    __tablename__ = "match_admin_overrides"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), nullable=False, index=True)
    admin_discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_final_result: Mapped[MatchResult | None] = mapped_column(
        SQLAlchemyEnum(
            MatchResult,
            name="match_admin_override_previous_result",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    new_final_result: Mapped[MatchResult] = mapped_column(
        SQLAlchemyEnum(
            MatchResult,
            name="match_admin_override_new_result",
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

    match: Mapped[Match] = relationship(back_populates="admin_overrides")
