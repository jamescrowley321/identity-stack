"""Unit tests for IdentityService ABC (AC-1.5.1).

Verifies:
- 21 abstract async methods exist
- All methods return Result[T, IdentityError]
- All tenant-scoped methods take tenant_id: UUID
- ABC cannot be instantiated directly
- Concrete subclass must implement all methods
"""

import inspect
import typing
import uuid
from abc import ABC

from expression import Result

from app.errors.identity import IdentityError
from app.services.identity import IdentityService


class TestIdentityServiceIsABC:
    def test_is_abstract(self):
        assert issubclass(IdentityService, ABC)

    def test_cannot_instantiate(self):
        try:
            IdentityService()  # type: ignore[abstract]
            assert False, "Should not be able to instantiate ABC"
        except TypeError:
            pass


class TestIdentityServiceMethods:
    """AC-1.5.1 + AC-2.3: IdentityService defines 28 abstract async methods."""

    EXPECTED_METHODS = [
        "create_user",
        "get_user",
        "update_user",
        "deactivate_user",
        "search_users",
        "create_role",
        "get_role",
        "update_role",
        "delete_role",
        "create_permission",
        "get_permission",
        "update_permission",
        "delete_permission",
        "map_permission_to_role",
        "unmap_permission_from_role",
        "create_tenant",
        "get_tenant",
        "update_tenant",
        "delete_tenant",
        "assign_role_to_user",
        "remove_role_from_user",
        "get_tenant_users_with_roles",
        "list_roles",
        "list_permissions",
        "get_role_by_name",
        "get_permission_by_name",
        "activate_user",
        "remove_user_from_tenant",
    ]

    def test_has_exactly_28_methods(self):
        """22 methods from Stories 1.5/2.2 + 6 new methods from Story 2.3 = 28 total."""
        abstract_methods = {
            name
            for name, method in inspect.getmembers(IdentityService, predicate=inspect.isfunction)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == len(self.EXPECTED_METHODS)

    def test_all_expected_methods_exist(self):
        for method_name in self.EXPECTED_METHODS:
            assert hasattr(IdentityService, method_name), f"Missing method: {method_name}"

    def test_all_methods_are_abstract(self):
        for method_name in self.EXPECTED_METHODS:
            method = getattr(IdentityService, method_name)
            assert getattr(method, "__isabstractmethod__", False), f"{method_name} is not abstract"

    def test_all_methods_are_coroutines(self):
        for method_name in self.EXPECTED_METHODS:
            method = getattr(IdentityService, method_name)
            assert inspect.iscoroutinefunction(method), f"{method_name} is not async"

    def test_all_methods_return_result(self):
        """All methods must have a return annotation of Result[T, IdentityError]."""
        for method_name in self.EXPECTED_METHODS:
            method = getattr(IdentityService, method_name)
            hints = typing.get_type_hints(method)
            assert "return" in hints, f"{method_name} has no return annotation"
            ret = hints["return"]
            origin = getattr(ret, "__origin__", None)
            assert origin is Result, f"{method_name} return type is {ret}, not Result"
            # Check error type is IdentityError
            args = ret.__args__
            assert args[1] is IdentityError, f"{method_name} error type is {args[1]}, not IdentityError"


class TestTenantScopedMethods:
    """AC-1.5.1: All tenant-scoped methods take tenant_id: UUID."""

    # Methods where tenant_id is required (no default)
    TENANT_SCOPED_REQUIRED = [
        "create_user",
        "get_user",
        "update_user",
        "deactivate_user",
        "search_users",
        "assign_role_to_user",
        "remove_role_from_user",
        "get_tenant_users_with_roles",
    ]

    # Methods where tenant_id is optional (has default)
    TENANT_SCOPED_OPTIONAL = [
        "create_role",
    ]

    def test_required_tenant_id_methods(self):
        for method_name in self.TENANT_SCOPED_REQUIRED:
            method = getattr(IdentityService, method_name)
            sig = inspect.signature(method)
            assert "tenant_id" in sig.parameters, f"{method_name} missing tenant_id param"
            hints = typing.get_type_hints(method)
            assert hints.get("tenant_id") is uuid.UUID, f"{method_name} tenant_id is not UUID"

    def test_optional_tenant_id_methods(self):
        for method_name in self.TENANT_SCOPED_OPTIONAL:
            method = getattr(IdentityService, method_name)
            sig = inspect.signature(method)
            assert "tenant_id" in sig.parameters, f"{method_name} missing tenant_id param"
            # Optional tenant_id has a default value
            param = sig.parameters["tenant_id"]
            assert param.default is not inspect.Parameter.empty, f"{method_name} tenant_id should be optional"


class TestNonTenantMethods:
    """Methods that identify entities by their own ID, not tenant_id."""

    NON_TENANT_METHODS = [
        "get_role",
        "update_role",
        "delete_role",
        "create_permission",
        "get_permission",
        "update_permission",
        "delete_permission",
        "map_permission_to_role",
        "unmap_permission_from_role",
    ]

    def test_non_tenant_methods_lack_tenant_id(self):
        """These methods operate on entities by their own ID, not tenant-scoped."""
        for method_name in self.NON_TENANT_METHODS:
            method = getattr(IdentityService, method_name)
            sig = inspect.signature(method)
            assert "tenant_id" not in sig.parameters, f"{method_name} unexpectedly has tenant_id"


class TestConcreteSubclassMustImplementAll:
    """A subclass missing any method should raise TypeError on instantiation."""

    def test_partial_implementation_fails(self):
        class PartialService(IdentityService):
            async def create_user(self, **kwargs):
                pass  # Only one method implemented

        try:
            PartialService()  # type: ignore[abstract]
            assert False, "Should not be able to instantiate partial implementation"
        except TypeError:
            pass
