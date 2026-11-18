"""Add tenants.external_org_provider_id: the provider whose organization external_org_id names.

``external_org_id`` holds the organization ids of more than one provider (Ory
Organizations, Zitadel organizations) and nothing keeps their formats apart, so
a first login matches a tenant by organization only for its own provider.
Existing links are left with no provider and match no first login: running
``scripts/seed_zitadel.py`` again names the provider for the tenants it links.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007_tenant_external_org_provider"
down_revision: Union[str, None] = "006_user_email_from_organization"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "external_org_provider_id",
            sa.Uuid(),
            sa.ForeignKey("providers.id", name="fk_tenants_external_org_provider_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_tenants_external_org_provider_id", "tenants", type_="foreignkey")
    op.drop_column("tenants", "external_org_provider_id")
