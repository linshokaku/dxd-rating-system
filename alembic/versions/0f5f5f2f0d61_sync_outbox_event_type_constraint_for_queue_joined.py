"""sync outbox event type constraint for queue joined

Revision ID: 0f5f5f2f0d61
Revises: a5d3c328f06b
Create Date: 2026-04-18 22:09:30.000000
"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0f5f5f2f0d61"
down_revision: str | None = "a5d3c328f06b"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_OUTBOX_EVENT_TYPE_WITH_QUEUE_JOINED = (
    "event_type IN ("
    "'queue_joined', "
    "'presence_reminder', "
    "'queue_expired', "
    "'match_created', "
    "'match_parent_assigned', "
    "'match_report_opened', "
    "'match_approval_requested', "
    "'match_finalized', "
    "'match_admin_review_required', "
    "'season_completed', "
    "'season_top_rankings', "
    "'admin_operations_notification'"
    ")"
)

_OUTBOX_EVENT_TYPE_WITHOUT_QUEUE_JOINED = (
    "event_type IN ("
    "'presence_reminder', "
    "'queue_expired', "
    "'match_created', "
    "'match_parent_assigned', "
    "'match_report_opened', "
    "'match_approval_requested', "
    "'match_finalized', "
    "'match_admin_review_required', "
    "'season_completed', "
    "'season_top_rankings', "
    "'admin_operations_notification'"
    ")"
)


def upgrade() -> None:
    op.drop_constraint("outbox_event_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "outbox_event_type",
        "outbox_events",
        _OUTBOX_EVENT_TYPE_WITH_QUEUE_JOINED,
    )


def downgrade() -> None:
    op.drop_constraint("outbox_event_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "outbox_event_type",
        "outbox_events",
        _OUTBOX_EVENT_TYPE_WITHOUT_QUEUE_JOINED,
    )
