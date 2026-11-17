"""Add users.email_from_organization.

Marks a canonical user whose email came from a Zitadel organization's first login.
An organization's administrators can give their users any email and mark it
verified, so no first login of any provider may link to such a user by email.
Existing users were not created that way: the column defaults to false.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_user_email_from_organization"
down_revision: Union[str, None] = "005_tenant_resource_unique_name"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_from_organization", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("users", "email_from_organization")
