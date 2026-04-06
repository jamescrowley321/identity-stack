"""Unit tests for identity dependency factories (AC-2.1.7, AC-2.2.6).

Verifies:
- get_user_service() returns a UserService with correctly wired repository and adapter.
- get_role_service() returns a RoleService with 3 repositories and adapter.
- get_permission_service() returns a PermissionService with repository and adapter.
- get_tenant_service() returns a TenantService with repository and adapter.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.dependencies.identity import (
    get_permission_service,
    get_role_service,
    get_tenant_service,
    get_user_service,
)
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.permission import PermissionService
from app.services.role import RoleService
from app.services.tenant import TenantService
from app.services.user import UserService


@pytest.mark.anyio
class TestGetUserService:
    """get_user_service() wires AsyncSession → UserRepository → UserService."""

    @patch("app.dependencies.identity.get_descope_client")
    async def test_returns_user_service(self, mock_get_client):
        """Factory returns a UserService with correct dependencies."""
        mock_session = AsyncMock()
        mock_get_client.return_value = AsyncMock()

        service = await get_user_service(session=mock_session)

        assert isinstance(service, UserService)
        assert isinstance(service._repository, UserRepository)
        assert isinstance(service._adapter, DescopeSyncAdapter)

    @patch("app.dependencies.identity.get_descope_client")
    async def test_repository_receives_session(self, mock_get_client):
        """Repository is initialized with the provided session."""
        mock_session = AsyncMock()
        mock_get_client.return_value = AsyncMock()

        service = await get_user_service(session=mock_session)

        assert service._repository._session is mock_session

    @patch("app.dependencies.identity.get_descope_client")
    async def test_adapter_receives_client(self, mock_get_client):
        """Adapter is initialized with the DescopeManagementClient."""
        mock_session = AsyncMock()
        mock_client = AsyncMock()
        mock_get_client.return_value = mock_client

        service = await get_user_service(session=mock_session)

        assert service._adapter._client is mock_client


@pytest.mark.anyio
class TestGetRoleService:
    """AC-2.2.6: get_role_service() wires 3 repositories + adapter → RoleService."""

    @patch("app.dependencies.identity.get_descope_client")
    async def test_returns_role_service(self, mock_get_client):
        mock_session = AsyncMock()
        mock_get_client.return_value = AsyncMock()

        service = await get_role_service(session=mock_session)

        assert isinstance(service, RoleService)
        assert isinstance(service._repository, RoleRepository)
        assert isinstance(service._permission_repository, PermissionRepository)
        assert isinstance(service._assignment_repository, UserTenantRoleRepository)
        assert isinstance(service._adapter, DescopeSyncAdapter)

    @patch("app.dependencies.identity.get_descope_client")
    async def test_all_repositories_share_session(self, mock_get_client):
        """All repositories must receive the same session for transactional consistency."""
        mock_session = AsyncMock()
        mock_get_client.return_value = AsyncMock()

        service = await get_role_service(session=mock_session)

        assert service._repository._session is mock_session
        assert service._permission_repository._session is mock_session
        assert service._assignment_repository._session is mock_session


@pytest.mark.anyio
class TestGetPermissionService:
    """AC-2.2.6: get_permission_service() wires PermissionRepository + adapter."""

    @patch("app.dependencies.identity.get_descope_client")
    async def test_returns_permission_service(self, mock_get_client):
        mock_session = AsyncMock()
        mock_get_client.return_value = AsyncMock()

        service = await get_permission_service(session=mock_session)

        assert isinstance(service, PermissionService)
        assert isinstance(service._repository, PermissionRepository)
        assert isinstance(service._adapter, DescopeSyncAdapter)

    @patch("app.dependencies.identity.get_descope_client")
    async def test_repository_receives_session(self, mock_get_client):
        mock_session = AsyncMock()
        mock_get_client.return_value = AsyncMock()

        service = await get_permission_service(session=mock_session)

        assert service._repository._session is mock_session


@pytest.mark.anyio
class TestGetTenantService:
    """AC-2.2.6: get_tenant_service() wires TenantRepository + adapter."""

    @patch("app.dependencies.identity.get_descope_client")
    async def test_returns_tenant_service(self, mock_get_client):
        mock_session = AsyncMock()
        mock_get_client.return_value = AsyncMock()

        service = await get_tenant_service(session=mock_session)

        assert isinstance(service, TenantService)
        assert isinstance(service._repository, TenantRepository)
        assert isinstance(service._adapter, DescopeSyncAdapter)

    @patch("app.dependencies.identity.get_descope_client")
    async def test_repository_receives_session(self, mock_get_client):
        mock_session = AsyncMock()
        mock_get_client.return_value = AsyncMock()

        service = await get_tenant_service(session=mock_session)

        assert service._repository._session is mock_session
