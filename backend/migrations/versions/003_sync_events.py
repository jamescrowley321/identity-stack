"""Create sync_events append-only log table.

Revision ID: 003_sync_events
Revises: 002_canonical_identity
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


def _assert_postgresql() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError(f"This migration requires PostgreSQL, got dialect '{bind.dialect.name}'.")


revision: str = "003_sync_events"
down_revision: Union[str, None] = "002_canonical_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _assert_postgresql()
    sync_event_verb = sa.Enum(
        "created",
        "updated",
        "deleted",
        "linked",
        "skipped",
        "failed",
        name="sync_event_verb",
    )
    op.create_table(
        "sync_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("providers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("verb", sync_event_verb, nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False, server_default=""),
        sa.Column("external_sub", sa.String(), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_events_provider_id", "sync_events", ["provider_id"])
    op.create_index("ix_sync_events_verb", "sync_events", ["verb"])
    op.create_index("ix_sync_events_occurred_at", "sync_events", ["occurred_at"])


def downgrade() -> None:
    _assert_postgresql()
    op.drop_index("ix_sync_events_occurred_at", table_name="sync_events")
    op.drop_index("ix_sync_events_verb", table_name="sync_events")
    op.drop_index("ix_sync_events_provider_id", table_name="sync_events")
    op.drop_table("sync_events")
    op.execute("DROP TYPE IF EXISTS sync_event_verb")
