"""Unit tests for NoOpSyncAdapter behavior (AC-1.5.3)."""

import uuid

import pytest
from expression import Ok

from app.services.adapters.noop import NoOpSyncAdapter


@pytest.mark.anyio
class TestNoOpSyncAdapterMethods:
    """AC-1.5.3: All methods return Ok(None) immediately."""

    async def test_sync_user(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.sync_user(user_id=uuid.uuid4(), data={"email": "a@b.com"})
        assert result == Ok(None)

    async def test_sync_role(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.sync_role(role_id=uuid.uuid4(), data={"name": "admin"})
        assert result == Ok(None)

    async def test_sync_permission(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.sync_permission(permission_id=uuid.uuid4(), data={"name": "read"})
        assert result == Ok(None)

    async def test_sync_tenant(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.sync_tenant(tenant_id=uuid.uuid4(), data={"name": "Acme"})
        assert result == Ok(None)

    async def test_sync_role_assignment(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.sync_role_assignment(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            role_id=uuid.uuid4(),
            role_name="admin",
        )
        assert result == Ok(None)

    async def test_delete_user(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.delete_user(user_id=uuid.uuid4())
        assert result == Ok(None)

    async def test_delete_role(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.delete_role(role_id=uuid.uuid4(), role_name="admin")
        assert result == Ok(None)

    async def test_delete_permission(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.delete_permission(permission_id=uuid.uuid4(), permission_name="docs.read")
        assert result == Ok(None)

    async def test_delete_tenant(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.delete_tenant(tenant_id=uuid.uuid4())
        assert result == Ok(None)

    async def test_all_methods_return_ok_none(self):
        """Verify every adapter method returns Ok(None) — no exceptions."""
        adapter = NoOpSyncAdapter()
        uid = uuid.uuid4()
        data = {"key": "value"}

        results = [
            await adapter.sync_user(user_id=uid, data=data),
            await adapter.sync_role(role_id=uid, data=data),
            await adapter.sync_permission(permission_id=uid, data=data),
            await adapter.sync_tenant(tenant_id=uid, data=data),
            await adapter.sync_role_assignment(user_id=uid, tenant_id=uid, role_id=uid, role_name="admin"),
            await adapter.delete_user(user_id=uid),
            await adapter.delete_role(role_id=uid, role_name="admin"),
            await adapter.delete_permission(permission_id=uid, permission_name="docs.read"),
            await adapter.delete_tenant(tenant_id=uid),
        ]

        for i, result in enumerate(results):
            assert result.is_ok(), f"Method {i} did not return Ok"
            assert result == Ok(None), f"Method {i} returned {result}, not Ok(None)"
