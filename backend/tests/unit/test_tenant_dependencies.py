"""Unit tests for tenant dependency functions."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.dependencies.tenant import get_tenant_claims, get_tenant_id


class TestGetTenantId:
    def test_returns_tenant_id_when_present(self):
        request = MagicMock()
        request.state.claims = {"sub": "user123", "dct": "tenant-abc"}
        result = get_tenant_id(request)
        assert result == "tenant-abc"

    def test_raises_403_when_no_dct_claim(self):
        request = MagicMock()
        request.state.claims = {"sub": "user123"}
        with pytest.raises(HTTPException) as exc_info:
            get_tenant_id(request)
        assert exc_info.value.status_code == 403

    def test_raises_403_when_dct_is_empty(self):
        request = MagicMock()
        request.state.claims = {"sub": "user123", "dct": ""}
        with pytest.raises(HTTPException) as exc_info:
            get_tenant_id(request)
        assert exc_info.value.status_code == 403

    def test_raises_401_when_no_claims(self):
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            get_tenant_id(request)
        assert exc_info.value.status_code == 401


class TestGetTenantClaims:
    def test_returns_tenant_claims_when_present(self):
        request = MagicMock()
        request.state.claims = {
            "sub": "user123",
            "tenants": {"t1": {"roles": ["admin"], "permissions": ["read"]}},
        }
        result = get_tenant_claims(request)
        assert result == {"t1": {"roles": ["admin"], "permissions": ["read"]}}

    def test_returns_empty_dict_when_no_tenants(self):
        request = MagicMock()
        request.state.claims = {"sub": "user123"}
        result = get_tenant_claims(request)
        assert result == {}

    def test_raises_401_when_no_claims(self):
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            get_tenant_claims(request)
        assert exc_info.value.status_code == 401
