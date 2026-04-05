"""Unit tests for identity dependency factory (AC-1.5.4).

Verifies:
- get_identity_service() has correct FastAPI dependency signature
- Currently raises NotImplementedError (pending PostgresIdentityService in story 2.x)
- AsyncSession is injected via get_async_session
"""

import inspect

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
    """get_identity_service() currently raises NotImplementedError."""

    async def test_raises_not_implemented(self):
        """Until PostgresIdentityService exists, the factory raises."""
        from unittest.mock import AsyncMock

        mock_session = AsyncMock()
        with pytest.raises(NotImplementedError, match="PostgresIdentityService"):
            await get_identity_service(session=mock_session)


class TestIdentityDependencyImports:
    """Verify the module imports the right dependencies."""

    def test_imports_identity_service(self):
        from app.dependencies import identity

        source = inspect.getsource(identity)
        assert "IdentityService" in source

    def test_references_noop_adapter(self):
        """NoOpSyncAdapter is referenced in the factory (wired in story 2.x)."""
        from app.dependencies import identity

        source = inspect.getsource(identity)
        assert "NoOpSyncAdapter" in source

    def test_imports_get_async_session(self):
        from app.dependencies import identity

        source = inspect.getsource(identity)
        assert "get_async_session" in source
