"""Baseline: bring existing Document and TenantResource tables under Alembic control.

Revision ID: 001_baseline
Revises: None
Create Date: 2026-04-01

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
        if_not_exists=True,
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"], if_not_exists=True)

    op.create_table(
        "tenant_resources",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_tenant_resources_tenant_id", "tenant_resources", ["tenant_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_tenant_resources_tenant_id", table_name="tenant_resources")
    op.drop_table("tenant_resources")
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    op.drop_table("documents")
