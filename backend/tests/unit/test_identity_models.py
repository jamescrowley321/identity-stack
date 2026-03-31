"""Unit tests for canonical identity SQLModel models (Story 1.2).

AC-1.2.4: SQLModel models in backend/app/models/identity/
"""

import uuid
from datetime import datetime

from sqlmodel import SQLModel


class TestUserModel:
    """AC-1.2.4: User model has SCIM-aligned fields."""

    def test_user_table_name(self):
        from app.models.identity.user import User

        assert User.__tablename__ == "users"

    def test_user_has_uuid_pk(self):
        from app.models.identity.user import User

        u = User(email="test@example.com", user_name="tuser")
        assert isinstance(u.id, uuid.UUID)

    def test_user_scim_fields_present(self):
        from app.models.identity.user import User

        u = User(
            email="test@example.com",
            user_name="tuser",
            given_name="Test",
            family_name="User",
        )
        assert u.email == "test@example.com"
        assert u.user_name == "tuser"
        assert u.given_name == "Test"
        assert u.family_name == "User"

    def test_user_optional_name_fields_default_none(self):
        from app.models.identity.user import User

        u = User(email="test@example.com", user_name="tuser")
        assert u.given_name is None
        assert u.family_name is None

    def test_user_status_defaults_to_active(self):
        from app.models.identity.user import User

        u = User(email="test@example.com", user_name="tuser")
        assert u.status == "active"

    def test_user_timestamps_auto_generated(self):
        from app.models.identity.user import User

        u = User(email="test@example.com", user_name="tuser")
        assert isinstance(u.created_at, datetime)
        assert isinstance(u.updated_at, datetime)

    def test_user_is_table_model(self):
        from app.models.identity.user import User

        assert hasattr(User, "__table__")

    def test_user_email_unique_constraint(self):
        from app.models.identity.user import User

        constraint_names = [c.name for c in User.__table__.constraints if hasattr(c, "name") and c.name]
        assert "uq_users_email" in constraint_names


class TestIdPLinkModel:
    """AC-1.2.4: IdPLink links canonical user to external provider."""

    def test_idplink_table_name(self):
        from app.models.identity.user import IdPLink

        assert IdPLink.__tablename__ == "idp_links"

    def test_idplink_has_uuid_pk(self):
        from app.models.identity.user import IdPLink

        link = IdPLink(
            user_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
            external_sub="ext-123",
        )
        assert isinstance(link.id, uuid.UUID)

    def test_idplink_required_fields(self):
        from app.models.identity.user import IdPLink

        uid = uuid.uuid4()
        pid = uuid.uuid4()
        link = IdPLink(user_id=uid, provider_id=pid, external_sub="ext-sub")
        assert link.user_id == uid
        assert link.provider_id == pid
        assert link.external_sub == "ext-sub"

    def test_idplink_optional_fields_default_none(self):
        from app.models.identity.user import IdPLink

        link = IdPLink(
            user_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
            external_sub="ext-123",
        )
        assert link.external_email is None
        assert link.link_metadata is None

    def test_idplink_linked_at_auto_generated(self):
        from app.models.identity.user import IdPLink

        link = IdPLink(
            user_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
            external_sub="ext-123",
        )
        assert isinstance(link.linked_at, datetime)

    def test_idplink_unique_constraint_user_provider(self):
        from app.models.identity.user import IdPLink

        constraint_names = [c.name for c in IdPLink.__table__.constraints if hasattr(c, "name") and c.name]
        assert "uq_idp_links_user_provider" in constraint_names

    def test_idplink_metadata_column_maps_to_link_metadata(self):
        """The DB column is 'metadata' but the Python attr is 'link_metadata'."""
        from app.models.identity.user import IdPLink

        col = IdPLink.__table__.c.get("metadata")
        assert col is not None

    def test_idplink_foreign_keys(self):
        from app.models.identity.user import IdPLink

        fk_columns = {col.name for col in IdPLink.__table__.columns if col.foreign_keys}
        assert "user_id" in fk_columns
        assert "provider_id" in fk_columns


class TestTenantModel:
    """AC-1.2.4: Tenant model is canonical, distinct from existing TenantResource."""

    def test_tenant_table_name(self):
        from app.models.identity.tenant import Tenant

        assert Tenant.__tablename__ == "tenants"

    def test_tenant_has_uuid_pk(self):
        from app.models.identity.tenant import Tenant

        t = Tenant(name="Acme Corp")
        assert isinstance(t.id, uuid.UUID)

    def test_tenant_required_fields(self):
        from app.models.identity.tenant import Tenant

        t = Tenant(name="Acme Corp")
        assert t.name == "Acme Corp"

    def test_tenant_domains_defaults_to_empty_list(self):
        from app.models.identity.tenant import Tenant

        t = Tenant(name="Acme Corp")
        assert t.domains == []

    def test_tenant_domains_accepts_list(self):
        from app.models.identity.tenant import Tenant

        t = Tenant(name="Acme Corp", domains=["acme.com", "acme.io"])
        assert t.domains == ["acme.com", "acme.io"]

    def test_tenant_status_defaults_to_active(self):
        from app.models.identity.tenant import Tenant

        t = Tenant(name="Acme Corp")
        assert t.status == "active"

    def test_tenant_timestamps_auto_generated(self):
        from app.models.identity.tenant import Tenant

        t = Tenant(name="Acme Corp")
        assert isinstance(t.created_at, datetime)
        assert isinstance(t.updated_at, datetime)

    def test_tenant_is_distinct_from_tenant_resource(self):
        """Canonical Tenant is a different table from legacy TenantResource."""
        from app.models.identity.tenant import Tenant
        from app.models.tenant import TenantResource

        assert Tenant.__tablename__ != TenantResource.__tablename__


class TestRoleModel:
    """AC-1.2.4: Role model with optional tenant_id for scoping."""

    def test_role_table_name(self):
        from app.models.identity.role import Role

        assert Role.__tablename__ == "roles"

    def test_role_has_uuid_pk(self):
        from app.models.identity.role import Role

        r = Role(name="admin")
        assert isinstance(r.id, uuid.UUID)

    def test_role_tenant_id_defaults_to_none(self):
        """Global roles have tenant_id=None."""
        from app.models.identity.role import Role

        r = Role(name="admin")
        assert r.tenant_id is None

    def test_role_can_be_tenant_scoped(self):
        from app.models.identity.role import Role

        tid = uuid.uuid4()
        r = Role(name="editor", tenant_id=tid)
        assert r.tenant_id == tid

    def test_role_description_optional(self):
        from app.models.identity.role import Role

        r = Role(name="admin")
        assert r.description is None

    def test_role_unique_constraint_name_tenant(self):
        from app.models.identity.role import Role

        constraint_names = [c.name for c in Role.__table__.constraints if hasattr(c, "name") and c.name]
        assert "uq_roles_name_tenant" in constraint_names


class TestPermissionModel:
    """AC-1.2.4: Permission model."""

    def test_permission_table_name(self):
        from app.models.identity.role import Permission

        assert Permission.__tablename__ == "permissions"

    def test_permission_has_uuid_pk(self):
        from app.models.identity.role import Permission

        p = Permission(name="documents.write")
        assert isinstance(p.id, uuid.UUID)

    def test_permission_description_optional(self):
        from app.models.identity.role import Permission

        p = Permission(name="documents.read")
        assert p.description is None


class TestRolePermissionModel:
    """AC-1.2.4: RolePermission is many-to-many junction table."""

    def test_role_permission_table_name(self):
        from app.models.identity.role import RolePermission

        assert RolePermission.__tablename__ == "role_permissions"

    def test_role_permission_composite_pk(self):
        from app.models.identity.role import RolePermission

        pk_cols = {col.name for col in RolePermission.__table__.primary_key.columns}
        assert pk_cols == {"role_id", "permission_id"}

    def test_role_permission_foreign_keys(self):
        from app.models.identity.role import RolePermission

        fk_columns = {col.name for col in RolePermission.__table__.columns if col.foreign_keys}
        assert "role_id" in fk_columns
        assert "permission_id" in fk_columns


class TestUserTenantRoleModel:
    """AC-1.2.4: UserTenantRole assignment with audit trail."""

    def test_user_tenant_role_table_name(self):
        from app.models.identity.assignment import UserTenantRole

        assert UserTenantRole.__tablename__ == "user_tenant_roles"

    def test_user_tenant_role_composite_pk(self):
        from app.models.identity.assignment import UserTenantRole

        pk_cols = {col.name for col in UserTenantRole.__table__.primary_key.columns}
        assert pk_cols == {"user_id", "tenant_id", "role_id"}

    def test_user_tenant_role_required_fields(self):
        from app.models.identity.assignment import UserTenantRole

        uid, tid, rid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        utr = UserTenantRole(user_id=uid, tenant_id=tid, role_id=rid)
        assert utr.user_id == uid
        assert utr.tenant_id == tid
        assert utr.role_id == rid

    def test_user_tenant_role_assigned_by_optional(self):
        from app.models.identity.assignment import UserTenantRole

        utr = UserTenantRole(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            role_id=uuid.uuid4(),
        )
        assert utr.assigned_by is None

    def test_user_tenant_role_assigned_at_auto_generated(self):
        from app.models.identity.assignment import UserTenantRole

        utr = UserTenantRole(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            role_id=uuid.uuid4(),
        )
        assert isinstance(utr.assigned_at, datetime)

    def test_user_tenant_role_foreign_keys(self):
        from app.models.identity.assignment import UserTenantRole

        fk_columns = {col.name for col in UserTenantRole.__table__.columns if col.foreign_keys}
        assert fk_columns == {"user_id", "tenant_id", "role_id", "assigned_by"}


class TestProviderModel:
    """AC-1.2.4: Provider model with all required fields."""

    def test_provider_table_name(self):
        from app.models.identity.provider import Provider

        assert Provider.__tablename__ == "providers"

    def test_provider_has_uuid_pk(self):
        from app.models.identity.provider import Provider

        p = Provider(name="Descope", type="descope")
        assert isinstance(p.id, uuid.UUID)

    def test_provider_required_fields(self):
        from app.models.identity.provider import Provider

        p = Provider(name="Descope", type="descope")
        assert p.name == "Descope"
        assert p.type == "descope"

    def test_provider_optional_urls_default_none(self):
        from app.models.identity.provider import Provider

        p = Provider(name="Descope", type="descope")
        assert p.issuer_url is None
        assert p.base_url is None

    def test_provider_capabilities_defaults_to_empty_list(self):
        from app.models.identity.provider import Provider

        p = Provider(name="Descope", type="descope")
        assert p.capabilities == []

    def test_provider_config_ref_defaults_to_none(self):
        from app.models.identity.provider import Provider

        p = Provider(name="Descope", type="descope")
        assert p.config_ref is None

    def test_provider_active_defaults_to_true(self):
        from app.models.identity.provider import Provider

        p = Provider(name="Descope", type="descope")
        assert p.active is True

    def test_provider_timestamps_auto_generated(self):
        from app.models.identity.provider import Provider

        p = Provider(name="Descope", type="descope")
        assert isinstance(p.created_at, datetime)
        assert isinstance(p.updated_at, datetime)

    def test_provider_accepts_all_type_values(self):
        """Provider type accepts various IdP strings."""
        from app.models.identity.provider import Provider

        for provider_type in ("descope", "ory", "entra", "cognito", "oidc"):
            p = Provider(name=f"Test-{provider_type}", type=provider_type)
            assert p.type == provider_type


class TestIdentityModuleExports:
    """identity/__init__.py re-exports all models."""

    def test_all_models_exported(self):
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

        # Verify they are actual SQLModel table models
        for model in (User, IdPLink, Tenant, Role, Permission, RolePermission, UserTenantRole, Provider):
            assert issubclass(model, SQLModel)
            assert hasattr(model, "__table__")

    def test_all_list_matches_exports(self):
        import app.models.identity as identity_mod

        expected = {"User", "IdPLink", "Tenant", "Role", "Permission", "RolePermission", "UserTenantRole", "Provider"}
        assert set(identity_mod.__all__) == expected


class TestModelIdGeneration:
    """All UUID PKs are generated via uuid4, not sequential or external."""

    def test_each_user_gets_unique_id(self):
        from app.models.identity.user import User

        u1 = User(email="a@b.com", user_name="u1")
        u2 = User(email="c@d.com", user_name="u2")
        assert u1.id != u2.id

    def test_each_tenant_gets_unique_id(self):
        from app.models.identity.tenant import Tenant

        t1 = Tenant(name="T1")
        t2 = Tenant(name="T2")
        assert t1.id != t2.id

    def test_each_role_gets_unique_id(self):
        from app.models.identity.role import Role

        r1 = Role(name="admin")
        r2 = Role(name="viewer")
        assert r1.id != r2.id

    def test_each_provider_gets_unique_id(self):
        from app.models.identity.provider import Provider

        p1 = Provider(name="P1", type="descope")
        p2 = Provider(name="P2", type="ory")
        assert p1.id != p2.id
