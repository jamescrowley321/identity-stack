"""Unit tests for identity dependency factories (AC-2.1.7, AC-2.2.6).

Verifies:
- get_user_service() returns a UserService with correctly wired repository and adapter.
- get_role_service() returns a RoleService with 3 repositories and adapter.
- get_permission_service() returns a PermissionService with repository and adapter.
- get_tenant_service() returns a TenantService with repository and adapter.
"""

from unittest.mock import AsyncMock

import pytest

from app.dependencies.identity import (
    get_idp_link_service,
    get_permission_service,
    get_provider_service,
    get_role_service,
    get_tenant_service,
    get_user_service,
)
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.idp_link import IdPLinkService
from app.services.permission import PermissionService
from app.services.provider import ProviderService
from app.services.role import RoleService
from app.services.tenant import TenantService
from app.services.user import UserService


def _make_mock_request(descope_client=None, cache_publisher=None, redis_client=None):
    """Create a mock Request with app.state attributes for dependency injection."""
    mock_request = AsyncMock()
    mock_request.app.state.descope_client = descope_client or AsyncMock()
    mock_request.app.state.cache_publisher = cache_publisher or AsyncMock()
    mock_request.app.state.redis_client = redis_client
    return mock_request


@pytest.mark.anyio
class TestGetUserService:
    """get_user_service() wires AsyncSession -> UserRepository -> UserService."""

    async def test_returns_user_service(self):
        """Factory returns a UserService with correct dependencies."""
        mock_session = AsyncMock()
        mock_request = _make_mock_request()

        service = await get_user_service(request=mock_request, session=mock_session)

        assert isinstance(service, UserService)
        assert isinstance(service._repository, UserRepository)
        assert isinstance(service._adapter, DescopeSyncAdapter)

    async def test_repository_receives_session(self):
        """Repository is initialized with the provided session."""
        mock_session = AsyncMock()
        mock_request = _make_mock_request()

        service = await get_user_service(request=mock_request, session=mock_session)

        assert service._repository._session is mock_session

    async def test_adapter_receives_client(self):
        """Adapter is initialized with the DescopeManagementClient."""
        mock_session = AsyncMock()
        mock_client = AsyncMock()
        mock_request = _make_mock_request(descope_client=mock_client)

        service = await get_user_service(request=mock_request, session=mock_session)

        assert service._adapter._client is mock_client


@pytest.mark.anyio
class TestGetRoleService:
    """AC-2.2.6: get_role_service() wires 3 repositories + adapter -> RoleService."""

    async def test_returns_role_service(self):
        mock_session = AsyncMock()
        mock_request = _make_mock_request()

        service = await get_role_service(request=mock_request, session=mock_session)

        assert isinstance(service, RoleService)
        assert isinstance(service._repository, RoleRepository)
        assert isinstance(service._permission_repository, PermissionRepository)
        assert isinstance(service._assignment_repository, UserTenantRoleRepository)
        assert isinstance(service._adapter, DescopeSyncAdapter)

    async def test_all_repositories_share_session(self):
        """All repositories must receive the same session for transactional consistency."""
        mock_session = AsyncMock()
        mock_request = _make_mock_request()

        service = await get_role_service(request=mock_request, session=mock_session)

        assert service._repository._session is mock_session
        assert service._permission_repository._session is mock_session
        assert service._assignment_repository._session is mock_session


@pytest.mark.anyio
class TestGetPermissionService:
    """AC-2.2.6: get_permission_service() wires PermissionRepository + adapter."""

    async def test_returns_permission_service(self):
        mock_session = AsyncMock()
        mock_request = _make_mock_request()

        service = await get_permission_service(request=mock_request, session=mock_session)

        assert isinstance(service, PermissionService)
        assert isinstance(service._repository, PermissionRepository)
        assert isinstance(service._adapter, DescopeSyncAdapter)

    async def test_repository_receives_session(self):
        mock_session = AsyncMock()
        mock_request = _make_mock_request()

        service = await get_permission_service(request=mock_request, session=mock_session)

        assert service._repository._session is mock_session


@pytest.mark.anyio
class TestGetTenantService:
    """AC-2.2.6: get_tenant_service() wires TenantRepository + adapter."""

    async def test_returns_tenant_service(self):
        mock_session = AsyncMock()
        mock_request = _make_mock_request()

        service = await get_tenant_service(request=mock_request, session=mock_session)

        assert isinstance(service, TenantService)
        assert isinstance(service._repository, TenantRepository)
        assert isinstance(service._adapter, DescopeSyncAdapter)

    async def test_repository_receives_session(self):
        mock_session = AsyncMock()
        mock_request = _make_mock_request()

        service = await get_tenant_service(request=mock_request, session=mock_session)

        assert service._repository._session is mock_session


@pytest.mark.anyio
class TestGetIdPLinkService:
    """AC-4.1.3: get_idp_link_service() wires 3 repositories -> IdPLinkService."""

    async def test_returns_idp_link_service(self):
        mock_session = AsyncMock()

        service = await get_idp_link_service(session=mock_session)

        assert isinstance(service, IdPLinkService)
        assert isinstance(service._repository, IdPLinkRepository)
        assert isinstance(service._user_repository, UserRepository)
        assert isinstance(service._provider_repository, ProviderRepository)

    async def test_all_repositories_share_session(self):
        """All repositories must receive the same session for transactional consistency."""
        mock_session = AsyncMock()

        service = await get_idp_link_service(session=mock_session)

        assert service._repository._session is mock_session
        assert service._user_repository._session is mock_session
        assert service._provider_repository._session is mock_session


@pytest.mark.anyio
class TestGetProviderService:
    """AC-4.1.3: get_provider_service() wires ProviderRepository -> ProviderService."""

    async def test_returns_provider_service(self):
        mock_session = AsyncMock()

        service = await get_provider_service(session=mock_session)

        assert isinstance(service, ProviderService)
        assert isinstance(service._repository, ProviderRepository)

    async def test_repository_receives_session(self):
        mock_session = AsyncMock()

        service = await get_provider_service(session=mock_session)

        assert service._repository._session is mock_session
