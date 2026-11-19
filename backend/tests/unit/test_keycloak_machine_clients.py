"""KeycloakSyncAdapter's machine credential clients (``MachineCredentialPush``), on a faked admin API.

What Keycloak 26.8.2 answers is covered live by
``tests/integration/test_keycloak_machine_credentials.py``: client registration with
``create-client``, the audience mapper the realm allows, a disabled service account
refused the grant (401 ``invalid_request``), and a deleted client (401 ``invalid_client``).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from expression import Ok

from app.middleware.providers import KEYCLOAK_MACHINE_CLIENT_PREFIX
from app.services.adapters.base import MachineClient, MachineClientRef
from app.services.adapters.keycloak import (
    CREDENTIAL_ATTRIBUTE,
    ORGANIZATION_ATTRIBUTE,
    TENANT_ATTRIBUTE,
    KeycloakSyncAdapter,
    Refusal,
    machine_client_id,
)
from app.services.keycloak_admin import KeycloakAdminError
from tests.helpers.keycloak_links import MemoryKeycloakLinks

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
CREDENTIAL = uuid.UUID("44444444-4444-4444-8444-444444444444")
API_CLIENT = "identity-stack-api"
CLIENT_ID = machine_client_id(CREDENTIAL)
REGISTERED = {"id": "kc-client", "clientId": CLIENT_ID, "secret": "s3cret", "registrationAccessToken": "rat"}
ACCOUNT = {"id": "kc-sub", "username": f"service-account-{CLIENT_ID}", "enabled": True}
REF = MachineClientRef(client_id=CLIENT_ID, external_id="kc-client", subject="kc-sub", registration_token="rat")


@pytest.fixture
def api() -> AsyncMock:
    api = AsyncMock()
    api.settings = SimpleNamespace(api_client_id=API_CLIENT)
    api.register_client.return_value = REGISTERED
    api.find_user.return_value = ACCOUNT
    api.get_user.return_value = ACCOUNT
    api.unregister_client.return_value = True
    return api


@pytest.fixture
async def links() -> MemoryKeycloakLinks:
    links = MemoryKeycloakLinks()
    await links.link_organization(TENANT, "acme")
    return links


async def _create(api, links):
    return await KeycloakSyncAdapter(api, links).create_machine_client(
        credential_id=CREDENTIAL, tenant_id=TENANT, name="robot"
    )


class TestCreate:
    async def test_registers_a_client_credentials_only_client_owned_by_the_tenants_organization(self, api, links):
        result = await _create(api, links)

        assert result == Ok(
            MachineClient(
                client_id=CLIENT_ID,
                external_id="kc-client",
                subject="kc-sub",
                secret="s3cret",
                registration_token="rat",
            )
        )
        (representation,), _ = api.register_client.await_args
        assert {
            key: representation[key]
            for key in (
                "clientId",
                "publicClient",
                "serviceAccountsEnabled",
                "standardFlowEnabled",
                "implicitFlowEnabled",
                "directAccessGrantsEnabled",
                "fullScopeAllowed",
                "attributes",
            )
        } == {
            "clientId": f"{KEYCLOAK_MACHINE_CLIENT_PREFIX}{CREDENTIAL}",
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "fullScopeAllowed": False,
            "attributes": {
                CREDENTIAL_ATTRIBUTE: str(CREDENTIAL),
                TENANT_ATTRIBUTE: str(TENANT),
                ORGANIZATION_ATTRIBUTE: "acme",
            },
        }
        assert [
            (mapper["protocolMapper"], mapper["config"]["included.client.audience"])
            for mapper in representation["protocolMappers"]
        ] == [("oidc-audience-mapper", API_CLIENT)]
        api.find_user.assert_awaited_once_with(f"service-account-{CLIENT_ID}")

    async def test_a_tenant_with_no_organization_is_refused_before_keycloak(self, api):
        result = await _create(api, MemoryKeycloakLinks())

        assert result.error.context["refusal"] is Refusal.NO_ORGANIZATION
        api.register_client.assert_not_awaited()

    async def test_a_client_without_its_service_account_is_deleted_again(self, api, links):
        api.find_user.return_value = None

        result = await _create(api, links)

        assert result.error.context["refusal"] is Refusal.NO_SERVICE_ACCOUNT
        api.unregister_client.assert_awaited_once_with(CLIENT_ID, "rat")

    async def test_a_refused_registration_is_an_error_with_its_status(self, api, links):
        api.register_client.side_effect = KeycloakAdminError("refused", status=403)

        result = await _create(api, links)

        assert (result.error.operation, result.error.context["status"]) == ("create_machine_client", 403)


class TestEnable:
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_sets_the_service_account_only_when_it_differs(self, api, links, enabled):
        api.get_user.return_value = {**ACCOUNT, "enabled": not enabled}

        result = await KeycloakSyncAdapter(api, links).set_machine_client_enabled(client=REF, enabled=enabled)

        assert result == Ok(None)
        api.update_user.assert_awaited_once_with("kc-sub", {"enabled": enabled})

    async def test_an_unchanged_service_account_is_not_written(self, api, links):
        result = await KeycloakSyncAdapter(api, links).set_machine_client_enabled(client=REF, enabled=True)

        assert result == Ok(None)
        api.update_user.assert_not_awaited()

    @pytest.mark.parametrize(
        "account", [None, {"id": "kc-sub", "username": "bob", "enabled": True}], ids=["gone", "a person"]
    )
    async def test_only_a_service_account_is_changed(self, api, links, account):
        api.get_user.return_value = account

        result = await KeycloakSyncAdapter(api, links).set_machine_client_enabled(client=REF, enabled=False)

        assert result.error.context["refusal"] is Refusal.NO_SERVICE_ACCOUNT
        api.update_user.assert_not_awaited()


class TestDelete:
    async def test_deletes_with_the_registration_token(self, api, links):
        result = await KeycloakSyncAdapter(api, links).delete_machine_client(client=REF)

        assert result == Ok(None)
        api.unregister_client.assert_awaited_once_with(CLIENT_ID, "rat")

    async def test_a_refused_token_for_a_client_already_gone_is_deleted(self, api, links):
        api.unregister_client.return_value = False
        api.find_user.return_value = None

        assert await KeycloakSyncAdapter(api, links).delete_machine_client(client=REF) == Ok(None)

    async def test_a_refused_token_for_a_client_still_there_is_an_error(self, api, links):
        api.unregister_client.return_value = False

        result = await KeycloakSyncAdapter(api, links).delete_machine_client(client=REF)

        assert result.error.context["refusal"] is Refusal.REGISTRATION_REFUSED
