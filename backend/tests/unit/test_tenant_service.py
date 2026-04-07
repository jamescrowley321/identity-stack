"""Unit tests for TenantService domain orchestration (Story 2.2).

Tests cover:
- create_tenant: persist, commit, sync; duplicate → Conflict; sync fail → Ok
- get_tenant: found → Ok(dict); not found → NotFound
- get_tenant_users_with_roles: success; tenant not found; empty result
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from expression import Error, Ok

from app.errors.identity import Conflict, NotFound
from app.models.identity.tenant import Tenant
from app.repositories.tenant import TenantRepository
from app.repositories.user import RepositoryConflictError
from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.tenant import TenantService


def _make_tenant(**overrides) -> Tenant:
    defaults = {
        "id": uuid.uuid4(),
        "name": "Acme Corp",
        "domains": ["acme.com"],
    }
    defaults.update(overrides)
    return Tenant(**defaults)


def _build_service(
    repo: AsyncMock | None = None,
    adapter: AsyncMock | None = None,
) -> tuple[TenantService, AsyncMock, AsyncMock]:
    if repo is None:
        repo = AsyncMock(spec=TenantRepository)
    if adapter is None:
        adapter = AsyncMock(spec=IdentityProviderAdapter)
    service = TenantService(repository=repo, adapter=adapter)
    return service, repo, adapter


@pytest.mark.anyio
class TestCreateTenant:
    """AC-2.2.3: create_tenant persists via repo, commits, syncs, returns Ok(dict)."""

    async def test_create_tenant_success(self):
        service, repo, adapter = _build_service()
        repo.get_by_name.return_value = None
        tenant = _make_tenant()
        repo.create.return_value = tenant
        adapter.sync_tenant.return_value = Ok(None)

        result = await service.create_tenant(name=tenant.name, domains=tenant.domains)

        assert result.is_ok()
        repo.create.assert_awaited_once()
        repo.commit.assert_awaited_once()
        adapter.sync_tenant.assert_awaited_once()

    async def test_create_tenant_commit_before_sync(self):
        service, repo, adapter = _build_service()
        repo.get_by_name.return_value = None
        tenant = _make_tenant()
        repo.create.return_value = tenant
        adapter.sync_tenant.return_value = Ok(None)

        call_order = []
        repo.commit.side_effect = lambda: call_order.append("commit")
        adapter.sync_tenant.side_effect = lambda **kw: (call_order.append("sync"), Ok(None))[1]

        await service.create_tenant(name=tenant.name)

        assert call_order == ["commit", "sync"]

    async def test_create_tenant_duplicate_name_returns_conflict(self):
        service, repo, _adapter = _build_service()
        repo.get_by_name.return_value = _make_tenant()

        result = await service.create_tenant(name="Acme Corp")

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        repo.create.assert_not_awaited()

    async def test_create_tenant_integrity_error_returns_conflict(self):
        """TOCTOU race: get_by_name returns None but flush raises IntegrityError."""
        service, repo, _adapter = _build_service()
        repo.get_by_name.return_value = None
        repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await service.create_tenant(name="Acme Corp")

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_create_tenant_sync_failure_still_returns_ok(self):
        service, repo, adapter = _build_service()
        repo.get_by_name.return_value = None
        tenant = _make_tenant()
        repo.create.return_value = tenant
        adapter.sync_tenant.return_value = Error(SyncError(message="Descope down", operation="sync_tenant"))

        with patch("app.services.tenant.logger") as mock_logger:
            result = await service.create_tenant(name=tenant.name)

        assert result.is_ok()
        repo.commit.assert_awaited_once()
        mock_logger.warning.assert_called_once()

    async def test_create_tenant_defaults_domains_to_empty(self):
        service, repo, adapter = _build_service()
        repo.get_by_name.return_value = None
        tenant = _make_tenant(domains=[])
        repo.create.return_value = tenant
        adapter.sync_tenant.return_value = Ok(None)

        result = await service.create_tenant(name=tenant.name)

        assert result.is_ok()


@pytest.mark.anyio
class TestGetTenant:
    async def test_get_tenant_found(self):
        service, repo, _adapter = _build_service()
        tenant = _make_tenant()
        repo.get.return_value = tenant

        result = await service.get_tenant(tenant_id=tenant.id)

        assert result.is_ok()
        assert result.ok["name"] == tenant.name

    async def test_get_tenant_not_found(self):
        service, repo, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.get_tenant(tenant_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)


@pytest.mark.anyio
class TestGetTenantUsersWithRoles:
    """AC-2.2.3: 3-way JOIN users <-> user_tenant_roles <-> roles."""

    async def test_users_with_roles_success(self):
        service, repo, _adapter = _build_service()
        tenant = _make_tenant()
        repo.get.return_value = tenant

        user1 = MagicMock()
        user1.id = uuid.uuid4()
        user1.email = "alice@acme.com"
        user1.user_name = "alice"
        user1.given_name = "Alice"
        user1.family_name = "Smith"

        role1 = MagicMock()
        role1.id = uuid.uuid4()
        role1.name = "admin"

        role2 = MagicMock()
        role2.id = uuid.uuid4()
        role2.name = "viewer"

        repo.get_users_with_roles.return_value = [(user1, role1), (user1, role2)]

        result = await service.get_tenant_users_with_roles(tenant_id=tenant.id)

        assert result.is_ok()
        users = result.ok
        assert len(users) == 1
        assert users[0]["email"] == "alice@acme.com"
        assert len(users[0]["roles"]) == 2
        role_names = [r["name"] for r in users[0]["roles"]]
        assert "admin" in role_names
        assert "viewer" in role_names

    async def test_users_with_roles_multiple_users(self):
        service, repo, _adapter = _build_service()
        tenant = _make_tenant()
        repo.get.return_value = tenant

        user1 = MagicMock()
        user1.id = uuid.uuid4()
        user1.email = "alice@acme.com"
        user1.user_name = "alice"
        user1.given_name = "Alice"
        user1.family_name = "Smith"

        user2 = MagicMock()
        user2.id = uuid.uuid4()
        user2.email = "bob@acme.com"
        user2.user_name = "bob"
        user2.given_name = "Bob"
        user2.family_name = "Jones"

        role = MagicMock()
        role.id = uuid.uuid4()
        role.name = "viewer"

        repo.get_users_with_roles.return_value = [(user1, role), (user2, role)]

        result = await service.get_tenant_users_with_roles(tenant_id=tenant.id)

        assert result.is_ok()
        assert len(result.ok) == 2

    async def test_users_with_roles_tenant_not_found(self):
        service, repo, _adapter = _build_service()
        repo.get.return_value = None

        result = await service.get_tenant_users_with_roles(tenant_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_users_with_roles_empty(self):
        service, repo, _adapter = _build_service()
        tenant = _make_tenant()
        repo.get.return_value = tenant
        repo.get_users_with_roles.return_value = []

        result = await service.get_tenant_users_with_roles(tenant_id=tenant.id)

        assert result.is_ok()
        assert result.ok == []


@pytest.mark.anyio
class TestCacheInvalidationPublishing:
    """AC-3.3.1: TenantService publishes cache invalidation events after commit."""

    async def test_create_tenant_publishes_event(self):
        publisher = AsyncMock(spec=CacheInvalidationPublisher)
        repo = AsyncMock(spec=TenantRepository)
        adapter = AsyncMock(spec=IdentityProviderAdapter)
        service = TenantService(repository=repo, adapter=adapter, publisher=publisher)
        repo.get_by_name.return_value = None
        tenant = _make_tenant()
        repo.create.return_value = tenant
        adapter.sync_tenant.return_value = Ok(None)

        result = await service.create_tenant(name=tenant.name, domains=tenant.domains)

        assert result.is_ok()
        publisher.publish.assert_awaited_once_with(entity_type="tenant", entity_id=tenant.id, operation="create")

    async def test_no_publish_on_failure(self):
        publisher = AsyncMock(spec=CacheInvalidationPublisher)
        repo = AsyncMock(spec=TenantRepository)
        adapter = AsyncMock(spec=IdentityProviderAdapter)
        service = TenantService(repository=repo, adapter=adapter, publisher=publisher)
        repo.get_by_name.return_value = _make_tenant()  # Duplicate

        result = await service.create_tenant(name="Acme Corp")

        assert result.is_error()
        publisher.publish.assert_not_awaited()
