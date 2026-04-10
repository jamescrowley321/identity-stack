"""Unit tests for DescopeSyncAdapter (AC-2.1.5).

Tests cover:
- sync_user: success → Ok(None), maps inactive→disabled/active→enabled/provisioned→enabled
- sync_user: client exception → Error(SyncError) with operation context
- sync_user: missing email/status in data → Error(SyncError)
- sync_user: unknown status → Error(SyncError) (fail-closed)
- sync_role, sync_permission, sync_tenant: success and failure paths
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from expression import Ok

from app.services.adapters.base import SyncError
from app.services.adapters.descope import DescopeSyncAdapter


@pytest.fixture
def mock_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def adapter(mock_client) -> DescopeSyncAdapter:
    return DescopeSyncAdapter(client=mock_client)


@pytest.mark.anyio
class TestSyncUser:
    """AC-2.1.5: sync_user wraps DescopeManagementClient.update_user_status."""

    async def test_sync_active_user_calls_enabled(self, adapter, mock_client):
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "a@b.com", "status": "active"},
        )

        assert result == Ok(None)
        mock_client.update_user_status.assert_awaited_once_with("a@b.com", "enabled")

    async def test_sync_inactive_user_calls_disabled(self, adapter, mock_client):
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "a@b.com", "status": "inactive"},
        )

        assert result == Ok(None)
        mock_client.update_user_status.assert_awaited_once_with("a@b.com", "disabled")

    async def test_sync_provisioned_user_calls_enabled(self, adapter, mock_client):
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "a@b.com", "status": "provisioned"},
        )

        assert result == Ok(None)
        mock_client.update_user_status.assert_awaited_once_with("a@b.com", "enabled")

    async def test_sync_user_missing_data_returns_error(self, adapter, mock_client):
        """When email or status is missing, return Error(SyncError)."""
        result = await adapter.sync_user(user_id=uuid.uuid4(), data={})

        assert result.is_error()
        assert isinstance(result.error, SyncError)
        assert result.error.operation == "sync_user"
        assert "Missing required" in result.error.message
        mock_client.update_user_status.assert_not_awaited()

    async def test_sync_user_missing_email_returns_error(self, adapter, mock_client):
        result = await adapter.sync_user(user_id=uuid.uuid4(), data={"status": "active"})

        assert result.is_error()
        assert "Missing required" in result.error.message

    async def test_sync_user_missing_status_returns_error(self, adapter, mock_client):
        result = await adapter.sync_user(user_id=uuid.uuid4(), data={"email": "a@b.com"})

        assert result.is_error()
        assert "Missing required" in result.error.message

    async def test_sync_user_unknown_status_returns_error(self, adapter, mock_client):
        """Unknown status should fail closed, not map to 'enabled'."""
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "a@b.com", "status": "garbage"},
        )

        assert result.is_error()
        assert "Unknown status" in result.error.message
        mock_client.update_user_status.assert_not_awaited()

    async def test_sync_user_client_error_returns_sync_error(self, adapter, mock_client):
        mock_client.update_user_status.side_effect = RuntimeError("connection refused")

        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "a@b.com", "status": "active"},
        )

        assert result.is_error()
        err = result.error
        assert isinstance(err, SyncError)
        assert err.operation == "sync_user"
        assert "connection refused" in err.message


@pytest.mark.anyio
class TestSyncRole:
    async def test_sync_role_success(self, adapter, mock_client):
        result = await adapter.sync_role(
            role_id=uuid.uuid4(),
            data={"name": "admin", "description": "Admin role"},
        )

        assert result == Ok(None)
        mock_client.create_role.assert_awaited_once()

    async def test_sync_role_failure(self, adapter, mock_client):
        mock_client.create_role.side_effect = RuntimeError("API error")

        result = await adapter.sync_role(
            role_id=uuid.uuid4(),
            data={"name": "admin"},
        )

        assert result.is_error()
        assert result.error.operation == "sync_role"


@pytest.mark.anyio
class TestSyncPermission:
    async def test_sync_permission_success(self, adapter, mock_client):
        result = await adapter.sync_permission(
            permission_id=uuid.uuid4(),
            data={"name": "read", "description": "Read access"},
        )

        assert result == Ok(None)
        mock_client.create_permission.assert_awaited_once()

    async def test_sync_permission_failure(self, adapter, mock_client):
        mock_client.create_permission.side_effect = RuntimeError("fail")

        result = await adapter.sync_permission(
            permission_id=uuid.uuid4(),
            data={"name": "read"},
        )

        assert result.is_error()
        assert result.error.operation == "sync_permission"


@pytest.mark.anyio
class TestSyncTenant:
    async def test_sync_tenant_success(self, adapter, mock_client):
        result = await adapter.sync_tenant(
            tenant_id=uuid.uuid4(),
            data={"name": "Acme", "self_provisioning_domains": ["acme.com"]},
        )

        assert result == Ok(None)
        mock_client.create_tenant.assert_awaited_once()

    async def test_sync_tenant_failure(self, adapter, mock_client):
        mock_client.create_tenant.side_effect = RuntimeError("fail")

        result = await adapter.sync_tenant(
            tenant_id=uuid.uuid4(),
            data={"name": "Acme"},
        )

        assert result.is_error()
        assert result.error.operation == "sync_tenant"


@pytest.mark.anyio
class TestDeleteOperations:
    """Delete methods are placeholder stubs — verify they return Ok(None)."""

    async def test_delete_user(self, adapter):
        result = await adapter.delete_user(user_id=uuid.uuid4())
        assert result == Ok(None)

    async def test_delete_role(self, adapter):
        result = await adapter.delete_role(role_id=uuid.uuid4())
        assert result == Ok(None)

    async def test_delete_permission(self, adapter):
        result = await adapter.delete_permission(permission_id=uuid.uuid4())
        assert result == Ok(None)

    async def test_delete_tenant(self, adapter):
        result = await adapter.delete_tenant(tenant_id=uuid.uuid4())
        assert result == Ok(None)

    async def test_sync_role_assignment(self, adapter, mock_client):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        role_id = uuid.uuid4()

        result = await adapter.sync_role_assignment(
            user_id=user_id,
            tenant_id=tenant_id,
            role_id=role_id,
            data={"role_name": "admin", "login_id": "user@example.com"},
        )

        assert result == Ok(None)
        mock_client.assign_roles.assert_awaited_once_with("user@example.com", str(tenant_id), ["admin"])

    async def test_sync_role_assignment_fallback_without_data(self, adapter, mock_client):
        """Without data, falls back to str(role_id) and str(user_id)."""
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        role_id = uuid.uuid4()

        result = await adapter.sync_role_assignment(
            user_id=user_id,
            tenant_id=tenant_id,
            role_id=role_id,
        )

        assert result == Ok(None)
        mock_client.assign_roles.assert_awaited_once_with(str(user_id), str(tenant_id), [str(role_id)])

    async def test_sync_role_assignment_failure(self, adapter, mock_client):
        mock_client.assign_roles.side_effect = RuntimeError("API error")

        result = await adapter.sync_role_assignment(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            role_id=uuid.uuid4(),
        )

        assert result.is_error()
        assert result.error.operation == "sync_role_assignment"
        assert "API error" in result.error.message
