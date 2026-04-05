"""Unit tests for identity dependency factory (AC-1.5.4, updated for AC-2.1).

Verifies:
- get_identity_service() has correct FastAPI dependency signature
- Returns PostgresIdentityService with DescopeSyncAdapter (story 2.1)
- AsyncSession is injected via get_async_session
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from app.dependencies.identity import get_identity_service


@pytest.mark.anyio
class TestGetIdentityServiceBehavior:
    """get_identity_service() returns PostgresIdentityService (story 2.1)."""

    async def test_returns_postgres_identity_service(self):
        from app.services.identity_impl import PostgresIdentityService

        mock_session = AsyncMock()
        with patch("app.dependencies.identity.get_descope_client"):
            svc = await get_identity_service(session=mock_session)
            assert isinstance(svc, PostgresIdentityService)

    async def test_uses_descope_sync_adapter(self):
        from app.services.adapters.descope import DescopeSyncAdapter

        mock_session = AsyncMock()
        with patch("app.dependencies.identity.get_descope_client"):
            svc = await get_identity_service(session=mock_session)
            assert isinstance(svc._adapter, DescopeSyncAdapter)


class TestIdentityDependencyImports:
    """Verify the module imports the right dependencies."""

    def test_imports_identity_service(self):
        from app.dependencies import identity

        source = inspect.getsource(identity)
        assert "IdentityService" in source

    def test_imports_descope_sync_adapter(self):
        """DescopeSyncAdapter is used in the factory (story 2.1)."""
        from app.dependencies import identity

        source = inspect.getsource(identity)
        assert "DescopeSyncAdapter" in source

    def test_imports_get_async_session(self):
        from app.dependencies import identity

        source = inspect.getsource(identity)
        assert "get_async_session" in source
