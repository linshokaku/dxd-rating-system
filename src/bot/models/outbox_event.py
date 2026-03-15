from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, String
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from bot.models.base import Base
from bot.models.enum_utils import enum_values


class OutboxEventType(StrEnum):
    PRESENCE_REMINDER = "presence_reminder"
    QUEUE_EXPIRED = "queue_expired"
    MATCH_CREATED = "match_created"


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_type: Mapped[OutboxEventType] = mapped_column(
        SQLAlchemyEnum(
            OutboxEventType,
            name="outbox_event_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=enum_values,
        ),
        nullable=False,
        index=True,
    )
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
