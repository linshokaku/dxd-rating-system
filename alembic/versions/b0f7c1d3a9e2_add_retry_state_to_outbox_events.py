"""add retry state to outbox events

Revision ID: b0f7c1d3a9e2
Revises: 4b3d4f8d0a3c
Create Date: 2026-03-15 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0f7c1d3a9e2"
down_revision: str | None = "4b3d4f8d0a3c"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column("outbox_events", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("outbox_events", sa.Column("last_failed_at", sa.DateTime(timezone=True)))
    op.create_index(
        op.f("ix_outbox_events_next_attempt_at"),
        "outbox_events",
        ["next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_outbox_events_next_attempt_at"), table_name="outbox_events")
    op.drop_column("outbox_events", "last_failed_at")
    op.drop_column("outbox_events", "last_error")
    op.drop_column("outbox_events", "next_attempt_at")
    op.drop_column("outbox_events", "failure_count")
