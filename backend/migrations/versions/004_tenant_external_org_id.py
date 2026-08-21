"""Add tenants.external_org_id for mapping canonical tenants to Ory Organizations.

Nullable and unused by default (canonical-side tenancy). Populated only when Ory
Organizations are enabled, so a canonical tenant can be linked to an Ory
Organization id. See the Ory-as-SSO-provider plan (Epic 3, FR-10).
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004_tenant_external_org_id"
down_revision: Union[str, None] = "003_sync_events"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("external_org_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_tenants_external_org_id",
        "tenants",
        ["external_org_id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_tenants_external_org_id", table_name="tenants", if_exists=True)
    op.drop_column("tenants", "external_org_id")
