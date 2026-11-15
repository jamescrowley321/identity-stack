"""Unit tests for DescopeManagementAdapter.

The adapter turns DescopeManagementClient calls into Result values carrying the
API's own response shapes. Upstream failures the routers never caught still
propagate (docs/conventions.md §4).
"""

from unittest.mock import AsyncMock

import httpx
import pytest
from expression import Ok

from app.errors.identity import Forbidden, NotFound, ProviderError, ValidationError
from app.services.descope_management import DescopeManagementAdapter

TENANT_ID = "tenant-1"
OTHER_TENANT_ID = "tenant-2"
USER_ID = "U2abc123"
KEY_ID = "K1"


def _status_error(status_code: int, json: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/x")
    response = httpx.Response(status_code, request=request, json=json or {})
    return httpx.HTTPStatusError(str(status_code), request=request, response=response)


def _transport_error() -> httpx.ConnectError:
    return httpx.ConnectError("refused", request=httpx.Request("POST", "https://api.descope.com/v1/mgmt/x"))


@pytest.fixture
def client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def adapter(client) -> DescopeManagementAdapter:
    return DescopeManagementAdapter(client)


@pytest.mark.anyio
class TestMembers:
    async def test_list_members_returns_the_api_shape(self, adapter, client):
        client.search_tenant_users.return_value = [
            {
                "userId": USER_ID,
                "email": "alice@example.com",
                "name": "Alice",
                "status": "disabled",
                "loginIds": ["alice@example.com"],
                "userTenants": [
                    {"tenantId": OTHER_TENANT_ID, "roleNames": ["owner"]},
                    {"tenantId": TENANT_ID, "roleNames": ["admin"]},
                ],
            },
            {},
        ]

        result = await adapter.list_members(TENANT_ID)

        assert result == Ok(
            {
                "members": [
                    {
                        "userId": USER_ID,
                        "email": "alice@example.com",
                        "name": "Alice",
                        "status": "disabled",
                        "roleNames": ["admin"],
                    },
                    {"userId": "", "email": "", "name": "", "status": "enabled", "roleNames": []},
                ]
            }
        )
        client.search_tenant_users.assert_awaited_once_with(TENANT_ID)

    async def test_list_members_status_error_is_a_provider_error(self, adapter, client):
        client.search_tenant_users.side_effect = _status_error(500)

        result = await adapter.list_members(TENANT_ID)

        assert isinstance(result.error, ProviderError)

    async def test_invite_member_returns_the_canonical_user(self, adapter, client):
        client.invite_user.return_value = {"userId": USER_ID, "createdTime": 1700000000}

        result = await adapter.invite_member(email="new@example.com", tenant_id=TENANT_ID, role_names=["member"])

        assert result == Ok(
            {
                "status": "invited",
                "email": "new@example.com",
                "user": {
                    "userId": USER_ID,
                    "createdTime": 1700000000,
                    "id": USER_ID,
                    "created_at": "2023-11-14T22:13:20+00:00",
                    "updated_at": "2023-11-14T22:13:20+00:00",
                },
            }
        )
        client.invite_user.assert_awaited_once_with(email="new@example.com", tenant_id=TENANT_ID, role_names=["member"])

    async def test_invite_member_400_carries_the_upstream_description(self, adapter, client):
        client.invite_user.side_effect = _status_error(400, {"errorDescription": "User already exists"})

        result = await adapter.invite_member(email="new@example.com", tenant_id=TENANT_ID, role_names=["member"])

        assert result.error == ValidationError(message="User already exists")

    async def test_invite_member_400_without_a_description(self, adapter, client):
        client.invite_user.side_effect = _status_error(400)

        result = await adapter.invite_member(email="new@example.com", tenant_id=TENANT_ID, role_names=["member"])

        assert result.error == ValidationError(message="Bad request")

    async def test_invite_member_other_status_is_a_provider_error(self, adapter, client):
        client.invite_user.side_effect = _status_error(500)

        result = await adapter.invite_member(email="new@example.com", tenant_id=TENANT_ID, role_names=["member"])

        assert isinstance(result.error, ProviderError)

    @pytest.mark.parametrize("status", ["enabled", "disabled"])
    async def test_set_member_status(self, adapter, client, status):
        result = await adapter.set_member_status(USER_ID, status)

        assert result == Ok(None)
        client.update_user_status.assert_awaited_once_with(USER_ID, status)

    async def test_set_member_status_error_is_a_provider_error(self, adapter, client):
        client.update_user_status.side_effect = _status_error(404)

        result = await adapter.set_member_status(USER_ID, "disabled")

        assert isinstance(result.error, ProviderError)

    async def test_remove_member(self, adapter, client):
        result = await adapter.remove_member(USER_ID, TENANT_ID)

        assert result == Ok(None)
        client.remove_user_from_tenant.assert_awaited_once_with(USER_ID, TENANT_ID)

    async def test_remove_member_status_error_is_a_provider_error(self, adapter, client):
        client.remove_user_from_tenant.side_effect = _status_error(500)

        result = await adapter.remove_member(USER_ID, TENANT_ID)

        assert isinstance(result.error, ProviderError)

    async def test_transport_failures_propagate(self, adapter, client):
        """§4 exception: the member routes never caught a transport failure."""
        for method in (
            client.search_tenant_users,
            client.invite_user,
            client.update_user_status,
            client.remove_user_from_tenant,
        ):
            method.side_effect = _transport_error()

        with pytest.raises(httpx.ConnectError):
            await adapter.list_members(TENANT_ID)
        with pytest.raises(httpx.ConnectError):
            await adapter.invite_member(email="new@example.com", tenant_id=TENANT_ID, role_names=["member"])
        with pytest.raises(httpx.ConnectError):
            await adapter.set_member_status(USER_ID, "enabled")
        with pytest.raises(httpx.ConnectError):
            await adapter.remove_member(USER_ID, TENANT_ID)


@pytest.mark.anyio
class TestProfile:
    async def test_load_profile_returns_the_api_shape(self, adapter, client):
        client.load_user.return_value = {
            "userId": USER_ID,
            "name": "Alice",
            "email": "alice@example.com",
            "customAttributes": {"department": "Engineering"},
        }

        result = await adapter.load_profile(USER_ID)

        assert result == Ok(
            {
                "user_id": USER_ID,
                "name": "Alice",
                "email": "alice@example.com",
                "custom_attributes": {"department": "Engineering"},
            }
        )
        client.load_user.assert_awaited_once_with(USER_ID)

    async def test_load_profile_of_an_unknown_user_is_blank(self, adapter, client):
        client.load_user.return_value = {}

        result = await adapter.load_profile(USER_ID)

        assert result == Ok({"user_id": USER_ID, "name": "", "email": "", "custom_attributes": {}})

    @pytest.mark.parametrize("failure", [_status_error(500), _transport_error()])
    async def test_load_profile_upstream_failure_is_a_provider_error(self, adapter, client, failure):
        client.load_user.side_effect = failure

        result = await adapter.load_profile(USER_ID)

        assert isinstance(result.error, ProviderError)

    async def test_update_profile_attribute_resolves_the_login_id(self, adapter, client):
        client.resolve_login_id.return_value = "alice@example.com"

        result = await adapter.update_profile_attribute(USER_ID, "department", "Engineering")

        assert result == Ok(None)
        client.resolve_login_id.assert_awaited_once_with(USER_ID)
        client.update_user_custom_attribute.assert_awaited_once_with("alice@example.com", "department", "Engineering")

    async def test_update_profile_attribute_status_error_is_a_provider_error(self, adapter, client):
        client.resolve_login_id.side_effect = _status_error(500)

        result = await adapter.update_profile_attribute(USER_ID, "department", "Engineering")

        assert isinstance(result.error, ProviderError)
        client.update_user_custom_attribute.assert_not_awaited()

    async def test_update_profile_attribute_other_failures_propagate(self, adapter, client):
        """§4 exception: neither a transport failure nor a user without loginIds was caught."""
        client.resolve_login_id.side_effect = ValueError("User U2abc123 has no loginIds")
        with pytest.raises(ValueError, match="no loginIds"):
            await adapter.update_profile_attribute(USER_ID, "department", "Engineering")

        client.resolve_login_id.side_effect = _transport_error()
        with pytest.raises(httpx.ConnectError):
            await adapter.update_profile_attribute(USER_ID, "department", "Engineering")


@pytest.mark.anyio
class TestTenantSettings:
    async def test_load_tenant_settings_returns_the_api_shape(self, adapter, client):
        client.load_tenant.return_value = {"id": TENANT_ID, "name": "Acme", "customAttributes": {"plan_tier": "pro"}}

        result = await adapter.load_tenant_settings(TENANT_ID)

        assert result == Ok({"tenant_id": TENANT_ID, "name": "Acme", "custom_attributes": {"plan_tier": "pro"}})
        client.load_tenant.assert_awaited_once_with(TENANT_ID)

    async def test_load_tenant_settings_of_an_unknown_tenant_is_not_found(self, adapter, client):
        client.load_tenant.return_value = {}

        result = await adapter.load_tenant_settings(TENANT_ID)

        assert isinstance(result.error, NotFound)

    async def test_load_tenant_settings_404_is_not_found(self, adapter, client):
        client.load_tenant.side_effect = _status_error(404)

        result = await adapter.load_tenant_settings(TENANT_ID)

        assert isinstance(result.error, NotFound)

    async def test_load_tenant_settings_other_status_is_a_provider_error(self, adapter, client):
        client.load_tenant.side_effect = _status_error(500)

        result = await adapter.load_tenant_settings(TENANT_ID)

        assert isinstance(result.error, ProviderError)

    async def test_update_tenant_settings(self, adapter, client):
        result = await adapter.update_tenant_settings(TENANT_ID, {"plan_tier": "pro"})

        assert result == Ok(None)
        client.update_tenant_custom_attributes.assert_awaited_once_with(TENANT_ID, {"plan_tier": "pro"})

    async def test_update_tenant_settings_status_error_is_a_provider_error(self, adapter, client):
        client.update_tenant_custom_attributes.side_effect = _status_error(500)

        result = await adapter.update_tenant_settings(TENANT_ID, {"plan_tier": "pro"})

        assert isinstance(result.error, ProviderError)

    async def test_transport_failures_propagate(self, adapter, client):
        """§4 exception: the tenant settings routes never caught a transport failure."""
        client.load_tenant.side_effect = _transport_error()
        client.update_tenant_custom_attributes.side_effect = _transport_error()

        with pytest.raises(httpx.ConnectError):
            await adapter.load_tenant_settings(TENANT_ID)
        with pytest.raises(httpx.ConnectError):
            await adapter.update_tenant_settings(TENANT_ID, {"plan_tier": "pro"})


@pytest.mark.anyio
class TestAccessKeys:
    async def test_create_access_key_returns_the_upstream_body(self, adapter, client):
        created = {"key": {"id": KEY_ID, "name": "ci"}, "cleartext": "secret"}
        client.create_access_key.return_value = created

        result = await adapter.create_access_key(
            name="ci", tenant_id=TENANT_ID, expire_time=1900000000, role_names=["admin"]
        )

        assert result == Ok(created)
        client.create_access_key.assert_awaited_once_with(
            name="ci", tenant_id=TENANT_ID, expire_time=1900000000, role_names=["admin"]
        )

    async def test_list_access_keys(self, adapter, client):
        client.search_access_keys.return_value = [{"id": KEY_ID}]

        result = await adapter.list_access_keys(TENANT_ID)

        assert result == Ok({"keys": [{"id": KEY_ID}]})
        client.search_access_keys.assert_awaited_once_with(TENANT_ID)

    async def test_load_tenant_access_key(self, adapter, client):
        key = {"id": KEY_ID, "keyTenants": [{"tenantId": TENANT_ID}]}
        client.load_access_key.return_value = key

        result = await adapter.load_tenant_access_key(KEY_ID, TENANT_ID)

        assert result == Ok(key)
        client.load_access_key.assert_awaited_once_with(KEY_ID)

    @pytest.mark.parametrize(
        "key",
        [
            {"id": KEY_ID, "keyTenants": [{"tenantId": OTHER_TENANT_ID}]},
            {"id": KEY_ID, "keyTenants": [{}]},
            {"id": KEY_ID, "keyTenants": []},
            {"id": KEY_ID, "keyTenants": None},
            {},
        ],
    )
    async def test_load_tenant_access_key_of_another_tenant_is_forbidden(self, adapter, client, key):
        client.load_access_key.return_value = key

        result = await adapter.load_tenant_access_key(KEY_ID, TENANT_ID)

        assert isinstance(result.error, Forbidden)

    @pytest.mark.parametrize(
        ("adapter_method", "client_method"),
        [
            ("deactivate_access_key", "deactivate_access_key"),
            ("activate_access_key", "activate_access_key"),
            ("delete_access_key", "delete_access_key"),
        ],
    )
    async def test_key_commands(self, adapter, client, adapter_method, client_method):
        result = await getattr(adapter, adapter_method)(KEY_ID)

        assert result == Ok(None)
        getattr(client, client_method).assert_awaited_once_with(KEY_ID)

    @pytest.mark.parametrize("failure", [_status_error(500), _transport_error()])
    async def test_upstream_failures_propagate(self, adapter, client, failure):
        """§4 exception: the access key routes never caught an upstream failure."""
        for method in (
            client.create_access_key,
            client.search_access_keys,
            client.load_access_key,
            client.deactivate_access_key,
            client.activate_access_key,
            client.delete_access_key,
        ):
            method.side_effect = failure

        with pytest.raises(type(failure)):
            await adapter.create_access_key(name="ci", tenant_id=TENANT_ID, expire_time=1900000000, role_names=None)
        with pytest.raises(type(failure)):
            await adapter.list_access_keys(TENANT_ID)
        with pytest.raises(type(failure)):
            await adapter.load_tenant_access_key(KEY_ID, TENANT_ID)
        with pytest.raises(type(failure)):
            await adapter.deactivate_access_key(KEY_ID)
        with pytest.raises(type(failure)):
            await adapter.activate_access_key(KEY_ID)
        with pytest.raises(type(failure)):
            await adapter.delete_access_key(KEY_ID)
