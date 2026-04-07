from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from dxd_rating.platform.db.models.base import Base
from dxd_rating.platform.db.models.enum_utils import enum_values


class OutboxEventType(StrEnum):
    PRESENCE_REMINDER = "presence_reminder"
    QUEUE_EXPIRED = "queue_expired"
    MATCH_CREATED = "match_created"
    MATCH_PARENT_ASSIGNED = "match_parent_assigned"
    MATCH_REPORT_OPENED = "match_report_opened"
    MATCH_APPROVAL_REQUESTED = "match_approval_requested"
    MATCH_FINALIZED = "match_finalized"
    MATCH_ADMIN_REVIEW_REQUIRED = "match_admin_review_required"
    SEASON_COMPLETED = "season_completed"
    SEASON_TOP_RANKINGS = "season_top_rankings"
    ADMIN_OPERATIONS_NOTIFICATION = "admin_operations_notification"


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
    failure_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
