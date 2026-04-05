"""Unit tests for identity dependency factory (AC-2.1.7).

Verifies:
- get_user_service() returns a UserService with correctly wired repository and adapter.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.dependencies.identity import get_user_service
from app.repositories.user import UserRepository
from app.services.adapters.descope import DescopeSyncAdapter
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
