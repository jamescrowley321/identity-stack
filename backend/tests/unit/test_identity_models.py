"""Unit tests for canonical identity SQLModel models.

Verifies model metadata, field definitions, constraints, enums, and table args
without requiring a database connection.
"""

import uuid as uuid_mod
from datetime import datetime

import sqlalchemy as sa
from sqlmodel import SQLModel

from app.models.identity import (
    IdPLink,
    Permission,
    Provider,
    Role,
    RolePermission,
    Tenant,
    User,
    UserTenantRole,
)
from app.models.identity.provider import ProviderType
from app.models.identity.tenant import TenantStatus
from app.models.identity.user import UserStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _col(model, name: str) -> sa.Column:
    """Get a SQLAlchemy column from a SQLModel table."""
    return model.__table__.columns[name]


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestEnums:
    def test_user_status_values(self):
        assert set(UserStatus) == {UserStatus.active, UserStatus.inactive, UserStatus.provisioned}

    def test_tenant_status_values(self):
        assert set(TenantStatus) == {TenantStatus.active, TenantStatus.suspended}

    def test_provider_type_values(self):
        assert set(ProviderType) == {
            ProviderType.descope,
            ProviderType.ory,
            ProviderType.entra,
            ProviderType.cognito,
            ProviderType.oidc,
        }

    def test_enums_are_str_enums(self):
        """Enums should be str subclasses for JSON serialization."""
        assert isinstance(UserStatus.active, str)
        assert isinstance(TenantStatus.active, str)
        assert isinstance(ProviderType.descope, str)


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------


class TestUserModel:
    def test_tablename(self):
        assert User.__tablename__ == "users"

    def test_primary_key_is_uuid(self):
        col = _col(User, "id")
        assert col.primary_key
        assert isinstance(col.type, sa.Uuid)

    def test_email_field(self):
        col = _col(User, "email")
        assert not col.nullable
        assert col.unique

    def test_scim_fields_present(self):
        """SCIM Core User fields: user_name, given_name, family_name (D13)."""
        cols = User.__table__.columns
        for name in ("user_name", "given_name", "family_name"):
            assert name in cols, f"Missing SCIM field: {name}"

    def test_status_column(self):
        col = _col(User, "status")
        assert not col.nullable

    def test_timestamps(self):
        for name in ("created_at", "updated_at"):
            col = _col(User, name)
            assert not col.nullable
            assert isinstance(col.type, sa.DateTime)
            assert col.type.timezone is True

    def test_default_factory_generates_uuid(self):
        user = User(email="a@b.com", user_name="ab")
        assert isinstance(user.id, uuid_mod.UUID)

    def test_default_status_is_active(self):
        user = User(email="a@b.com", user_name="ab")
        assert user.status == UserStatus.active

    def test_default_timestamps_are_utc(self):
        user = User(email="a@b.com", user_name="ab")
        assert isinstance(user.created_at, datetime)
        assert user.created_at.tzinfo is not None


# ---------------------------------------------------------------------------
# IdPLink model
# ---------------------------------------------------------------------------


class TestIdPLinkModel:
    def test_tablename(self):
        assert IdPLink.__tablename__ == "idp_links"

    def test_primary_key_is_uuid(self):
        col = _col(IdPLink, "id")
        assert col.primary_key
        assert isinstance(col.type, sa.Uuid)

    def test_user_fk(self):
        col = _col(IdPLink, "user_id")
        assert not col.nullable
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "users.id"

    def test_provider_fk(self):
        col = _col(IdPLink, "provider_id")
        assert not col.nullable
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "providers.id"

    def test_external_sub_indexed(self):
        col = _col(IdPLink, "external_sub")
        assert col.index

    def test_unique_constraint_user_provider(self):
        constraints = {c.name for c in IdPLink.__table__.constraints if isinstance(c, sa.UniqueConstraint)}
        assert "uq_idp_links_user_provider" in constraints

    def test_metadata_nullable(self):
        col = _col(IdPLink, "metadata")
        assert col.nullable


# ---------------------------------------------------------------------------
# Tenant model
# ---------------------------------------------------------------------------


class TestTenantModel:
    def test_tablename(self):
        assert Tenant.__tablename__ == "tenants"

    def test_primary_key_is_uuid(self):
        col = _col(Tenant, "id")
        assert col.primary_key
        assert isinstance(col.type, sa.Uuid)

    def test_domains_is_json(self):
        col = _col(Tenant, "domains")
        assert isinstance(col.type, sa.JSON)
        assert not col.nullable

    def test_default_domains_empty_list(self):
        tenant = Tenant(name="Test")
        assert tenant.domains == []

    def test_default_status_is_active(self):
        tenant = Tenant(name="Test")
        assert tenant.status == TenantStatus.active


# ---------------------------------------------------------------------------
# Role model
# ---------------------------------------------------------------------------


class TestRoleModel:
    def test_tablename(self):
        assert Role.__tablename__ == "roles"

    def test_tenant_id_nullable(self):
        """tenant_id=NULL means global role."""
        col = _col(Role, "tenant_id")
        assert col.nullable

    def test_tenant_fk(self):
        col = _col(Role, "tenant_id")
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "tenants.id"

    def test_unique_constraint_name_tenant(self):
        constraints = {c.name for c in Role.__table__.constraints if isinstance(c, sa.UniqueConstraint)}
        assert "uq_roles_name_tenant" in constraints


# ---------------------------------------------------------------------------
# Permission model
# ---------------------------------------------------------------------------


class TestPermissionModel:
    def test_tablename(self):
        assert Permission.__tablename__ == "permissions"

    def test_name_unique(self):
        col = _col(Permission, "name")
        assert col.unique

    def test_primary_key_is_uuid(self):
        col = _col(Permission, "id")
        assert col.primary_key
        assert isinstance(col.type, sa.Uuid)


# ---------------------------------------------------------------------------
# RolePermission model
# ---------------------------------------------------------------------------


class TestRolePermissionModel:
    def test_tablename(self):
        assert RolePermission.__tablename__ == "role_permissions"

    def test_composite_primary_key(self):
        pk_cols = {c.name for c in RolePermission.__table__.primary_key.columns}
        assert pk_cols == {"role_id", "permission_id"}

    def test_role_fk(self):
        col = _col(RolePermission, "role_id")
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "roles.id"

    def test_permission_fk(self):
        col = _col(RolePermission, "permission_id")
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "permissions.id"


# ---------------------------------------------------------------------------
# UserTenantRole model
# ---------------------------------------------------------------------------


class TestUserTenantRoleModel:
    def test_tablename(self):
        assert UserTenantRole.__tablename__ == "user_tenant_roles"

    def test_composite_primary_key(self):
        pk_cols = {c.name for c in UserTenantRole.__table__.primary_key.columns}
        assert pk_cols == {"user_id", "tenant_id", "role_id"}

    def test_user_fk(self):
        col = _col(UserTenantRole, "user_id")
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "users.id"

    def test_tenant_fk(self):
        col = _col(UserTenantRole, "tenant_id")
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "tenants.id"

    def test_role_fk(self):
        col = _col(UserTenantRole, "role_id")
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "roles.id"

    def test_assigned_by_nullable(self):
        col = _col(UserTenantRole, "assigned_by")
        assert col.nullable

    def test_assigned_by_fk_to_users(self):
        col = _col(UserTenantRole, "assigned_by")
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "users.id"

    def test_composite_index_user_tenant(self):
        indexes = {idx.name for idx in UserTenantRole.__table__.indexes}
        assert "ix_user_tenant_roles_user_tenant" in indexes

    def test_unique_constraint(self):
        constraints = {c.name for c in UserTenantRole.__table__.constraints if isinstance(c, sa.UniqueConstraint)}
        assert "uq_user_tenant_roles_user_tenant_role" in constraints


# ---------------------------------------------------------------------------
# Provider model
# ---------------------------------------------------------------------------


class TestProviderModel:
    def test_tablename(self):
        assert Provider.__tablename__ == "providers"

    def test_primary_key_is_uuid(self):
        col = _col(Provider, "id")
        assert col.primary_key
        assert isinstance(col.type, sa.Uuid)

    def test_name_unique(self):
        col = _col(Provider, "name")
        assert col.unique

    def test_capabilities_is_json(self):
        col = _col(Provider, "capabilities")
        assert isinstance(col.type, sa.JSON)

    def test_config_ref_present(self):
        """config_ref points to Infisical — never stores credentials."""
        col = _col(Provider, "config_ref")
        assert not col.nullable

    def test_active_default_true(self):
        provider = Provider(name="test", type=ProviderType.descope)
        assert provider.active is True

    def test_default_capabilities_empty_list(self):
        provider = Provider(name="test", type=ProviderType.descope)
        assert provider.capabilities == []


# ---------------------------------------------------------------------------
# Package __init__ re-exports
# ---------------------------------------------------------------------------


class TestPackageExports:
    def test_all_models_exported(self):
        from app.models.identity import __all__

        expected = {"IdPLink", "Permission", "Provider", "Role", "RolePermission", "Tenant", "User", "UserTenantRole"}
        assert set(__all__) == expected

    def test_all_8_canonical_tables_in_metadata(self):
        """AC-1.2.3: 8 canonical tables exist in SQLModel metadata."""
        canonical_tables = {
            "users",
            "tenants",
            "roles",
            "permissions",
            "role_permissions",
            "user_tenant_roles",
            "idp_links",
            "providers",
        }
        metadata_tables = set(SQLModel.metadata.tables.keys())
        assert canonical_tables.issubset(metadata_tables)
