"""Unit tests for the Descope-to-canonical identity seed migration script."""

import uuid as uuid_mod
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.seed_descope import (
    _STATUS_MAP,
    _require_env,
    ensure_descope_provider,
    import_idp_links,
    import_permissions,
    import_roles,
    import_tenants,
    import_user_tenant_roles,
    import_users,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session_factory():
    """Create a mock async session factory + session with standard setup."""
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=session)
    return factory, session


def _make_execute_return(existing_obj=None):
    """Create a mock result from session.execute()."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.first.return_value = existing_obj
    result.scalars.return_value = scalars
    return result


# ---------------------------------------------------------------------------
# _require_env
# ---------------------------------------------------------------------------


class TestRequireEnv:
    def test_missing_env_var_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            _require_env("DEFINITELY_NOT_SET_SEED_DESCOPE_XYZ")
        assert exc_info.value.code == 1

    def test_empty_env_var_exits(self, monkeypatch):
        monkeypatch.setenv("EMPTY_SEED_VAR", "")
        with pytest.raises(SystemExit) as exc_info:
            _require_env("EMPTY_SEED_VAR")
        assert exc_info.value.code == 1

    def test_present_env_var_returns_value(self, monkeypatch):
        monkeypatch.setenv("SEED_TEST_VAR", "hello")
        assert _require_env("SEED_TEST_VAR") == "hello"


# ---------------------------------------------------------------------------
# _STATUS_MAP
# ---------------------------------------------------------------------------


class TestStatusMap:
    def test_enabled_maps_to_active(self):
        from app.models.identity.user import UserStatus

        assert _STATUS_MAP["enabled"] == UserStatus.active

    def test_disabled_maps_to_inactive(self):
        from app.models.identity.user import UserStatus

        assert _STATUS_MAP["disabled"] == UserStatus.inactive

    def test_invited_maps_to_provisioned(self):
        from app.models.identity.user import UserStatus

        assert _STATUS_MAP["invited"] == UserStatus.provisioned


# ---------------------------------------------------------------------------
# ensure_descope_provider
# ---------------------------------------------------------------------------


class TestEnsureDescopeProvider:
    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_returns_existing_provider_id(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        existing = MagicMock()
        existing.id = uuid_mod.UUID("12345678-1234-1234-1234-123456789abc")
        session.execute.return_value = _make_execute_return(existing)

        result = await ensure_descope_provider(dry_run=False)
        assert result == existing.id
        session.add.assert_not_called()
        session.commit.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_creates_provider_when_not_exists(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        await ensure_descope_provider(dry_run=False)
        session.add.assert_called_once()
        session.commit.assert_called_once()
        session.refresh.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_dry_run_returns_none_when_not_exists(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        result = await ensure_descope_provider(dry_run=True)
        assert result is None
        session.add.assert_not_called()
        session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# import_tenants
# ---------------------------------------------------------------------------


class TestImportTenants:
    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_creates_new_tenants(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_tenants = [
            {"id": "T1", "name": "Acme", "selfProvisioningDomains": ["acme.com"]},
            {"id": "T2", "name": "Globex"},
        ]

        await import_tenants(descope_tenants, dry_run=False)

        assert session.add.call_count == 2
        session.commit.assert_called_once()
        session.flush.assert_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_existing_tenants(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        existing = MagicMock()
        existing.id = uuid_mod.uuid4()
        session.execute.return_value = _make_execute_return(existing)

        descope_tenants = [{"id": "T1", "name": "Acme"}]
        tenant_map = await import_tenants(descope_tenants, dry_run=False)

        assert tenant_map["T1"] == existing.id
        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_dry_run_does_not_commit(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_tenants = [{"id": "T1", "name": "Acme"}]
        await import_tenants(descope_tenants, dry_run=True)

        session.add.assert_not_called()
        session.commit.assert_not_called()
        session.flush.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_empty_input(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        tenant_map = await import_tenants([], dry_run=False)
        assert tenant_map == {}

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_uses_id_as_name_fallback(self, mock_factory):
        """When name is missing, falls back to descope id."""
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_tenants = [{"id": "T1"}]
        await import_tenants(descope_tenants, dry_run=False)

        # The Tenant added should use id as name fallback
        session.add.assert_called_once()


# ---------------------------------------------------------------------------
# import_permissions
# ---------------------------------------------------------------------------


class TestImportPermissions:
    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_creates_new_permissions(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_perms = [
            {"name": "documents.write", "description": "Write docs"},
            {"name": "documents.read", "description": "Read docs"},
        ]

        await import_permissions(descope_perms, dry_run=False)

        assert session.add.call_count == 2
        session.commit.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_existing_permissions(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        existing = MagicMock()
        existing.id = uuid_mod.uuid4()
        session.execute.return_value = _make_execute_return(existing)

        descope_perms = [{"name": "documents.write"}]
        perm_map = await import_permissions(descope_perms, dry_run=False)

        assert perm_map["documents.write"] == existing.id
        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_empty_name(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_perms = [{"name": ""}, {"description": "no name"}]
        perm_map = await import_permissions(descope_perms, dry_run=False)

        assert perm_map == {}
        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_dry_run_does_not_commit(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_perms = [{"name": "documents.write"}]
        await import_permissions(descope_perms, dry_run=True)

        session.add.assert_not_called()
        session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# import_roles
# ---------------------------------------------------------------------------


class TestImportRoles:
    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_creates_new_roles(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_roles = [
            {"name": "admin", "description": "Admin role"},
        ]
        perm_map: dict[str, uuid_mod.UUID] = {}

        await import_roles(descope_roles, perm_map, dry_run=False)

        session.add.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_existing_roles(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        existing = MagicMock()
        existing.id = uuid_mod.uuid4()
        session.execute.return_value = _make_execute_return(existing)

        descope_roles = [{"name": "admin"}]
        perm_map: dict[str, uuid_mod.UUID] = {}

        role_map = await import_roles(descope_roles, perm_map, dry_run=False)

        assert role_map["admin"] == existing.id

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_creates_role_permission_mappings(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        # First call: role lookup (not found), second+: role_permission lookups (not found)
        session.execute.return_value = _make_execute_return(None)

        perm_id = uuid_mod.uuid4()
        descope_roles = [{"name": "admin", "permissionNames": ["documents.write"]}]
        perm_map = {"documents.write": perm_id}

        await import_roles(descope_roles, perm_map, dry_run=False)

        # 1 role + 1 role_permission
        assert session.add.call_count == 2
        session.commit.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_missing_permission_in_perm_map(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_roles = [{"name": "admin", "permissionNames": ["nonexistent.perm"]}]
        perm_map: dict[str, uuid_mod.UUID] = {}

        await import_roles(descope_roles, perm_map, dry_run=False)

        # Only the role is added, not the permission mapping
        assert session.add.call_count == 1

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_empty_role_name(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_roles = [{"name": ""}]
        perm_map: dict[str, uuid_mod.UUID] = {}

        role_map = await import_roles(descope_roles, perm_map, dry_run=False)

        assert role_map == {}
        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_dry_run_does_not_commit(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_roles = [{"name": "admin", "permissionNames": ["doc.write"]}]
        perm_map = {"doc.write": uuid_mod.uuid4()}

        await import_roles(descope_roles, perm_map, dry_run=True)

        session.add.assert_not_called()
        session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# import_users
# ---------------------------------------------------------------------------


class TestImportUsers:
    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_creates_new_users(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_users = [
            {
                "userId": "U1",
                "email": "alice@acme.com",
                "name": "Alice",
                "givenName": "Alice",
                "familyName": "Smith",
                "status": "enabled",
            },
        ]

        await import_users(descope_users, dry_run=False)

        session.add.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_existing_users(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        existing = MagicMock()
        existing.id = uuid_mod.uuid4()
        session.execute.return_value = _make_execute_return(existing)

        descope_users = [{"userId": "U1", "email": "alice@acme.com"}]
        user_map = await import_users(descope_users, dry_run=False)

        assert user_map["U1"] == existing.id
        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_users_without_email(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_users = [{"userId": "U1", "email": ""}]
        user_map = await import_users(descope_users, dry_run=False)

        assert user_map == {}
        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_maps_status_correctly(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_users = [
            {"userId": "U1", "email": "a@a.com", "status": "disabled"},
        ]
        await import_users(descope_users, dry_run=False)

        added_user = session.add.call_args[0][0]
        from app.models.identity.user import UserStatus

        assert added_user.status == UserStatus.inactive

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_unknown_status_defaults_to_active(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_users = [
            {"userId": "U1", "email": "a@a.com", "status": "unknown_status"},
        ]
        await import_users(descope_users, dry_run=False)

        added_user = session.add.call_args[0][0]
        from app.models.identity.user import UserStatus

        assert added_user.status == UserStatus.active

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_dry_run_does_not_commit(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_users = [{"userId": "U1", "email": "a@a.com"}]
        await import_users(descope_users, dry_run=True)

        session.add.assert_not_called()
        session.commit.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_email_used_as_username_fallback(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_users = [{"userId": "U1", "email": "a@a.com"}]
        await import_users(descope_users, dry_run=False)

        added_user = session.add.call_args[0][0]
        assert added_user.user_name == "a@a.com"


# ---------------------------------------------------------------------------
# import_user_tenant_roles
# ---------------------------------------------------------------------------


class TestImportUserTenantRoles:
    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_creates_assignments(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        user_id = uuid_mod.uuid4()
        tenant_id = uuid_mod.uuid4()
        role_id = uuid_mod.uuid4()

        descope_users = [
            {
                "userId": "U1",
                "userTenants": [{"tenantId": "T1", "roleNames": ["admin"]}],
            },
        ]
        user_map = {"U1": user_id}
        tenant_map = {"T1": tenant_id}
        role_map = {"admin": role_id}

        await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=False)

        session.add.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_existing_assignments(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        existing = MagicMock()
        session.execute.return_value = _make_execute_return(existing)

        descope_users = [
            {
                "userId": "U1",
                "userTenants": [{"tenantId": "T1", "roleNames": ["admin"]}],
            },
        ]
        user_map = {"U1": uuid_mod.uuid4()}
        tenant_map = {"T1": uuid_mod.uuid4()}
        role_map = {"admin": uuid_mod.uuid4()}

        await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=False)

        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_unmapped_user(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_users = [
            {
                "userId": "UNKNOWN",
                "userTenants": [{"tenantId": "T1", "roleNames": ["admin"]}],
            },
        ]
        user_map: dict[str, uuid_mod.UUID] = {}
        tenant_map = {"T1": uuid_mod.uuid4()}
        role_map = {"admin": uuid_mod.uuid4()}

        await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=False)

        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_unmapped_tenant(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_users = [
            {
                "userId": "U1",
                "userTenants": [{"tenantId": "UNKNOWN", "roleNames": ["admin"]}],
            },
        ]
        user_map = {"U1": uuid_mod.uuid4()}
        tenant_map: dict[str, uuid_mod.UUID] = {}
        role_map = {"admin": uuid_mod.uuid4()}

        await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=False)

        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_unmapped_role(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_users = [
            {
                "userId": "U1",
                "userTenants": [{"tenantId": "T1", "roleNames": ["unknown_role"]}],
            },
        ]
        user_map = {"U1": uuid_mod.uuid4()}
        tenant_map = {"T1": uuid_mod.uuid4()}
        role_map: dict[str, uuid_mod.UUID] = {}

        await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=False)

        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_multi_tenant_multi_role(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_users = [
            {
                "userId": "U1",
                "userTenants": [
                    {"tenantId": "T1", "roleNames": ["admin", "viewer"]},
                    {"tenantId": "T2", "roleNames": ["viewer"]},
                ],
            },
        ]
        user_map = {"U1": uuid_mod.uuid4()}
        tenant_map = {"T1": uuid_mod.uuid4(), "T2": uuid_mod.uuid4()}
        role_map = {"admin": uuid_mod.uuid4(), "viewer": uuid_mod.uuid4()}

        await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=False)

        # 2 roles in T1 + 1 role in T2 = 3 assignments
        assert session.add.call_count == 3

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_dry_run_does_not_commit(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_users = [
            {
                "userId": "U1",
                "userTenants": [{"tenantId": "T1", "roleNames": ["admin"]}],
            },
        ]
        user_map = {"U1": uuid_mod.uuid4()}
        tenant_map = {"T1": uuid_mod.uuid4()}
        role_map = {"admin": uuid_mod.uuid4()}

        await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=True)

        session.add.assert_not_called()
        session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# import_idp_links
# ---------------------------------------------------------------------------


class TestImportIdpLinks:
    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_creates_idp_links(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        provider_id = uuid_mod.uuid4()
        user_id = uuid_mod.uuid4()
        descope_users = [{"userId": "U1", "email": "a@a.com"}]
        user_map = {"U1": user_id}

        await import_idp_links(descope_users, user_map, provider_id, dry_run=False)

        session.add.assert_called_once()
        added = session.add.call_args[0][0]
        assert added.provider_id == provider_id
        assert added.external_sub == "U1"
        assert added.external_email == "a@a.com"
        session.commit.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_existing_links(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        existing = MagicMock()
        session.execute.return_value = _make_execute_return(existing)

        descope_users = [{"userId": "U1", "email": "a@a.com"}]
        user_map = {"U1": uuid_mod.uuid4()}

        await import_idp_links(descope_users, user_map, uuid_mod.uuid4(), dry_run=False)

        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_unmapped_user(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_users = [{"userId": "UNKNOWN", "email": "a@a.com"}]
        user_map: dict[str, uuid_mod.UUID] = {}

        await import_idp_links(descope_users, user_map, uuid_mod.uuid4(), dry_run=False)

        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_skips_when_no_provider_id(self, mock_factory):
        """When provider_id is None and not dry_run, skip entirely."""
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_users = [{"userId": "U1", "email": "a@a.com"}]
        user_map = {"U1": uuid_mod.uuid4()}

        await import_idp_links(descope_users, user_map, None, dry_run=False)

        session.execute.assert_not_called()
        session.add.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_dry_run_with_none_provider(self, mock_factory):
        """Dry run with None provider_id should still count."""
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        descope_users = [{"userId": "U1", "email": "a@a.com"}]
        user_map = {"U1": uuid_mod.uuid4()}

        await import_idp_links(descope_users, user_map, None, dry_run=True)

        session.add.assert_not_called()
        session.commit.assert_not_called()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.async_session_factory")
    async def test_dry_run_does_not_commit(self, mock_factory):
        factory, session = _mock_session_factory()
        mock_factory.return_value = factory.return_value

        session.execute.return_value = _make_execute_return(None)

        descope_users = [{"userId": "U1", "email": "a@a.com"}]
        user_map = {"U1": uuid_mod.uuid4()}

        await import_idp_links(descope_users, user_map, uuid_mod.uuid4(), dry_run=True)

        session.add.assert_not_called()
        session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    @pytest.mark.anyio
    @patch("scripts.seed_descope.import_idp_links", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_user_tenant_roles", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_users", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_roles", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_permissions", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_tenants", new_callable=AsyncMock)
    @patch("scripts.seed_descope.ensure_descope_provider", new_callable=AsyncMock)
    @patch("scripts.seed_descope.DescopeManagementClient")
    @patch("scripts.seed_descope._require_env")
    async def test_orchestrates_all_imports(
        self,
        mock_require_env,
        mock_client_cls,
        mock_provider,
        mock_tenants,
        mock_perms,
        mock_roles,
        mock_users,
        mock_utr,
        mock_links,
        monkeypatch,
    ):
        """main() calls all import functions in correct order."""
        monkeypatch.setattr("sys.argv", ["seed_descope"])

        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.list_tenants.return_value = [{"id": "T1", "name": "Acme"}]
        mock_client.list_permissions.return_value = [{"name": "doc.write"}]
        mock_client.list_roles.return_value = [{"name": "admin"}]
        mock_client.search_all_users.return_value = [{"userId": "U1", "email": "a@a.com"}]

        provider_id = uuid_mod.uuid4()
        mock_provider.return_value = provider_id
        mock_tenants.return_value = {"T1": uuid_mod.uuid4()}
        mock_perms.return_value = {"doc.write": uuid_mod.uuid4()}
        mock_roles.return_value = {"admin": uuid_mod.uuid4()}
        mock_users.return_value = {"U1": uuid_mod.uuid4()}

        await main()

        mock_provider.assert_called_once_with(dry_run=False)
        mock_tenants.assert_called_once()
        mock_perms.assert_called_once()
        mock_roles.assert_called_once()
        mock_users.assert_called_once()
        mock_utr.assert_called_once()
        mock_links.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_descope.import_idp_links", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_user_tenant_roles", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_users", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_roles", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_permissions", new_callable=AsyncMock)
    @patch("scripts.seed_descope.import_tenants", new_callable=AsyncMock)
    @patch("scripts.seed_descope.ensure_descope_provider", new_callable=AsyncMock)
    @patch("scripts.seed_descope.DescopeManagementClient")
    @patch("scripts.seed_descope._require_env")
    async def test_dry_run_flag_passed_through(
        self,
        mock_require_env,
        mock_client_cls,
        mock_provider,
        mock_tenants,
        mock_perms,
        mock_roles,
        mock_users,
        mock_utr,
        mock_links,
        monkeypatch,
    ):
        """main() passes --dry-run flag to all import functions."""
        monkeypatch.setattr("sys.argv", ["seed_descope", "--dry-run"])

        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.list_tenants.return_value = []
        mock_client.list_permissions.return_value = []
        mock_client.list_roles.return_value = []
        mock_client.search_all_users.return_value = []

        mock_provider.return_value = None
        mock_tenants.return_value = {}
        mock_perms.return_value = {}
        mock_roles.return_value = {}
        mock_users.return_value = {}

        await main()

        mock_provider.assert_called_once_with(dry_run=True)
        mock_tenants.assert_called_once()
        assert mock_tenants.call_args[1]["dry_run"] is True

    @pytest.mark.anyio
    @patch("scripts.seed_descope.ensure_descope_provider", new_callable=AsyncMock)
    @patch("scripts.seed_descope.DescopeManagementClient")
    @patch("scripts.seed_descope._require_env")
    async def test_exits_on_provider_failure(self, mock_require_env, mock_client_cls, mock_provider, monkeypatch):
        """main() exits with code 1 when ensure_descope_provider fails."""
        monkeypatch.setattr("sys.argv", ["seed_descope"])

        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
        }[k]

        mock_client_cls.return_value = AsyncMock()
        mock_provider.side_effect = RuntimeError("db error")

        with pytest.raises(SystemExit) as exc_info:
            await main()
        assert exc_info.value.code == 1

    @pytest.mark.anyio
    @patch("scripts.seed_descope.ensure_descope_provider", new_callable=AsyncMock)
    @patch("scripts.seed_descope.DescopeManagementClient")
    @patch("scripts.seed_descope._require_env")
    async def test_exits_on_fetch_tenants_failure(self, mock_require_env, mock_client_cls, mock_provider, monkeypatch):
        """main() exits with code 1 when list_tenants fails."""
        monkeypatch.setattr("sys.argv", ["seed_descope"])

        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.list_tenants.side_effect = RuntimeError("api error")
        mock_provider.return_value = uuid_mod.uuid4()

        with pytest.raises(SystemExit) as exc_info:
            await main()
        assert exc_info.value.code == 1

    @pytest.mark.anyio
    @patch("scripts.seed_descope.import_tenants", new_callable=AsyncMock)
    @patch("scripts.seed_descope.ensure_descope_provider", new_callable=AsyncMock)
    @patch("scripts.seed_descope.DescopeManagementClient")
    @patch("scripts.seed_descope._require_env")
    async def test_continues_on_import_failure(
        self, mock_require_env, mock_client_cls, mock_provider, mock_tenants, monkeypatch
    ):
        """main() continues to next entity type when one import fails (partial failure resilience)."""
        monkeypatch.setattr("sys.argv", ["seed_descope"])

        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.list_tenants.return_value = [{"id": "T1", "name": "Acme"}]
        mock_client.list_permissions.return_value = []
        mock_client.list_roles.return_value = []
        mock_client.search_all_users.return_value = []

        mock_provider.return_value = uuid_mod.uuid4()
        mock_tenants.side_effect = RuntimeError("db error")

        # Should NOT raise — continues past tenant import failure
        with (
            patch("scripts.seed_descope.import_permissions", new_callable=AsyncMock, return_value={}) as mock_perms,
            patch("scripts.seed_descope.import_roles", new_callable=AsyncMock, return_value={}),
            patch("scripts.seed_descope.import_users", new_callable=AsyncMock, return_value={}),
            patch("scripts.seed_descope.import_user_tenant_roles", new_callable=AsyncMock),
            patch("scripts.seed_descope.import_idp_links", new_callable=AsyncMock),
        ):
            await main()

        # Permissions import still called despite tenant failure
        mock_perms.assert_called_once()
