"""Add tenant_resources.tenant_space: whose tenant tenant_id names (#690).

``tenant_id`` held canonical tenant ids and Descope tenant ids in one column,
and a Descope tenant can be created with a custom id equal to a canonical
tenant's UUID: its members were then served that canonical tenant's resources.
Each row now says which id space its tenant id is in, and the routes read and
write only the caller's.

Existing rows: a ``tenant_id`` that is a canonical tenant's id is taken as that
tenant's, and every other as a Descope tenant's. Descope tenant ids are not UUIDs
unless created so, and a canonical tenant's UUID is generated here, so a row is
taken for the wrong space only where the collision this closes was already made.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "012_tenant_resource_id_space"
down_revision: Union[str, None] = "011_seal_registration_tokens"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_OLD_CONSTRAINT = "uq_tenant_resources_tenant_id_name"
_NEW_CONSTRAINT = "uq_tenant_resources_tenant_space_id_name"
_CHECK = "ck_tenant_resources_tenant_space"


def upgrade() -> None:
    op.add_column("tenant_resources", sa.Column("tenant_space", sa.String(16), nullable=True))
    op.execute(
        """
        UPDATE tenant_resources
        SET tenant_space = CASE
            WHEN tenant_id IN (SELECT CAST(id AS VARCHAR) FROM tenants) THEN 'canonical'
            ELSE 'descope'
        END
        """
    )
    op.alter_column("tenant_resources", "tenant_space", nullable=False)
    op.create_check_constraint(_CHECK, "tenant_resources", "tenant_space IN ('canonical', 'descope')")
    op.drop_constraint(_OLD_CONSTRAINT, "tenant_resources", type_="unique")
    op.create_unique_constraint(_NEW_CONSTRAINT, "tenant_resources", ["tenant_space", "tenant_id", "name"])


def downgrade() -> None:
    # Two spaces may each hold a resource of the same name in a tenant of the same
    # id, which the old constraint refuses: keep the earliest-created of each, as
    # 005 did.
    op.execute(
        """
        DELETE FROM tenant_resources
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY tenant_id, name
                           ORDER BY created_at, id
                       ) AS rn
                FROM tenant_resources
            ) AS ranked
            WHERE rn = 1
        )
        """
    )
    op.drop_constraint(_NEW_CONSTRAINT, "tenant_resources", type_="unique")
    op.drop_constraint(_CHECK, "tenant_resources", type_="check")
    op.drop_column("tenant_resources", "tenant_space")
    op.create_unique_constraint(_OLD_CONSTRAINT, "tenant_resources", ["tenant_id", "name"])
