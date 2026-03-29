"""Unit tests for the Descope Management API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
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
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_tenant_custom_attributes("t1", {"plan_tier": "pro"})
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/tenant/update",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"id": "t1", "customAttributes": {"plan_tier": "pro"}},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_create_access_key(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"key": {"id": "k1"}, "cleartext": "secret"}),
        )

        result = await client.create_access_key("Test Key", "t1", role_names=["viewer"])
        assert result["cleartext"] == "secret"
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/accesskey/create",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"name": "Test Key", "tenantId": "t1", "roleNames": ["viewer"]},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_search_access_keys(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"keys": [{"id": "k1"}]}),
        )

        result = await client.search_access_keys("t1")
        assert len(result) == 1

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_deactivate_access_key(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.deactivate_access_key("k1")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/accesskey/deactivate",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"id": "k1"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_delete_access_key(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.delete_access_key("k1")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/accesskey/delete",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"id": "k1"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_invite_user(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"user": {"userId": "u1", "email": "a@b.com"}}),
        )

        result = await client.invite_user("a@b.com", "t1", ["member"])
        assert result["email"] == "a@b.com"
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/user/create",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"loginId": "a@b.com", "email": "a@b.com", "tenants": [{"tenantId": "t1", "roleNames": ["member"]}]},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_search_tenant_users(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"users": [{"userId": "u1"}]}),
        )

        result = await client.search_tenant_users("t1")
        assert len(result) == 1

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_update_user_status(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_user_status("u1", "disabled")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/user/updateStatus",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"loginId": "u1", "status": "disabled"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_remove_user_from_tenant(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.remove_user_from_tenant("u1", "t1")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/user/update/tenant/remove",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"loginId": "u1", "tenantId": "t1"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_permissions(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"permissions": [{"name": "reports.read", "description": "View reports"}]}),
        )

        result = await client.list_permissions()
        assert result == [{"name": "reports.read", "description": "View reports"}]
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/permission/all",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_create_permission(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.create_permission("reports.read", "View reports")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/permission/create",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"name": "reports.read", "description": "View reports"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_update_permission(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_permission("reports.read", "reports.view", "View reports")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/permission/update",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"name": "reports.read", "newName": "reports.view", "description": "View reports"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_delete_permission(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.delete_permission("reports.read")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/permission/delete",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"name": "reports.read"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_roles(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"roles": [{"name": "admin", "description": "Admin role"}]}),
        )

        result = await client.list_roles()
        assert result == [{"name": "admin", "description": "Admin role"}]
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/role/all",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_create_role(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.create_role("editor", "Can edit", ["docs.write", "docs.read"])
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/role/create",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"name": "editor", "description": "Can edit", "permissionNames": ["docs.write", "docs.read"]},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_create_role_without_permissions(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.create_role("viewer", "Read only")
        call_json = mock_http.post.call_args[1]["json"]
        assert "permissionNames" not in call_json
        assert call_json["name"] == "viewer"

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_update_role(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_role("editor", "senior-editor", "Senior editor", ["docs.write"])
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/role/update",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={
                "name": "editor",
                "newName": "senior-editor",
                "description": "Senior editor",
                "permissionNames": ["docs.write"],
            },
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_update_role_without_description(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_role("editor", "senior-editor")
        call_json = mock_http.post.call_args[1]["json"]
        assert "description" not in call_json
        assert "permissionNames" not in call_json
        assert call_json["name"] == "editor"
        assert call_json["newName"] == "senior-editor"

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_update_role_without_permissions(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_role("editor", "senior-editor", "Senior editor")
        call_json = mock_http.post.call_args[1]["json"]
        assert "permissionNames" not in call_json

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_delete_role(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.delete_role("editor")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/role/delete",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"name": "editor"},
        )

    # --- FGA method tests ---

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_get_fga_schema(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"schema": {"name": "test-schema"}}),
        )

        result = await client.get_fga_schema()
        assert result == {"name": "test-schema"}
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/authz/schema/load",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_update_fga_schema(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.update_fga_schema('{"name": "my-schema"}')
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/authz/schema/save",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"schema": '{"name": "my-schema"}'},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_create_relation(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.create_relation("document", "doc-123", "editor", "user:u1")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/authz/re/save",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={
                "resourceType": "document",
                "resource": "doc-123",
                "relationDefinition": "editor",
                "target": "user:u1",
            },
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_delete_relation(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.delete_relation("document", "doc-123", "editor", "user:u1")
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/authz/re/delete",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={
                "resourceType": "document",
                "resource": "doc-123",
                "relationDefinition": "editor",
                "target": "user:u1",
            },
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_relations(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(
                return_value={
                    "relationInfo": [
                        {"target": "user:u1", "relationDefinition": "editor"},
                        {"target": "user:u2", "relationDefinition": "viewer"},
                    ]
                }
            ),
        )

        result = await client.list_relations("document", "doc-123")
        assert len(result) == 2
        assert result[0]["target"] == "user:u1"
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/authz/re/who",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={"resourceType": "document", "resource": "doc-123"},
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_relations_empty(self, mock_cls, client):
        """AC: list_relations on resource with no relations returns empty list."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={}),
        )

        result = await client.list_relations("document", "doc-empty")
        assert result == []

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_user_resources(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"resources": [{"resource": "doc-1"}, {"resource": "doc-2"}]}),
        )

        result = await client.list_user_resources("document", "editor", "user:u1")
        assert len(result) == 2
        assert result[0]["resource"] == "doc-1"
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/authz/re/resource",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={
                "resourceType": "document",
                "relationDefinition": "editor",
                "target": "user:u1",
            },
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_user_resources_empty(self, mock_cls, client):
        """AC: list_user_resources for user with no resources returns empty list."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={}),
        )

        result = await client.list_user_resources("document", "editor", "user:u1")
        assert result == []

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_check_permission_allowed(self, mock_cls, client):
        """AC: check_permission returns True when Descope returns allowed: true."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"allowed": True}),
        )

        result = await client.check_permission("document", "doc-123", "editor", "user:u1")
        assert result is True
        mock_http.post.assert_called_once_with(
            "https://api.descope.com/v1/mgmt/authz/re/has",
            headers={"Authorization": "Bearer proj-123:mgmt-key-456"},
            json={
                "resourceType": "document",
                "resource": "doc-123",
                "relationDefinition": "editor",
                "target": "user:u1",
            },
        )

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_check_permission_denied(self, mock_cls, client):
        """AC: check_permission returns False when Descope returns allowed: false."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"allowed": False}),
        )

        result = await client.check_permission("document", "doc-123", "editor", "user:u1")
        assert result is False

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "method_name,args",
        [
            ("get_fga_schema", []),
            ("update_fga_schema", ["type user"]),
            ("create_relation", ["document", "doc-1", "owner", "user:u1"]),
            ("delete_relation", ["document", "doc-1", "owner", "user:u1"]),
            ("list_relations", ["document", "doc-1"]),
            ("list_user_resources", ["document", "can_view", "user:u1"]),
            ("check_permission", ["document", "doc-1", "can_view", "user:u1"]),
        ],
    )
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_fga_method_propagates_http_error(self, mock_cls, client, method_name, args):
        """All FGA methods propagate HTTPStatusError from Descope."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_response = MagicMock(status_code=400, text="Bad Request")
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "400 Bad Request", request=MagicMock(), response=mock_response
        )
        mock_http.post.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await getattr(client, method_name)(*args)

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "method_name,args",
        [
            ("get_fga_schema", []),
            ("update_fga_schema", ["type user"]),
            ("create_relation", ["document", "doc-1", "owner", "user:u1"]),
            ("delete_relation", ["document", "doc-1", "owner", "user:u1"]),
            ("list_relations", ["document", "doc-1"]),
            ("list_user_resources", ["document", "can_view", "user:u1"]),
            ("check_permission", ["document", "doc-1", "can_view", "user:u1"]),
        ],
    )
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_fga_method_propagates_network_error(self, mock_cls, client, method_name, args):
        """All FGA methods propagate RequestError on network failure."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.side_effect = httpx.RequestError("Connection refused", request=MagicMock())

        with pytest.raises(httpx.RequestError):
            await getattr(client, method_name)(*args)

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_check_permission_missing_allowed_field(self, mock_cls, client):
        """Edge case: if 'allowed' field is missing, defaults to False (fail-closed)."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={}),
        )

        result = await client.check_permission("document", "doc-123", "editor", "user:u1")
        assert result is False

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_check_permission_null_allowed_field(self, mock_cls, client):
        """Edge case: if 'allowed' is null, returns False (fail-closed, coerced to bool)."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"allowed": None}),
        )

        result = await client.check_permission("document", "doc-123", "editor", "user:u1")
        assert result is False
        assert isinstance(result, bool)

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_relations_null_value(self, mock_cls, client):
        """Edge case: if 'relationInfo' is null, returns empty list."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"relationInfo": None}),
        )

        result = await client.list_relations("document", "doc-123")
        assert result == []

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_user_resources_null_value(self, mock_cls, client):
        """Edge case: if 'resources' is null, returns empty list."""
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        mock_http.post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"resources": None}),
        )

        result = await client.list_user_resources("document", "editor", "user:u1")
        assert result == []


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

    def test_raises_without_management_key(self, monkeypatch):
        monkeypatch.setenv("DESCOPE_PROJECT_ID", "proj-env")
        monkeypatch.delenv("DESCOPE_MANAGEMENT_KEY", raising=False)
        with pytest.raises(KeyError):
            get_descope_client()
