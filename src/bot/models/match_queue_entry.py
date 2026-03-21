from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values
from bot.models.match_format import MatchFormat

if TYPE_CHECKING:
    from bot.models.match_participant import MatchParticipant
    from bot.models.player import Player


class MatchQueueEntryStatus(StrEnum):
    WAITING = "waiting"
    LEFT = "left"
    EXPIRED = "expired"
    MATCHED = "matched"


class MatchQueueRemovalReason(StrEnum):
    USER_LEAVE = "user_leave"
    TIMEOUT = "timeout"


class MatchQueueEntry(Base):
    __tablename__ = "match_queue_entries"
    __table_args__ = (
        Index(
            "uq_match_queue_entries_waiting_player_id",
            "player_id",
            unique=True,
            postgresql_where=text("status = 'waiting'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    match_format: Mapped[MatchFormat] = mapped_column(
        SQLAlchemyEnum(
            MatchFormat,
            name="match_format",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        index=True,
    )
    queue_class_id: Mapped[str] = mapped_column(String(length=64), nullable=False, index=True)
    status: Mapped[MatchQueueEntryStatus] = mapped_column(
        SQLAlchemyEnum(
            MatchQueueEntryStatus,
            name="match_queue_entry_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        server_default=text(f"'{MatchQueueEntryStatus.WAITING.value}'"),
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_present_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    last_reminded_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notification_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notification_guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notification_mention_discord_user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    notification_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removal_reason: Mapped[MatchQueueRemovalReason | None] = mapped_column(
        SQLAlchemyEnum(
            MatchQueueRemovalReason,
            name="match_queue_removal_reason",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=True,
    )

    player: Mapped[Player] = relationship(back_populates="match_queue_entries")
    match_participant: Mapped[MatchParticipant | None] = relationship(back_populates="queue_entry")
