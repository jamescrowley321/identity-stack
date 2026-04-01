"""Create 8 canonical identity tables.

Revision ID: 002_canonical_identity
Revises: 001_baseline
Create Date: 2026-04-01

This migration uses PostgreSQL-specific features (enum types, partial indexes).
The identity-stack architecture mandates PostgreSQL (D2).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


def _assert_postgresql() -> None:
    """Fail fast if running against a non-PostgreSQL database."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            f"This migration requires PostgreSQL, got dialect '{bind.dialect.name}'. "
            "The identity-stack architecture mandates PostgreSQL (D2)."
        )


revision: str = "002_canonical_identity"
down_revision: Union[str, None] = "001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _assert_postgresql()
    # --- Enum types (use raw SQL for offline-mode compatibility) ---
    op.execute("CREATE TYPE IF NOT EXISTS userstatus AS ENUM ('active', 'inactive', 'provisioned')")
    op.execute("CREATE TYPE IF NOT EXISTS tenantstatus AS ENUM ('active', 'suspended')")
    op.execute("CREATE TYPE IF NOT EXISTS providertype AS ENUM ('descope', 'ory', 'entra', 'cognito', 'oidc')")

    userstatus = sa.Enum("active", "inactive", "provisioned", name="userstatus", create_type=False)
    tenantstatus = sa.Enum("active", "suspended", name="tenantstatus", create_type=False)
    providertype = sa.Enum("descope", "ory", "entra", "cognito", "oidc", name="providertype", create_type=False)

    # --- 1. users ---
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("user_name", sa.String(), nullable=False),
        sa.Column("given_name", sa.String(), nullable=False, server_default=""),
        sa.Column("family_name", sa.String(), nullable=False, server_default=""),
        sa.Column("status", userstatus, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # --- 2. tenants ---
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("domains", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", tenantstatus, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- 3. providers ---
    op.create_table(
        "providers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", providertype, nullable=False),
        sa.Column("issuer_url", sa.String(), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(), nullable=False, server_default=""),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("config_ref", sa.String(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_providers_name"),
    )
    op.create_index("ix_providers_type", "providers", ["type"])

    # --- 4. roles ---
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "tenant_id", name="uq_roles_name_tenant"),
    )
    op.create_index("ix_roles_tenant_id", "roles", ["tenant_id"])
    # Partial unique index: enforce unique global role names where tenant_id IS NULL
    op.execute("CREATE UNIQUE INDEX ix_roles_name_global ON roles (name) WHERE tenant_id IS NULL")

    # --- 5. permissions ---
    op.create_table(
        "permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_permissions_name"),
    )
    op.create_index("ix_permissions_name", "permissions", ["name"])

    # --- 6. role_permissions ---
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id",
            sa.Uuid(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "permission_id",
            sa.Uuid(),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )

    # --- 7. user_tenant_roles ---
    op.create_table(
        "user_tenant_roles",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.Uuid(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assigned_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("user_id", "tenant_id", "role_id"),
    )
    op.create_index("ix_user_tenant_roles_user_tenant", "user_tenant_roles", ["user_id", "tenant_id"])

    # --- 8. idp_links ---
    op.create_table(
        "idp_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_sub", sa.String(), nullable=False),
        sa.Column("external_email", sa.String(), nullable=False, server_default=""),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider_id", name="uq_idp_links_user_provider"),
        sa.UniqueConstraint("provider_id", "external_sub", name="uq_idp_links_provider_external_sub"),
    )
    op.create_index("ix_idp_links_external_sub", "idp_links", ["external_sub"])


def downgrade() -> None:
    _assert_postgresql()
    # Drop tables in reverse dependency order
    op.drop_index("ix_idp_links_external_sub", table_name="idp_links")
    op.drop_table("idp_links")

    op.drop_index("ix_user_tenant_roles_user_tenant", table_name="user_tenant_roles")
    op.drop_table("user_tenant_roles")

    op.drop_table("role_permissions")

    op.drop_index("ix_permissions_name", table_name="permissions")
    op.drop_table("permissions")

    op.execute("DROP INDEX IF EXISTS ix_roles_name_global")
    op.drop_index("ix_roles_tenant_id", table_name="roles")
    op.drop_table("roles")

    op.drop_index("ix_providers_type", table_name="providers")
    op.drop_table("providers")

    op.drop_table("tenants")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    # Drop enum types (use raw SQL for offline-mode compatibility)
    op.execute("DROP TYPE IF EXISTS providertype")
    op.execute("DROP TYPE IF EXISTS tenantstatus")
    op.execute("DROP TYPE IF EXISTS userstatus")
