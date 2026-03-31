"""Baseline existing tables (documents, tenant_resources).

Revision ID: 0001
Revises:
Create Date: 2026-03-31

NOTE: For databases that already have these tables (e.g. existing deployments),
run `alembic stamp 0001` to mark this migration as applied without executing it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])

    op.create_table(
        "tenant_resources",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_resources_tenant_id", "tenant_resources", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_resources_tenant_id", table_name="tenant_resources")
    op.drop_table("tenant_resources")
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    op.drop_table("documents")
