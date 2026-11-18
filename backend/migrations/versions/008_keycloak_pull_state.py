"""Add keycloak_pull_state: the Keycloak event pull's cursor and what it knows of the realm.

One row per realm (its issuer URL). The pull reads the realm's admin and user
events from the cursors and applies them to the canonical model in the same
transaction that advances them, so a restart resumes where the last commit left
off and applies no event twice.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008_keycloak_pull_state"
down_revision: Union[str, None] = "007_tenant_external_org_provider"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "keycloak_pull_state",
        sa.Column("realm", sa.String(), primary_key=True),
        sa.Column("admin_cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("admin_seen", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("user_cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("user_seen", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("organizations", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("client_roles", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("keycloak_pull_state")
