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


class TestGetIdentityServiceSignature:
    """AC-1.5.4: Dependency factory injects AsyncSession from request scope."""

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(get_identity_service)

    def test_has_session_parameter(self):
        sig = inspect.signature(get_identity_service)
        assert "session" in sig.parameters

    def test_session_has_depends_default(self):
        """The session parameter should use Depends(get_async_session)."""
        sig = inspect.signature(get_identity_service)
        param = sig.parameters["session"]
        # FastAPI Depends objects are used as defaults
        assert param.default is not inspect.Parameter.empty


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
