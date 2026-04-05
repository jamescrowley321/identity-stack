"""Unit tests for IdentityProviderAdapter ABC, SyncError, and NoOpSyncAdapter.

Covers:
- IdentityProviderAdapter ABC with 9 abstract methods (AC-1.5.2)
- SyncError dataclass
- NoOpSyncAdapter returns Ok(None) for all methods (AC-1.5.3)
"""

import inspect
import typing
import uuid
from abc import ABC

import pytest
from expression import Ok, Result

from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.adapters.noop import NoOpSyncAdapter

# ---------------------------------------------------------------------------
# SyncError dataclass
# ---------------------------------------------------------------------------


class TestSyncError:
    def test_fields(self):
        err = SyncError(message="sync failed", operation="create_user", context={"user_id": "abc"})
        assert err.message == "sync failed"
        assert err.operation == "create_user"
        assert err.context == {"user_id": "abc"}

    def test_defaults(self):
        err = SyncError(message="failed")
        assert err.operation == ""
        assert err.context is None

    def test_frozen(self):
        err = SyncError(message="test")
        try:
            err.message = "changed"  # type: ignore[misc]
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# IdentityProviderAdapter ABC (AC-1.5.2)
# ---------------------------------------------------------------------------


class TestIdentityProviderAdapterIsABC:
    def test_is_abstract(self):
        assert issubclass(IdentityProviderAdapter, ABC)

    def test_cannot_instantiate(self):
        try:
            IdentityProviderAdapter()  # type: ignore[abstract]
            assert False, "Should not be able to instantiate ABC"
        except TypeError:
            pass


class TestIdentityProviderAdapterMethods:
    """AC-1.5.2: 9 abstract async methods returning Result[None, SyncError]."""

    EXPECTED_METHODS = [
        "sync_user",
        "sync_role",
        "sync_permission",
        "sync_tenant",
        "sync_role_assignment",
        "delete_user",
        "delete_role",
        "delete_permission",
        "delete_tenant",
    ]

    def test_has_exactly_9_methods(self):
        abstract_methods = {
            name
            for name, method in inspect.getmembers(IdentityProviderAdapter, predicate=inspect.isfunction)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 9

    def test_all_expected_methods_exist(self):
        for method_name in self.EXPECTED_METHODS:
            assert hasattr(IdentityProviderAdapter, method_name), f"Missing method: {method_name}"

    def test_all_methods_are_abstract(self):
        for method_name in self.EXPECTED_METHODS:
            method = getattr(IdentityProviderAdapter, method_name)
            assert getattr(method, "__isabstractmethod__", False), f"{method_name} is not abstract"

    def test_all_methods_are_coroutines(self):
        for method_name in self.EXPECTED_METHODS:
            method = getattr(IdentityProviderAdapter, method_name)
            assert inspect.iscoroutinefunction(method), f"{method_name} is not async"

    def test_all_methods_return_result_none_sync_error(self):
        """All adapter methods return Result[None, SyncError]."""
        for method_name in self.EXPECTED_METHODS:
            method = getattr(IdentityProviderAdapter, method_name)
            hints = typing.get_type_hints(method)
            assert "return" in hints, f"{method_name} has no return annotation"
            ret = hints["return"]
            origin = getattr(ret, "__origin__", None)
            assert origin is Result, f"{method_name} return type is {ret}, not Result"
            args = ret.__args__
            assert args[0] is None, f"{method_name} ok type is {args[0]}, not None"
            assert args[1] is SyncError, f"{method_name} error type is {args[1]}, not SyncError"


class TestAdapterPartialImplementationFails:
    def test_partial_implementation_fails(self):
        class PartialAdapter(IdentityProviderAdapter):
            async def sync_user(self, **kwargs):
                pass

        try:
            PartialAdapter()  # type: ignore[abstract]
            assert False, "Should not be able to instantiate partial adapter"
        except TypeError:
            pass


# ---------------------------------------------------------------------------
# NoOpSyncAdapter (AC-1.5.3)
# ---------------------------------------------------------------------------


class TestNoOpSyncAdapterIsAdapter:
    def test_is_subclass(self):
        assert issubclass(NoOpSyncAdapter, IdentityProviderAdapter)

    def test_can_instantiate(self):
        adapter = NoOpSyncAdapter()
        assert adapter is not None


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
        )
        assert result == Ok(None)

    async def test_delete_user(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.delete_user(user_id=uuid.uuid4())
        assert result == Ok(None)

    async def test_delete_role(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.delete_role(role_id=uuid.uuid4())
        assert result == Ok(None)

    async def test_delete_permission(self):
        adapter = NoOpSyncAdapter()
        result = await adapter.delete_permission(permission_id=uuid.uuid4())
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
            await adapter.sync_role_assignment(user_id=uid, tenant_id=uid, role_id=uid),
            await adapter.delete_user(user_id=uid),
            await adapter.delete_role(role_id=uid),
            await adapter.delete_permission(permission_id=uid),
            await adapter.delete_tenant(tenant_id=uid),
        ]

        for i, result in enumerate(results):
            assert result.is_ok(), f"Method {i} did not return Ok"
            assert result == Ok(None), f"Method {i} returned {result}, not Ok(None)"
