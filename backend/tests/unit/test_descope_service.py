"""Unit tests for the Descope Management API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.descope import DescopeManagementClient, get_descope_client


@pytest.fixture
def client():
    return DescopeManagementClient("proj-123", "mgmt-key-456", "https://api.descope.com")


class TestDescopeManagementClient:
    def test_auth_header_format(self, client):
        assert client._auth_header == "Bearer proj-123:mgmt-key-456"

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_create_tenant(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"id": "new-tenant-id"}),
        )

        result = await client.create_tenant("Acme Corp", ["acme.com"])
        assert result == {"id": "new-tenant-id"}
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/tenant/create",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"name": "Acme Corp", "selfProvisioningDomains": ["acme.com"]},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_create_tenant_without_domains(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"id": "t1"}),
        )

        await client.create_tenant("Simple Corp")
        call_json = mock_http.post.call_args[1]["json"]
        assert "selfProvisioningDomains" not in call_json

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_tenants(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"tenants": [{"id": "t1", "name": "Acme"}]}),
        )

        result = await client.list_tenants()
        assert result == [{"id": "t1", "name": "Acme"}]

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_load_tenant(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"id": "t1", "name": "Acme"}),
        )

        result = await client.load_tenant("t1")
        assert result["name"] == "Acme"

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_delete_tenant(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
        )

        await client.delete_tenant("t1")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/tenant/delete",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"id": "t1"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_add_user_to_tenant(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
        )

        await client.add_user_to_tenant("user1", "t1")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/user/update/tenant/add",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"loginId": "user1", "tenantId": "t1"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_assign_roles(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
        )

        await client.assign_roles("user1", "t1", ["admin", "member"])
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/user/update/role/add",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"loginId": "user1", "tenantId": "t1", "roleNames": ["admin", "member"]},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_remove_roles(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
        )

        await client.remove_roles("user1", "t1", ["member"])
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/user/update/role/remove",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"loginId": "user1", "tenantId": "t1", "roleNames": ["member"]},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_load_user(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"user": {"name": "Test", "customAttributes": {"dept": "Eng"}}}),
        )

        result = await client.load_user("user1")
        assert result["name"] == "Test"
        assert result["customAttributes"]["dept"] == "Eng"

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_update_user_custom_attribute(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_user_custom_attribute("user1", "department", "Product")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/user/update/customAttribute",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"loginId": "user1", "attributeKey": "department", "attributeValue": "Product"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_update_tenant_custom_attributes(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_tenant_custom_attributes("t1", {"plan_tier": "pro"})
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/tenant/update",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"id": "t1", "customAttributes": {"plan_tier": "pro"}},
        )


class TestGetDescopeClient:
    def test_creates_client_from_env(self, monkeypatch):
        monkeypatch.setenv("DESCOPE_PROJECT_ID", "proj-env")
        monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "key-env")
        client = get_descope_client()
        assert client._auth_header == "Bearer proj-env:key-env"

    def test_uses_custom_base_url(self, monkeypatch):
        monkeypatch.setenv("DESCOPE_PROJECT_ID", "proj-env")
        monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "key-env")
        monkeypatch.setenv("DESCOPE_BASE_URL", "https://custom.api.com")
        client = get_descope_client()
        assert client.base_url == "https://custom.api.com"

    def test_raises_without_project_id(self, monkeypatch):
        monkeypatch.delenv("DESCOPE_PROJECT_ID", raising=False)
        with pytest.raises(KeyError):
            get_descope_client()
