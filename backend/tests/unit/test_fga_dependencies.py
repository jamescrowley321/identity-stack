"""Unit tests for the require_fga dependency factory and extract_user_id helper."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.dependencies.fga import extract_user_id, require_fga


def _make_request(claims, path_params=None):
    request = MagicMock()
    request.state.claims = claims
    request.path_params = path_params or {}
    return request


def _make_http_status_error(status_code=500):
    req = httpx.Request("POST", "https://api.descope.com/v1/mgmt/authz/has")
    resp = httpx.Response(status_code, request=req, text="error")
    return httpx.HTTPStatusError(f"{status_code}", request=req, response=resp)


def _make_request_error():
    req = httpx.Request("POST", "https://api.descope.com/v1/mgmt/authz/has")
    return httpx.RequestError("Connection refused", request=req)


VALID_CLAIMS = {"sub": "user-abc", "dct": "tenant-1"}


class TestExtractUserId:
    def test_returns_user_id(self):
        assert extract_user_id(_make_request(VALID_CLAIMS)) == "user-abc"

    def test_no_claims_attribute_raises_401(self):
        request = MagicMock()
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            extract_user_id(request)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Not authenticated"

    def test_non_dict_claims_raises_401(self):
        """Claims set to a non-dict type (e.g., string) -> 401."""
        with pytest.raises(HTTPException) as exc_info:
            extract_user_id(_make_request("not-a-dict"))
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Not authenticated"

    def test_none_claims_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            extract_user_id(_make_request(None))
        assert exc_info.value.status_code == 401

    def test_missing_sub_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            extract_user_id(_make_request({"dct": "tenant-1"}))
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Missing user identity"

    def test_empty_sub_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            extract_user_id(_make_request({"sub": "", "dct": "t1"}))
        assert exc_info.value.status_code == 401


class TestRequireFga:
    @pytest.mark.anyio
    @patch("app.dependencies.fga.get_descope_client")
    async def test_allowed_returns_user_id(self, mock_factory):
        mock_client = AsyncMock()
        mock_client.check_permission.return_value = True
        mock_factory.return_value = mock_client

        dep = require_fga("document", "can_view")
        result = await dep(_make_request(VALID_CLAIMS, {"document_id": "doc-123"}))
        assert result == "user-abc"
        mock_client.check_permission.assert_called_once_with("document", "doc-123", "can_view", "user-abc")

    @pytest.mark.anyio
    @patch("app.dependencies.fga.get_descope_client")
    async def test_denied_raises_403(self, mock_factory):
        mock_client = AsyncMock()
        mock_client.check_permission.return_value = False
        mock_factory.return_value = mock_client

        dep = require_fga("document", "can_edit")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(VALID_CLAIMS, {"document_id": "doc-123"}))
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Access denied"

    @pytest.mark.anyio
    @patch("app.dependencies.fga.get_descope_client")
    async def test_denied_logs_warning(self, mock_factory, caplog):
        """AC3: FGA denial is logged with warning level for audit trail."""
        mock_client = AsyncMock()
        mock_client.check_permission.return_value = False
        mock_factory.return_value = mock_client

        dep = require_fga("document", "can_edit")
        with caplog.at_level(logging.WARNING, logger="app.dependencies.fga"):
            with pytest.raises(HTTPException):
                await dep(_make_request(VALID_CLAIMS, {"document_id": "doc-123"}))
        assert any("FGA denied" in r.message for r in caplog.records)
        assert any("user-abc" in r.message for r in caplog.records)

    @pytest.mark.anyio
    async def test_no_claims_attribute_raises_401(self):
        request = MagicMock()
        request.state = MagicMock(spec=[])
        request.path_params = {"document_id": "doc-123"}

        dep = require_fga("document", "can_view")
        with pytest.raises(HTTPException) as exc_info:
            await dep(request)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Not authenticated"

    @pytest.mark.anyio
    async def test_none_claims_raises_401(self):
        dep = require_fga("document", "can_view")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(None, {"document_id": "doc-123"}))
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_missing_sub_raises_401(self):
        dep = require_fga("document", "can_view")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request({"dct": "t-1"}, {"document_id": "doc-123"}))
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Missing user identity"

    @pytest.mark.anyio
    async def test_empty_sub_raises_401(self):
        dep = require_fga("document", "can_view")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request({"sub": "", "dct": "t1"}, {"document_id": "doc-123"}))
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    @patch("app.dependencies.fga.get_descope_client")
    async def test_http_error_raises_502_fail_closed(self, mock_factory):
        mock_client = AsyncMock()
        mock_client.check_permission.side_effect = _make_http_status_error(500)
        mock_factory.return_value = mock_client

        dep = require_fga("document", "can_view")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(VALID_CLAIMS, {"document_id": "doc-123"}))
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail == "Authorization check failed"

    @pytest.mark.anyio
    @patch("app.dependencies.fga.get_descope_client")
    async def test_network_error_raises_502_fail_closed(self, mock_factory):
        mock_client = AsyncMock()
        mock_client.check_permission.side_effect = _make_request_error()
        mock_factory.return_value = mock_client

        dep = require_fga("document", "can_view")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(VALID_CLAIMS, {"document_id": "doc-123"}))
        assert exc_info.value.status_code == 502

    @pytest.mark.anyio
    @patch("app.dependencies.fga.get_descope_client")
    async def test_none_result_treated_as_denied(self, mock_factory):
        """check_permission returns None -> treated as denied (fail-closed)."""
        mock_client = AsyncMock()
        mock_client.check_permission.return_value = None
        mock_factory.return_value = mock_client

        dep = require_fga("document", "can_view")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(VALID_CLAIMS, {"document_id": "doc-123"}))
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    @patch("app.dependencies.fga.get_descope_client")
    async def test_custom_resource_id_param(self, mock_factory):
        """Verify factory uses the custom resource_id_param name."""
        mock_client = AsyncMock()
        mock_client.check_permission.return_value = True
        mock_factory.return_value = mock_client

        dep = require_fga("folder", "can_delete", resource_id_param="folder_id")
        await dep(_make_request(VALID_CLAIMS, {"folder_id": "folder-99"}))
        mock_client.check_permission.assert_called_once_with("folder", "folder-99", "can_delete", "user-abc")

    @pytest.mark.anyio
    async def test_missing_resource_id_raises_400(self):
        """Path param not present -> 400."""
        dep = require_fga("document", "can_view")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(VALID_CLAIMS, {}))
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Missing resource identifier"
