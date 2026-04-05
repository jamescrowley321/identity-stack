"""Unit tests for identity dependency factory behavior (AC-1.5.4).

Verifies:
- get_identity_service() currently raises NotImplementedError (pending PostgresIdentityService)
"""

import pytest

from app.dependencies.identity import get_identity_service


@pytest.mark.anyio
class TestGetIdentityServiceBehavior:
    """get_identity_service() currently raises NotImplementedError."""

    async def test_raises_not_implemented(self):
        """Until PostgresIdentityService exists, the factory raises."""
        from unittest.mock import AsyncMock

        mock_session = AsyncMock()
        with pytest.raises(NotImplementedError, match="PostgresIdentityService"):
            await get_identity_service(session=mock_session)
