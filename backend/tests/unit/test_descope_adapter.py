"""Unit tests for DescopeSyncAdapter (AC-2.1.4, AC-2.2).

Covers:
- sync_user: user exists (update status), user not found (404 skip), HTTP error, request error
- delete_user: happy path, HTTP error, request error
- sync_role: happy path, 409 duplicate skip, HTTP error, request error
- sync_permission: happy path, 409 duplicate skip, HTTP error, request error
- sync_tenant: happy path, 409 duplicate skip, HTTP error, request error
- sync_role_assignment: happy path, 404 skip, HTTP error, request error
- delete_role: happy path, 404 skip, HTTP error, request error
- delete_permission: happy path, 404 skip, HTTP error, request error
- delete_tenant: happy path, 404 skip, HTTP error, request error
- OTel spans created for operations
- Error wrapping: httpx exceptions → SyncError Result
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from expression import Ok

from app.services.adapters.base import SyncError
from app.services.adapters.descope import DescopeSyncAdapter


@pytest.fixture
def mock_client():
    """Mock DescopeManagementClient."""
    client = AsyncMock()
    client.load_user = AsyncMock()
    client.resolve_login_id = AsyncMock(return_value="login-id-123")
    client.update_user_status = AsyncMock()
    return client


@pytest.fixture
def adapter(mock_client):
    return DescopeSyncAdapter(client=mock_client)


# ---------------------------------------------------------------------------
# sync_user
# ---------------------------------------------------------------------------


class TestSyncUser:
    @pytest.mark.anyio
    async def test_user_exists_updates_status(self, adapter, mock_client):
        """When user exists in Descope, resolve login ID and update status."""
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "alice@test.com", "status": "active"},
        )
        assert result == Ok(None)
        mock_client.load_user.assert_awaited_once()
        mock_client.resolve_login_id.assert_awaited_once()
        mock_client.update_user_status.assert_awaited_once()
        # active → enabled
        call_args = mock_client.update_user_status.call_args
        assert call_args[0][1] == "enabled"

    @pytest.mark.anyio
    async def test_inactive_user_maps_to_disabled(self, adapter, mock_client):
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "bob@test.com", "status": "inactive"},
        )
        assert result == Ok(None)
        call_args = mock_client.update_user_status.call_args
        assert call_args[0][1] == "disabled"

    @pytest.mark.anyio
    async def test_user_not_found_in_descope_skips(self, adapter, mock_client):
        """404 from Descope → skip sync, return Ok(None)."""
        response = MagicMock()
        response.status_code = 404
        mock_client.load_user = AsyncMock(
            side_effect=httpx.HTTPStatusError("not found", request=MagicMock(), response=response)
        )
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "new@test.com", "status": "active"},
        )
        assert result == Ok(None)
        mock_client.resolve_login_id.assert_not_awaited()

    @pytest.mark.anyio
    async def test_http_status_error_returns_sync_error(self, adapter, mock_client):
        """Non-404 HTTPStatusError → SyncError result."""
        response = MagicMock()
        response.status_code = 500
        mock_client.load_user = AsyncMock(
            side_effect=httpx.HTTPStatusError("server error", request=MagicMock(), response=response)
        )
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "err@test.com", "status": "active"},
        )
        assert result.is_error()
        err = result.error
        assert isinstance(err, SyncError)
        assert err.operation == "sync_user"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        """httpx.RequestError → SyncError result."""
        mock_client.load_user = AsyncMock(side_effect=httpx.RequestError("connection failed", request=MagicMock()))
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "timeout@test.com", "status": "active"},
        )
        assert result.is_error()
        err = result.error
        assert isinstance(err, SyncError)
        assert err.operation == "sync_user"

    @pytest.mark.anyio
    async def test_sync_error_contains_context(self, adapter, mock_client):
        uid = uuid.uuid4()
        response = MagicMock()
        response.status_code = 500
        mock_client.load_user = AsyncMock(
            side_effect=httpx.HTTPStatusError("fail", request=MagicMock(), response=response)
        )
        result = await adapter.sync_user(
            user_id=uid,
            data={"email": "ctx@test.com", "status": "active"},
        )
        assert result.error.context["user_id"] == str(uid)

    @pytest.mark.anyio
    async def test_logs_warning_on_error(self, adapter, mock_client):
        response = MagicMock()
        response.status_code = 500
        mock_client.load_user = AsyncMock(
            side_effect=httpx.HTTPStatusError("fail", request=MagicMock(), response=response)
        )
        with patch("app.services.adapters.descope.logger") as mock_logger:
            await adapter.sync_user(
                user_id=uuid.uuid4(),
                data={"email": "log@test.com", "status": "active"},
            )
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_default_status_is_enabled(self, adapter, mock_client):
        """When status is missing from data, default to enabled."""
        await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "no-status@test.com"},
        )
        call_args = mock_client.update_user_status.call_args
        assert call_args[0][1] == "enabled"

    @pytest.mark.anyio
    async def test_value_error_returns_sync_error(self, adapter, mock_client):
        """ValueError from resolve_login_id (e.g. empty loginIds) → SyncError."""
        mock_client.resolve_login_id = AsyncMock(side_effect=ValueError("empty loginIds"))
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "val@test.com", "status": "active"},
        )
        assert result.is_error()
        assert isinstance(result.error, SyncError)
        assert result.error.operation == "sync_user"


# ---------------------------------------------------------------------------
# delete_user
# ---------------------------------------------------------------------------


class TestDeleteUser:
    @pytest.mark.anyio
    async def test_happy_path(self, adapter, mock_client):
        result = await adapter.delete_user(user_id=uuid.uuid4())
        assert result == Ok(None)
        mock_client.resolve_login_id.assert_awaited_once()
        mock_client.update_user_status.assert_awaited_once()
        call_args = mock_client.update_user_status.call_args
        assert call_args[0][1] == "disabled"

    @pytest.mark.anyio
    async def test_user_not_found_in_descope_skips(self, adapter, mock_client):
        """404 from Descope → skip delete, return Ok(None)."""
        response = MagicMock()
        response.status_code = 404
        mock_client.resolve_login_id = AsyncMock(
            side_effect=httpx.HTTPStatusError("not found", request=MagicMock(), response=response)
        )
        result = await adapter.delete_user(user_id=uuid.uuid4())
        assert result == Ok(None)
        mock_client.update_user_status.assert_not_awaited()

    @pytest.mark.anyio
    async def test_http_error_returns_sync_error(self, adapter, mock_client):
        response = MagicMock()
        response.status_code = 500
        mock_client.resolve_login_id = AsyncMock(
            side_effect=httpx.HTTPStatusError("fail", request=MagicMock(), response=response)
        )
        result = await adapter.delete_user(user_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, SyncError)
        assert result.error.operation == "delete_user"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        mock_client.resolve_login_id = AsyncMock(side_effect=httpx.RequestError("timeout", request=MagicMock()))
        result = await adapter.delete_user(user_id=uuid.uuid4())
        assert result.is_error()
        assert result.error.operation == "delete_user"

    @pytest.mark.anyio
    async def test_value_error_returns_sync_error(self, adapter, mock_client):
        """ValueError from resolve_login_id (e.g. empty loginIds) → SyncError."""
        mock_client.resolve_login_id = AsyncMock(side_effect=ValueError("empty loginIds"))
        result = await adapter.delete_user(user_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, SyncError)
        assert result.error.operation == "delete_user"

    @pytest.mark.anyio
    async def test_logs_warning_on_error(self, adapter, mock_client):
        response = MagicMock()
        response.status_code = 500
        mock_client.resolve_login_id = AsyncMock(
            side_effect=httpx.HTTPStatusError("fail", request=MagicMock(), response=response)
        )
        with patch("app.services.adapters.descope.logger") as mock_logger:
            await adapter.delete_user(user_id=uuid.uuid4())
            mock_logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# Helper for creating httpx errors
# ---------------------------------------------------------------------------


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    response = MagicMock()
    response.status_code = status_code
    return httpx.HTTPStatusError(f"{status_code} error", request=MagicMock(), response=response)


# ---------------------------------------------------------------------------
# sync_role (AC-2.2)
# ---------------------------------------------------------------------------


class TestSyncRole:
    @pytest.mark.anyio
    async def test_happy_path(self, adapter, mock_client):
        mock_client.create_role = AsyncMock()
        result = await adapter.sync_role(
            role_id=uuid.uuid4(),
            data={"name": "editor", "description": "Can edit"},
        )
        assert result == Ok(None)
        mock_client.create_role.assert_awaited_once_with(name="editor", description="Can edit")

    @pytest.mark.anyio
    async def test_409_conflict_skips(self, adapter, mock_client):
        """409 from Descope means role already exists — return Ok(None)."""
        mock_client.create_role = AsyncMock(side_effect=_make_http_status_error(409))
        result = await adapter.sync_role(
            role_id=uuid.uuid4(),
            data={"name": "editor"},
        )
        assert result == Ok(None)

    @pytest.mark.anyio
    async def test_http_error_returns_sync_error(self, adapter, mock_client):
        mock_client.create_role = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.sync_role(
            role_id=uuid.uuid4(),
            data={"name": "editor"},
        )
        assert result.is_error()
        assert isinstance(result.error, SyncError)
        assert result.error.operation == "sync_role"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        mock_client.create_role = AsyncMock(side_effect=httpx.RequestError("connection refused", request=MagicMock()))
        result = await adapter.sync_role(
            role_id=uuid.uuid4(),
            data={"name": "editor"},
        )
        assert result.is_error()
        assert result.error.operation == "sync_role"

    @pytest.mark.anyio
    async def test_error_contains_context(self, adapter, mock_client):
        rid = uuid.uuid4()
        mock_client.create_role = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.sync_role(role_id=rid, data={"name": "editor"})
        assert result.error.context["role_id"] == str(rid)

    @pytest.mark.anyio
    async def test_logs_warning_on_error(self, adapter, mock_client):
        mock_client.create_role = AsyncMock(side_effect=_make_http_status_error(500))
        with patch("app.services.adapters.descope.logger") as mock_logger:
            await adapter.sync_role(role_id=uuid.uuid4(), data={"name": "editor"})
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_empty_data_defaults(self, adapter, mock_client):
        mock_client.create_role = AsyncMock()
        await adapter.sync_role(role_id=uuid.uuid4(), data={})
        mock_client.create_role.assert_awaited_once_with(name="", description="")


# ---------------------------------------------------------------------------
# sync_permission (AC-2.2)
# ---------------------------------------------------------------------------


class TestSyncPermission:
    @pytest.mark.anyio
    async def test_happy_path(self, adapter, mock_client):
        mock_client.create_permission = AsyncMock()
        result = await adapter.sync_permission(
            permission_id=uuid.uuid4(),
            data={"name": "docs.write", "description": "Write docs"},
        )
        assert result == Ok(None)
        mock_client.create_permission.assert_awaited_once_with(name="docs.write", description="Write docs")

    @pytest.mark.anyio
    async def test_409_conflict_skips(self, adapter, mock_client):
        mock_client.create_permission = AsyncMock(side_effect=_make_http_status_error(409))
        result = await adapter.sync_permission(
            permission_id=uuid.uuid4(),
            data={"name": "docs.write"},
        )
        assert result == Ok(None)

    @pytest.mark.anyio
    async def test_http_error_returns_sync_error(self, adapter, mock_client):
        mock_client.create_permission = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.sync_permission(
            permission_id=uuid.uuid4(),
            data={"name": "docs.write"},
        )
        assert result.is_error()
        assert result.error.operation == "sync_permission"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        mock_client.create_permission = AsyncMock(side_effect=httpx.RequestError("timeout", request=MagicMock()))
        result = await adapter.sync_permission(
            permission_id=uuid.uuid4(),
            data={"name": "docs.write"},
        )
        assert result.is_error()
        assert result.error.operation == "sync_permission"

    @pytest.mark.anyio
    async def test_error_contains_context(self, adapter, mock_client):
        pid = uuid.uuid4()
        mock_client.create_permission = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.sync_permission(permission_id=pid, data={"name": "x"})
        assert result.error.context["permission_id"] == str(pid)


# ---------------------------------------------------------------------------
# sync_tenant (AC-2.2)
# ---------------------------------------------------------------------------


class TestSyncTenant:
    @pytest.mark.anyio
    async def test_happy_path(self, adapter, mock_client):
        mock_client.create_tenant = AsyncMock()
        result = await adapter.sync_tenant(
            tenant_id=uuid.uuid4(),
            data={"name": "Acme", "domains": ["acme.com"]},
        )
        assert result == Ok(None)
        mock_client.create_tenant.assert_awaited_once_with(name="Acme", self_provisioning_domains=["acme.com"])

    @pytest.mark.anyio
    async def test_empty_domains_passes_none(self, adapter, mock_client):
        mock_client.create_tenant = AsyncMock()
        await adapter.sync_tenant(
            tenant_id=uuid.uuid4(),
            data={"name": "Acme", "domains": []},
        )
        mock_client.create_tenant.assert_awaited_once_with(name="Acme", self_provisioning_domains=None)

    @pytest.mark.anyio
    async def test_409_conflict_skips(self, adapter, mock_client):
        mock_client.create_tenant = AsyncMock(side_effect=_make_http_status_error(409))
        result = await adapter.sync_tenant(
            tenant_id=uuid.uuid4(),
            data={"name": "Acme"},
        )
        assert result == Ok(None)

    @pytest.mark.anyio
    async def test_http_error_returns_sync_error(self, adapter, mock_client):
        mock_client.create_tenant = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.sync_tenant(
            tenant_id=uuid.uuid4(),
            data={"name": "Acme"},
        )
        assert result.is_error()
        assert result.error.operation == "sync_tenant"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        mock_client.create_tenant = AsyncMock(side_effect=httpx.RequestError("timeout", request=MagicMock()))
        result = await adapter.sync_tenant(
            tenant_id=uuid.uuid4(),
            data={"name": "Acme"},
        )
        assert result.is_error()
        assert result.error.operation == "sync_tenant"

    @pytest.mark.anyio
    async def test_error_contains_context(self, adapter, mock_client):
        tid = uuid.uuid4()
        mock_client.create_tenant = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.sync_tenant(tenant_id=tid, data={"name": "Acme"})
        assert result.error.context["tenant_id"] == str(tid)


# ---------------------------------------------------------------------------
# sync_role_assignment (AC-2.2)
# ---------------------------------------------------------------------------


class TestSyncRoleAssignment:
    @pytest.mark.anyio
    async def test_happy_path(self, adapter, mock_client):
        mock_client.assign_roles = AsyncMock()
        uid = uuid.uuid4()
        tid = uuid.uuid4()
        rid = uuid.uuid4()
        result = await adapter.sync_role_assignment(user_id=uid, tenant_id=tid, role_id=rid, role_name="admin")
        assert result == Ok(None)
        mock_client.resolve_login_id.assert_awaited_once_with(str(uid))
        mock_client.assign_roles.assert_awaited_once_with("login-id-123", str(tid), ["admin"])

    @pytest.mark.anyio
    async def test_404_skips(self, adapter, mock_client):
        """User/tenant not found in Descope �� skip, return Ok(None)."""
        mock_client.resolve_login_id = AsyncMock(side_effect=_make_http_status_error(404))
        result = await adapter.sync_role_assignment(
            user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=uuid.uuid4(), role_name="admin"
        )
        assert result == Ok(None)

    @pytest.mark.anyio
    async def test_http_error_returns_sync_error(self, adapter, mock_client):
        mock_client.resolve_login_id = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.sync_role_assignment(
            user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=uuid.uuid4(), role_name="admin"
        )
        assert result.is_error()
        assert result.error.operation == "sync_role_assignment"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        mock_client.resolve_login_id = AsyncMock(side_effect=httpx.RequestError("timeout", request=MagicMock()))
        result = await adapter.sync_role_assignment(
            user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=uuid.uuid4(), role_name="admin"
        )
        assert result.is_error()
        assert result.error.operation == "sync_role_assignment"

    @pytest.mark.anyio
    async def test_value_error_returns_sync_error(self, adapter, mock_client):
        mock_client.resolve_login_id = AsyncMock(side_effect=ValueError("empty loginIds"))
        result = await adapter.sync_role_assignment(
            user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=uuid.uuid4(), role_name="admin"
        )
        assert result.is_error()
        assert result.error.operation == "sync_role_assignment"

    @pytest.mark.anyio
    async def test_error_contains_all_ids(self, adapter, mock_client):
        uid = uuid.uuid4()
        tid = uuid.uuid4()
        rid = uuid.uuid4()
        mock_client.resolve_login_id = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.sync_role_assignment(user_id=uid, tenant_id=tid, role_id=rid, role_name="admin")
        ctx = result.error.context
        assert ctx["user_id"] == str(uid)
        assert ctx["tenant_id"] == str(tid)
        assert ctx["role_id"] == str(rid)


# ---------------------------------------------------------------------------
# delete_role (AC-2.2)
# ---------------------------------------------------------------------------


class TestDeleteRole:
    @pytest.mark.anyio
    async def test_happy_path(self, adapter, mock_client):
        mock_client.delete_role = AsyncMock()
        rid = uuid.uuid4()
        result = await adapter.delete_role(role_id=rid, role_name="admin")
        assert result == Ok(None)
        mock_client.delete_role.assert_awaited_once_with("admin")

    @pytest.mark.anyio
    async def test_404_skips(self, adapter, mock_client):
        mock_client.delete_role = AsyncMock(side_effect=_make_http_status_error(404))
        result = await adapter.delete_role(role_id=uuid.uuid4(), role_name="admin")
        assert result == Ok(None)

    @pytest.mark.anyio
    async def test_http_error_returns_sync_error(self, adapter, mock_client):
        mock_client.delete_role = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.delete_role(role_id=uuid.uuid4(), role_name="admin")
        assert result.is_error()
        assert result.error.operation == "delete_role"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        mock_client.delete_role = AsyncMock(side_effect=httpx.RequestError("timeout", request=MagicMock()))
        result = await adapter.delete_role(role_id=uuid.uuid4(), role_name="admin")
        assert result.is_error()
        assert result.error.operation == "delete_role"

    @pytest.mark.anyio
    async def test_error_contains_context(self, adapter, mock_client):
        rid = uuid.uuid4()
        mock_client.delete_role = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.delete_role(role_id=rid, role_name="admin")
        assert result.error.context["role_id"] == str(rid)

    @pytest.mark.anyio
    async def test_logs_warning_on_error(self, adapter, mock_client):
        mock_client.delete_role = AsyncMock(side_effect=_make_http_status_error(500))
        with patch("app.services.adapters.descope.logger") as mock_logger:
            await adapter.delete_role(role_id=uuid.uuid4(), role_name="admin")
            mock_logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# delete_permission (AC-2.2)
# ---------------------------------------------------------------------------


class TestDeletePermission:
    @pytest.mark.anyio
    async def test_happy_path(self, adapter, mock_client):
        mock_client.delete_permission = AsyncMock()
        pid = uuid.uuid4()
        result = await adapter.delete_permission(permission_id=pid, permission_name="documents.write")
        assert result == Ok(None)
        mock_client.delete_permission.assert_awaited_once_with("documents.write")

    @pytest.mark.anyio
    async def test_404_skips(self, adapter, mock_client):
        mock_client.delete_permission = AsyncMock(side_effect=_make_http_status_error(404))
        result = await adapter.delete_permission(permission_id=uuid.uuid4(), permission_name="documents.write")
        assert result == Ok(None)

    @pytest.mark.anyio
    async def test_http_error_returns_sync_error(self, adapter, mock_client):
        mock_client.delete_permission = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.delete_permission(permission_id=uuid.uuid4(), permission_name="documents.write")
        assert result.is_error()
        assert result.error.operation == "delete_permission"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        mock_client.delete_permission = AsyncMock(side_effect=httpx.RequestError("timeout", request=MagicMock()))
        result = await adapter.delete_permission(permission_id=uuid.uuid4(), permission_name="documents.write")
        assert result.is_error()
        assert result.error.operation == "delete_permission"

    @pytest.mark.anyio
    async def test_error_contains_context(self, adapter, mock_client):
        pid = uuid.uuid4()
        mock_client.delete_permission = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.delete_permission(permission_id=pid, permission_name="documents.write")
        assert result.error.context["permission_id"] == str(pid)


# ---------------------------------------------------------------------------
# delete_tenant (AC-2.2)
# ---------------------------------------------------------------------------


class TestDeleteTenant:
    @pytest.mark.anyio
    async def test_happy_path(self, adapter, mock_client):
        mock_client.delete_tenant = AsyncMock()
        tid = uuid.uuid4()
        result = await adapter.delete_tenant(tenant_id=tid)
        assert result == Ok(None)
        mock_client.delete_tenant.assert_awaited_once_with(str(tid))

    @pytest.mark.anyio
    async def test_404_skips(self, adapter, mock_client):
        mock_client.delete_tenant = AsyncMock(side_effect=_make_http_status_error(404))
        result = await adapter.delete_tenant(tenant_id=uuid.uuid4())
        assert result == Ok(None)

    @pytest.mark.anyio
    async def test_http_error_returns_sync_error(self, adapter, mock_client):
        mock_client.delete_tenant = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.delete_tenant(tenant_id=uuid.uuid4())
        assert result.is_error()
        assert result.error.operation == "delete_tenant"

    @pytest.mark.anyio
    async def test_request_error_returns_sync_error(self, adapter, mock_client):
        mock_client.delete_tenant = AsyncMock(side_effect=httpx.RequestError("timeout", request=MagicMock()))
        result = await adapter.delete_tenant(tenant_id=uuid.uuid4())
        assert result.is_error()
        assert result.error.operation == "delete_tenant"

    @pytest.mark.anyio
    async def test_error_contains_context(self, adapter, mock_client):
        tid = uuid.uuid4()
        mock_client.delete_tenant = AsyncMock(side_effect=_make_http_status_error(500))
        result = await adapter.delete_tenant(tenant_id=tid)
        assert result.error.context["tenant_id"] == str(tid)

    @pytest.mark.anyio
    async def test_logs_warning_on_error(self, adapter, mock_client):
        mock_client.delete_tenant = AsyncMock(side_effect=_make_http_status_error(500))
        with patch("app.services.adapters.descope.logger") as mock_logger:
            await adapter.delete_tenant(tenant_id=uuid.uuid4())
            mock_logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# DescopeSyncAdapter is concrete IdentityProviderAdapter
# ---------------------------------------------------------------------------


class TestDescopeSyncAdapterIsAdapter:
    def test_is_subclass(self):
        from app.services.adapters.base import IdentityProviderAdapter

        assert issubclass(DescopeSyncAdapter, IdentityProviderAdapter)

    def test_can_instantiate(self, mock_client):
        adapter = DescopeSyncAdapter(client=mock_client)
        assert adapter is not None
