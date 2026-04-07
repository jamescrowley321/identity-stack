"""Unit tests for ReconciliationService domain orchestration (Story 3.2).

Tests cover:
- run: full reconciliation pass, advisory lock, Descope fetch failure, commit failure
- _reconcile_tenants: create, update, skip empty name, conflict on create
- _reconcile_permissions: create, update, skip empty name, conflict on create
- _reconcile_roles: create, update, skip empty name, conflict on create
- _reconcile_users: create user + IdP link, update user, status mapping,
  name splitting, skip missing email/userId, provider not configured,
  conflict on create/update, duplicate link skip
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors.identity import ProviderError
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Permission, Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import User, UserStatus
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import RepositoryConflictError, UserRepository
from app.services.descope import DescopeManagementClient
from app.services.reconciliation import ReconciliationService


@asynccontextmanager
async def _noop_nested():
    """Mock for session.begin_nested() — savepoint that lets exceptions propagate."""
    yield


PROVIDER_ID = uuid.uuid4()


def _make_provider(**overrides) -> Provider:
    defaults = {"id": PROVIDER_ID, "name": "descope", "type": ProviderType.descope}
    defaults.update(overrides)
    return Provider(**defaults)


def _make_user(**overrides) -> User:
    defaults = {
        "id": uuid.uuid4(),
        "email": "alice@example.com",
        "user_name": "alice@example.com",
        "given_name": "Alice",
        "family_name": "Smith",
        "status": UserStatus.active,
    }
    defaults.update(overrides)
    return User(**defaults)


def _make_tenant(**overrides) -> Tenant:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"tenant-{uuid.uuid4().hex[:8]}",
        "domains": [],
        "status": TenantStatus.active,
    }
    defaults.update(overrides)
    return Tenant(**defaults)


def _make_role(**overrides) -> Role:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"role-{uuid.uuid4().hex[:8]}",
        "description": "A role",
        "tenant_id": None,
    }
    defaults.update(overrides)
    return Role(**defaults)


def _make_permission(**overrides) -> Permission:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"perm-{uuid.uuid4().hex[:8]}",
        "description": "A permission",
    }
    defaults.update(overrides)
    return Permission(**defaults)


def _build_service(
    session: AsyncMock | None = None,
    acquire_lock: AsyncMock | None = None,
    descope_client: AsyncMock | None = None,
    user_repo: AsyncMock | None = None,
    role_repo: AsyncMock | None = None,
    perm_repo: AsyncMock | None = None,
    tenant_repo: AsyncMock | None = None,
    link_repo: AsyncMock | None = None,
    provider_repo: AsyncMock | None = None,
) -> tuple[ReconciliationService, dict[str, AsyncMock]]:
    if session is None:
        session = AsyncMock()
        session.begin_nested = _noop_nested
    if acquire_lock is None:
        acquire_lock = AsyncMock()
    if descope_client is None:
        descope_client = AsyncMock(spec=DescopeManagementClient)
    if user_repo is None:
        user_repo = AsyncMock(spec=UserRepository)
    if role_repo is None:
        role_repo = AsyncMock(spec=RoleRepository)
    if perm_repo is None:
        perm_repo = AsyncMock(spec=PermissionRepository)
    if tenant_repo is None:
        tenant_repo = AsyncMock(spec=TenantRepository)
    if link_repo is None:
        link_repo = AsyncMock(spec=IdPLinkRepository)
    if provider_repo is None:
        provider_repo = AsyncMock(spec=ProviderRepository)

    service = ReconciliationService(
        session=session,
        acquire_lock=acquire_lock,
        descope_client=descope_client,
        user_repository=user_repo,
        role_repository=role_repo,
        permission_repository=perm_repo,
        tenant_repository=tenant_repo,
        idp_link_repository=link_repo,
        provider_repository=provider_repo,
    )
    mocks = {
        "session": session,
        "acquire_lock": acquire_lock,
        "descope": descope_client,
        "user_repo": user_repo,
        "role_repo": role_repo,
        "perm_repo": perm_repo,
        "tenant_repo": tenant_repo,
        "link_repo": link_repo,
        "provider_repo": provider_repo,
    }
    return service, mocks


def _empty_descope_state(mocks: dict[str, AsyncMock]) -> None:
    """Configure Descope client to return empty lists for all entity types."""
    mocks["descope"].search_all_users.return_value = []
    mocks["descope"].list_roles.return_value = []
    mocks["descope"].list_permissions.return_value = []
    mocks["descope"].list_tenants.return_value = []


def _empty_canonical_state(mocks: dict[str, AsyncMock]) -> None:
    """Configure repos to return empty lists for all entity types."""
    mocks["tenant_repo"].list_all.return_value = []
    mocks["perm_repo"].list_all.return_value = []
    mocks["role_repo"].list_by_tenant.return_value = []
    mocks["user_repo"].list_all.return_value = []
    mocks["provider_repo"].get_by_type.return_value = _make_provider()


@pytest.mark.anyio
class TestRun:
    """AC-3.2.1 through AC-3.2.5: Full reconciliation orchestration."""

    async def test_run_empty_state_returns_zero_stats(self):
        """No Descope entities, no canonical entities → all stats zero."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)

        result = await service.run()

        assert result.is_ok()
        stats = result.ok["stats"]
        assert all(v == 0 for v in stats.values())
        assert result.ok["status"] == "completed"
        mocks["session"].commit.assert_awaited_once()

    async def test_run_advisory_lock_failure_returns_error(self):
        """AC-3.2.2: Lock acquisition failure → ProviderError, no modifications."""
        lock_fn = AsyncMock(side_effect=RuntimeError("lock unavailable"))
        service, mocks = _build_service(acquire_lock=lock_fn)

        result = await service.run()

        assert result.is_error()
        assert isinstance(result.error, ProviderError)
        assert "lock" in result.error.message.lower()
        # No Descope calls should be made
        mocks["descope"].search_all_users.assert_not_awaited()

    async def test_run_descope_fetch_failure_aborts(self):
        """AC-3.2.3: Descope API failure → ProviderError, no canonical changes."""
        service, mocks = _build_service()
        mocks["descope"].search_all_users.side_effect = RuntimeError("connection refused")

        result = await service.run()

        assert result.is_error()
        assert isinstance(result.error, ProviderError)
        assert "unavailable" in result.error.message.lower()
        # No repo calls should happen
        mocks["tenant_repo"].list_all.assert_not_awaited()
        mocks["session"].commit.assert_not_awaited()

    async def test_run_descope_partial_fetch_failure_aborts(self):
        """If list_tenants succeeds but list_roles fails → still abort."""
        service, mocks = _build_service()
        mocks["descope"].search_all_users.return_value = []
        mocks["descope"].list_roles.side_effect = RuntimeError("timeout")

        result = await service.run()

        assert result.is_error()
        assert isinstance(result.error, ProviderError)
        mocks["session"].commit.assert_not_awaited()

    async def test_run_commit_failure_returns_error(self):
        """Commit failure after successful reconciliation → ProviderError."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["session"].commit.side_effect = RuntimeError("database gone")

        result = await service.run()

        assert result.is_error()
        assert isinstance(result.error, ProviderError)
        assert "commit" in result.error.message.lower()

    async def test_run_creates_tenants_permissions_roles_users(self):
        """Full pass creates all entity types."""
        service, mocks = _build_service()
        mocks["descope"].list_tenants.return_value = [{"name": "Acme", "selfProvisioningDomains": ["acme.com"]}]
        mocks["descope"].list_permissions.return_value = [{"name": "docs.read", "description": "Read docs"}]
        mocks["descope"].list_roles.return_value = [{"name": "admin", "description": "Admin role"}]
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "a@b.com", "name": "Alice Smith", "status": "enabled"}
        ]

        # Canonical state is empty
        mocks["tenant_repo"].list_all.return_value = []
        mocks["perm_repo"].list_all.return_value = []
        mocks["role_repo"].list_by_tenant.return_value = []
        mocks["user_repo"].list_all.return_value = []
        provider = _make_provider()
        mocks["provider_repo"].get_by_type.return_value = provider
        mocks["link_repo"].get_by_provider_and_sub.return_value = None

        result = await service.run()

        assert result.is_ok()
        stats = result.ok["stats"]
        assert stats["tenants_created"] == 1
        assert stats["permissions_created"] == 1
        assert stats["roles_created"] == 1
        assert stats["users_created"] == 1
        assert stats["links_created"] == 1
        mocks["session"].commit.assert_awaited_once()

    async def test_run_reconciles_in_dependency_order(self):
        """Tenants → permissions → roles → users (dependency order)."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)

        call_order: list[str] = []
        mocks["tenant_repo"].list_all.side_effect = lambda: (call_order.append("tenants"), [])[-1]
        mocks["perm_repo"].list_all.side_effect = lambda: (call_order.append("permissions"), [])[-1]
        mocks["role_repo"].list_by_tenant.side_effect = lambda tenant_id: (call_order.append("roles"), [])[-1]
        mocks["user_repo"].list_all.side_effect = lambda: (call_order.append("users"), [])[-1]
        mocks["provider_repo"].get_by_type.return_value = _make_provider()

        await service.run()

        assert call_order == ["tenants", "permissions", "roles", "users"]


@pytest.mark.anyio
class TestReconcileTenants:
    """AC-3.2.1: Tenant drift detection and resolution."""

    async def test_create_new_tenant(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_tenants.return_value = [{"name": "Acme", "selfProvisioningDomains": ["acme.com"]}]
        mocks["tenant_repo"].list_all.return_value = []

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["tenants_created"] == 1
        mocks["tenant_repo"].create.assert_awaited_once()
        created = mocks["tenant_repo"].create.call_args[0][0]
        assert created.name == "Acme"
        assert created.domains == ["acme.com"]

    async def test_update_tenant_domains(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_tenant(name="Acme", domains=["old.com"])
        mocks["descope"].list_tenants.return_value = [{"name": "Acme", "selfProvisioningDomains": ["new.com"]}]
        mocks["tenant_repo"].list_all.return_value = [existing]

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["tenants_updated"] == 1
        assert existing.domains == ["new.com"]
        mocks["tenant_repo"].update.assert_awaited_once()

    async def test_no_change_tenant_not_updated(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_tenant(name="Acme", domains=["acme.com"])
        mocks["descope"].list_tenants.return_value = [{"name": "Acme", "selfProvisioningDomains": ["acme.com"]}]
        mocks["tenant_repo"].list_all.return_value = [existing]

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["tenants_updated"] == 0
        mocks["tenant_repo"].update.assert_not_awaited()

    async def test_skip_empty_tenant_name(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_tenants.return_value = [{"name": "", "selfProvisioningDomains": []}]
        mocks["tenant_repo"].list_all.return_value = []

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["tenants_created"] == 0
        mocks["tenant_repo"].create.assert_not_awaited()

    async def test_tenant_conflict_on_create_skips(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_tenants.return_value = [{"name": "Acme"}]
        mocks["tenant_repo"].list_all.return_value = []
        mocks["tenant_repo"].create.side_effect = RepositoryConflictError("duplicate")

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["tenants_created"] == 0

    async def test_missing_domains_defaults_to_empty_list(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_tenants.return_value = [{"name": "Acme"}]
        mocks["tenant_repo"].list_all.return_value = []

        await service.run()

        created = mocks["tenant_repo"].create.call_args[0][0]
        assert created.domains == []


@pytest.mark.anyio
class TestReconcilePermissions:
    """AC-3.2.1: Permission drift detection and resolution."""

    async def test_create_new_permission(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_permissions.return_value = [{"name": "docs.read", "description": "Read documents"}]
        mocks["perm_repo"].list_all.return_value = []

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["permissions_created"] == 1
        created = mocks["perm_repo"].create.call_args[0][0]
        assert created.name == "docs.read"
        assert created.description == "Read documents"

    async def test_update_permission_description(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_permission(name="docs.read", description="Old desc")
        mocks["descope"].list_permissions.return_value = [{"name": "docs.read", "description": "New desc"}]
        mocks["perm_repo"].list_all.return_value = [existing]

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["permissions_updated"] == 1
        assert existing.description == "New desc"

    async def test_no_change_permission_not_updated(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_permission(name="docs.read", description="Same")
        mocks["descope"].list_permissions.return_value = [{"name": "docs.read", "description": "Same"}]
        mocks["perm_repo"].list_all.return_value = [existing]

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["permissions_updated"] == 0

    async def test_skip_empty_permission_name(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_permissions.return_value = [{"name": ""}]
        mocks["perm_repo"].list_all.return_value = []

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["permissions_created"] == 0

    async def test_permission_conflict_on_create_skips(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_permissions.return_value = [{"name": "docs.read"}]
        mocks["perm_repo"].list_all.return_value = []
        mocks["perm_repo"].create.side_effect = RepositoryConflictError("duplicate")

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["permissions_created"] == 0


@pytest.mark.anyio
class TestReconcileRoles:
    """AC-3.2.1: Role drift detection and resolution (global roles only)."""

    async def test_create_new_role(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_roles.return_value = [{"name": "admin", "description": "Administrator"}]
        mocks["role_repo"].list_by_tenant.return_value = []

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["roles_created"] == 1
        created = mocks["role_repo"].create.call_args[0][0]
        assert created.name == "admin"
        assert created.tenant_id is None  # Global role

    async def test_update_role_description(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_role(name="admin", description="Old")
        mocks["descope"].list_roles.return_value = [{"name": "admin", "description": "New"}]
        mocks["role_repo"].list_by_tenant.return_value = [existing]

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["roles_updated"] == 1
        assert existing.description == "New"

    async def test_no_change_role_not_updated(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_role(name="admin", description="Same")
        mocks["descope"].list_roles.return_value = [{"name": "admin", "description": "Same"}]
        mocks["role_repo"].list_by_tenant.return_value = [existing]

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["roles_updated"] == 0

    async def test_skip_empty_role_name(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_roles.return_value = [{"name": ""}]
        mocks["role_repo"].list_by_tenant.return_value = []

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["roles_created"] == 0

    async def test_role_conflict_on_create_skips(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].list_roles.return_value = [{"name": "admin"}]
        mocks["role_repo"].list_by_tenant.return_value = []
        mocks["role_repo"].create.side_effect = RepositoryConflictError("duplicate")

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["roles_created"] == 0


@pytest.mark.anyio
class TestReconcileUsers:
    """AC-3.2.1 + AC-3.2.5: User drift detection, resolution, and IdP link management."""

    async def test_create_new_user_with_idp_link(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        new_user = _make_user(email="alice@example.com")
        mocks["user_repo"].create.return_value = new_user
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "alice@example.com", "name": "Alice Smith", "status": "enabled"}
        ]
        mocks["user_repo"].list_all.return_value = []
        mocks["link_repo"].get_by_provider_and_sub.return_value = None

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_created"] == 1
        assert result.ok["stats"]["links_created"] == 1
        mocks["user_repo"].create.assert_awaited_once()
        mocks["link_repo"].create.assert_awaited_once()

    async def test_update_existing_user_name(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_user(email="alice@example.com", given_name="Old", family_name="Name")
        mocks["descope"].search_all_users.return_value = [
            {
                "userId": "ext-1",
                "email": "alice@example.com",
                "givenName": "New",
                "familyName": "Name2",
                "status": "enabled",
            }
        ]
        mocks["user_repo"].list_all.return_value = [existing]
        mocks["link_repo"].get_by_provider_and_sub.return_value = MagicMock()  # Existing link

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_updated"] == 1
        assert existing.given_name == "New"
        assert existing.family_name == "Name2"

    async def test_update_user_status_disabled_to_inactive(self):
        """Descope disabled → canonical inactive."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_user(email="alice@example.com", status=UserStatus.active)
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "alice@example.com", "status": "disabled"}
        ]
        mocks["user_repo"].list_all.return_value = [existing]
        mocks["link_repo"].get_by_provider_and_sub.return_value = MagicMock()

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_updated"] == 1
        assert existing.status == UserStatus.inactive

    async def test_update_user_status_enabled_to_active(self):
        """Descope enabled → canonical active."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_user(email="alice@example.com", status=UserStatus.inactive)
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "alice@example.com", "status": "enabled"}
        ]
        mocks["user_repo"].list_all.return_value = [existing]
        mocks["link_repo"].get_by_provider_and_sub.return_value = MagicMock()

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_updated"] == 1
        assert existing.status == UserStatus.active

    async def test_invited_user_mapped_to_provisioned(self):
        """Descope invited → canonical provisioned (not inactive)."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        new_user = _make_user(email="invite@example.com", status=UserStatus.provisioned)
        mocks["user_repo"].create.return_value = new_user
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "invite@example.com", "status": "invited"}
        ]
        mocks["user_repo"].list_all.return_value = []
        mocks["link_repo"].get_by_provider_and_sub.return_value = None

        result = await service.run()

        assert result.is_ok()
        created = mocks["user_repo"].create.call_args[0][0]
        assert created.status == UserStatus.provisioned

    async def test_no_change_user_not_updated(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_user(
            email="alice@example.com",
            given_name="Alice",
            family_name="Smith",
            status=UserStatus.active,
        )
        mocks["descope"].search_all_users.return_value = [
            {
                "userId": "ext-1",
                "email": "alice@example.com",
                "givenName": "Alice",
                "familyName": "Smith",
                "status": "enabled",
            }
        ]
        mocks["user_repo"].list_all.return_value = [existing]
        mocks["link_repo"].get_by_provider_and_sub.return_value = MagicMock()

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_updated"] == 0
        mocks["user_repo"].update.assert_not_awaited()

    async def test_name_splitting_from_full_name(self):
        """Descope user with name but no givenName/familyName → split name."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        new_user = _make_user()
        mocks["user_repo"].create.return_value = new_user
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "a@b.com", "name": "Jane Doe", "status": "enabled"}
        ]
        mocks["user_repo"].list_all.return_value = []
        mocks["link_repo"].get_by_provider_and_sub.return_value = None

        await service.run()

        created = mocks["user_repo"].create.call_args[0][0]
        assert created.given_name == "Jane"
        assert created.family_name == "Doe"

    async def test_name_splitting_single_word(self):
        """Single-word name → given_name only, family_name empty."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        new_user = _make_user()
        mocks["user_repo"].create.return_value = new_user
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "a@b.com", "name": "Cher", "status": "enabled"}
        ]
        mocks["user_repo"].list_all.return_value = []
        mocks["link_repo"].get_by_provider_and_sub.return_value = None

        await service.run()

        created = mocks["user_repo"].create.call_args[0][0]
        assert created.given_name == "Cher"
        assert created.family_name == ""

    async def test_skip_user_missing_email(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].search_all_users.return_value = [{"userId": "ext-1", "email": "", "status": "enabled"}]
        mocks["user_repo"].list_all.return_value = []

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_created"] == 0
        mocks["user_repo"].create.assert_not_awaited()

    async def test_skip_user_missing_user_id(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].search_all_users.return_value = [{"userId": "", "email": "a@b.com", "status": "enabled"}]
        mocks["user_repo"].list_all.return_value = []

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_created"] == 0

    async def test_provider_not_configured_skips_users(self):
        """No Descope provider row → skip user reconciliation, return Ok."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["provider_repo"].get_by_type.return_value = None
        mocks["descope"].search_all_users.return_value = [{"userId": "ext-1", "email": "a@b.com", "status": "enabled"}]

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_created"] == 0
        mocks["user_repo"].create.assert_not_awaited()

    async def test_user_conflict_on_create_skips(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        mocks["descope"].search_all_users.return_value = [{"userId": "ext-1", "email": "a@b.com", "status": "enabled"}]
        mocks["user_repo"].list_all.return_value = []
        mocks["user_repo"].create.side_effect = RepositoryConflictError("duplicate")

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_created"] == 0

    async def test_user_conflict_on_update_skips(self):
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_user(email="a@b.com", given_name="Old")
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "a@b.com", "givenName": "New", "status": "enabled"}
        ]
        mocks["user_repo"].list_all.return_value = [existing]
        mocks["user_repo"].update.side_effect = RepositoryConflictError("constraint")

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_updated"] == 0

    async def test_idp_link_conflict_skips(self):
        """Duplicate IdP link on create → skip, continue."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        new_user = _make_user()
        mocks["user_repo"].create.return_value = new_user
        mocks["descope"].search_all_users.return_value = [{"userId": "ext-1", "email": "a@b.com", "status": "enabled"}]
        mocks["user_repo"].list_all.return_value = []
        mocks["link_repo"].get_by_provider_and_sub.return_value = None
        mocks["link_repo"].create.side_effect = RepositoryConflictError("dup link")

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_created"] == 1
        assert result.ok["stats"]["links_created"] == 0

    async def test_existing_link_not_recreated(self):
        """If IdP link already exists → don't create a new one."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        existing = _make_user(email="a@b.com", given_name="Alice", family_name="Smith", status=UserStatus.active)
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "a@b.com", "givenName": "Alice", "familyName": "Smith", "status": "enabled"}
        ]
        mocks["user_repo"].list_all.return_value = [existing]
        mocks["link_repo"].get_by_provider_and_sub.return_value = MagicMock()  # Existing link

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["links_created"] == 0
        mocks["link_repo"].create.assert_not_awaited()

    async def test_multiple_users_processed(self):
        """Multiple Descope users are all reconciled."""
        service, mocks = _build_service()
        _empty_descope_state(mocks)
        _empty_canonical_state(mocks)
        user1 = _make_user(email="a@b.com")
        user2 = _make_user(email="c@d.com")
        mocks["user_repo"].create.side_effect = [user1, user2]
        mocks["descope"].search_all_users.return_value = [
            {"userId": "ext-1", "email": "a@b.com", "status": "enabled"},
            {"userId": "ext-2", "email": "c@d.com", "status": "enabled"},
        ]
        mocks["user_repo"].list_all.return_value = []
        mocks["link_repo"].get_by_provider_and_sub.return_value = None

        result = await service.run()

        assert result.is_ok()
        assert result.ok["stats"]["users_created"] == 2
        assert result.ok["stats"]["links_created"] == 2
