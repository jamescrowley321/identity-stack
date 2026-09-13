"""Make tenant_resources.(tenant_id, name) unique.

app/routers/tenants.py:create_tenant_resource answers a duplicate name with 409 by
catching IntegrityError, but no constraint ever raised one — the duplicate insert
succeeded and returned 200, leaving the 409 branch unreachable. Scoped to the
tenant so two tenants may each hold a resource of the same name.
"""

from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005_tenant_resource_unique_name"
down_revision: Union[str, None] = "004_tenant_external_org_id"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_CONSTRAINT = "uq_tenant_resources_tenant_id_name"


def upgrade() -> None:
    # Existing duplicates would fail the constraint. Keep the earliest-created row
    # of each (tenant_id, name) group — the one the route would have accepted had
    # the constraint been present — and drop the rest. id breaks ties, since
    # created_at is a string with second resolution and can collide.
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
    op.create_unique_constraint(_CONSTRAINT, "tenant_resources", ["tenant_id", "name"])


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "tenant_resources", type_="unique")
