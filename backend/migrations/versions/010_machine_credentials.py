"""Add machine_credentials and machine_credential_roles: the canonical machine credentials of a tenant.

A machine credential is a client of the sync provider that signs in with the
client-credentials grant (a Keycloak client with a service account). Its row ties
the client and the ``sub`` of its tokens to one tenant, and its roles there are
what authorizes it (conventions §18). Deleting the tenant or a role deletes what
hangs off it.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010_machine_credentials"
down_revision: Union[str, None] = "009_pending_pushes"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "machine_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False, unique=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("registration_token", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.Enum("active", "inactive", name="machine_credential_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "subject", name="uq_machine_credentials_provider_subject"),
    )
    op.create_index("ix_machine_credentials_tenant_id", "machine_credentials", ["tenant_id"])
    op.create_table(
        "machine_credential_roles",
        sa.Column(
            "credential_id",
            sa.Uuid(),
            sa.ForeignKey("machine_credentials.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role_id", sa.Uuid(), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("machine_credential_roles")
    op.drop_index("ix_machine_credentials_tenant_id", table_name="machine_credentials")
    op.drop_table("machine_credentials")
    op.execute("DROP TYPE machine_credential_status")
