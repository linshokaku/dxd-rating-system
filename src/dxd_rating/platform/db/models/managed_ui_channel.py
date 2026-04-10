from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values
from dxd_rating.platform.db.models.match_format import MatchFormat


class ManagedUiType(StrEnum):
    REGISTER_PANEL = "register_panel"
    MATCHMAKING_CHANNEL = "matchmaking_channel"
    MATCHMAKING_NEWS_CHANNEL = "matchmaking_news_channel"
    INFO_CHANNEL = "info_channel"
    SYSTEM_ANNOUNCEMENTS_CHANNEL = "system_announcements_channel"
    ADMIN_CONTACT_CHANNEL = "admin_contact_channel"
    ADMIN_OPERATIONS_CHANNEL = "admin_operations_channel"


class ManagedUiChannel(Base):
    __tablename__ = "managed_ui_channels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ui_type: Mapped[ManagedUiType] = mapped_column(
        SQLAlchemyEnum(
            ManagedUiType,
            name="managed_ui_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    status_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    matchmaking_one_v_one_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        unique=True,
    )
    matchmaking_two_v_two_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        unique=True,
    )
    matchmaking_three_v_three_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        unique=True,
    )
    created_by_discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def get_matchmaking_panel_message_id(self, match_format: MatchFormat) -> int | None:
        if match_format is MatchFormat.ONE_VS_ONE:
            return self.matchmaking_one_v_one_message_id
        if match_format is MatchFormat.TWO_VS_TWO:
            return self.matchmaking_two_v_two_message_id
        if match_format is MatchFormat.THREE_VS_THREE:
            return self.matchmaking_three_v_three_message_id

        raise ValueError(f"Unsupported match_format: {match_format}")
