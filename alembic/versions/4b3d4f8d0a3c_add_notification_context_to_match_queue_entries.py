"""add notification context to match queue entries

Revision ID: 4b3d4f8d0a3c
Revises: ad6d2d0e0d2b
Create Date: 2026-03-15 20:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b3d4f8d0a3c"
down_revision: str | None = "ad6d2d0e0d2b"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "match_queue_entries",
        sa.Column("notification_channel_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "match_queue_entries",
        sa.Column("notification_guild_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "match_queue_entries",
        sa.Column("notification_mention_discord_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "match_queue_entries",
        sa.Column("notification_recorded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("match_queue_entries", "notification_recorded_at")
    op.drop_column("match_queue_entries", "notification_mention_discord_user_id")
    op.drop_column("match_queue_entries", "notification_guild_id")
    op.drop_column("match_queue_entries", "notification_channel_id")
