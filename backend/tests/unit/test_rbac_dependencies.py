"""Unit tests for RBAC dependency factories."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.dependencies.rbac import require_all_permissions, require_any_permission, require_permission, require_role


def _make_request(claims):
    request = MagicMock()
    request.state.claims = claims
    return request


CLAIMS_ADMIN = {
    "sub": "user123",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": ["projects.create", "projects.read", "members.invite"]},
    },
}

CLAIMS_VIEWER = {
    "sub": "user456",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["viewer"], "permissions": ["projects.read", "documents.read"]},
    },
}

CLAIMS_MULTI_ROLE = {
    "sub": "user789",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin", "member"], "permissions": ["projects.read"]},
    },
}

CLAIMS_NO_TENANT = {
    "sub": "user123",
    "tenants": {"tenant-abc": {"roles": ["admin"], "permissions": []}},
}

CLAIMS_NO_TENANTS_KEY = {
    "sub": "user123",
    "dct": "tenant-abc",
}


class TestRequireRole:
    def test_allows_matching_role(self):
        dep = require_role("admin")
        result = dep(_make_request(CLAIMS_ADMIN))
        assert result == ["admin"]

    def test_allows_any_of_multiple_roles(self):
        dep = require_role("owner", "admin")
        result = dep(_make_request(CLAIMS_ADMIN))
        assert "admin" in result

    def test_rejects_non_matching_role(self):
        dep = require_role("owner")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(CLAIMS_VIEWER))
        assert exc_info.value.status_code == 403

    def test_rejects_without_tenant_context(self):
        dep = require_role("admin")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(CLAIMS_NO_TENANT))
        assert exc_info.value.status_code == 403

    def test_rejects_without_claims(self):
        dep = require_role("admin")
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            dep(request)
        assert exc_info.value.status_code == 401

    def test_handles_missing_tenants_key(self):
        dep = require_role("admin")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(CLAIMS_NO_TENANTS_KEY))
        assert exc_info.value.status_code == 403

    def test_multi_role_user(self):
        dep = require_role("member")
        result = dep(_make_request(CLAIMS_MULTI_ROLE))
        assert "member" in result


class TestRequirePermission:
    def test_allows_matching_permission(self):
        dep = require_permission("projects.create")
        result = dep(_make_request(CLAIMS_ADMIN))
        assert "projects.create" in result

    def test_allows_any_of_multiple_permissions(self):
        dep = require_permission("billing.manage", "projects.read")
        result = dep(_make_request(CLAIMS_VIEWER))
        assert "projects.read" in result

    def test_rejects_non_matching_permission(self):
        dep = require_permission("billing.manage")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(CLAIMS_VIEWER))
        assert exc_info.value.status_code == 403

    def test_rejects_without_tenant_context(self):
        dep = require_permission("projects.read")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(CLAIMS_NO_TENANT))
        assert exc_info.value.status_code == 403

    def test_rejects_without_claims(self):
        dep = require_permission("projects.read")
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            dep(request)
        assert exc_info.value.status_code == 401

    def test_handles_non_dict_tenant_info(self):
        claims = {"sub": "u1", "dct": "t1", "tenants": {"t1": None}}
        dep = require_permission("projects.read")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(claims))
        assert exc_info.value.status_code == 403


class TestRequireAnyPermissionAlias:
    def test_alias_is_same_function(self):
        assert require_any_permission is require_permission


class TestRequireAllPermissions:
    def test_rejects_zero_args(self):
        with pytest.raises(ValueError, match="at least one permission"):
            require_all_permissions()

    def test_allows_when_user_has_all(self):
        dep = require_all_permissions("projects.create", "projects.read")
        result = dep(_make_request(CLAIMS_ADMIN))
        assert "projects.create" in result
        assert "projects.read" in result

    def test_rejects_when_missing_one(self):
        dep = require_all_permissions("projects.read", "billing.manage")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(CLAIMS_ADMIN))
        assert exc_info.value.status_code == 403

    def test_allows_single_permission(self):
        dep = require_all_permissions("projects.read")
        result = dep(_make_request(CLAIMS_VIEWER))
        assert "projects.read" in result

    def test_rejects_without_tenant_context(self):
        dep = require_all_permissions("projects.read")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(CLAIMS_NO_TENANT))
        assert exc_info.value.status_code == 403

    def test_rejects_without_claims(self):
        dep = require_all_permissions("projects.read")
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            dep(request)
        assert exc_info.value.status_code == 401

    def test_handles_non_dict_tenant_info(self):
        claims = {"sub": "u1", "dct": "t1", "tenants": {"t1": None}}
        dep = require_all_permissions("projects.read")
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(claims))
        assert exc_info.value.status_code == 403
