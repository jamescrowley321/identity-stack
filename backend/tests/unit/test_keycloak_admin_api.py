"""KeycloakAdminApi settings and the startup credential check, against recorded answers."""

import httpx
import pytest

from app.services import keycloak_admin
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ISSUER_VARIABLE,
    KeycloakAdminError,
    KeycloakAdminNotConfigured,
    KeycloakAdminRolesMissing,
    KeycloakAdminSettings,
    connect_keycloak_admin,
    keycloak_admin_settings,
)
from tests.helpers.keycloak_admin_recorded import (
    ADMIN_CLIENT_ID,
    ADMIN_CLIENT_SECRET,
    DIRECTORY_CLIENT_ID,
    DIRECTORY_CLIENT_SECRET,
    ISSUER,
    RECORDED,
    ROLE_CHECK,
    TOKEN_PATH,
    RecordedAdminApi,
    admin_path,
)

SETTINGS = {
    ISSUER_VARIABLE: ISSUER,
    ADMIN_CLIENT_ID_VARIABLE: ADMIN_CLIENT_ID,
    ADMIN_CREDENTIAL_VARIABLE: ADMIN_CLIENT_SECRET,
}


def set_settings(monkeypatch, **overrides: str | None) -> None:
    for name, value in {**SETTINGS, **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


class TestSettings:
    def test_reads_the_issuer_and_the_admin_client(self, monkeypatch):
        set_settings(monkeypatch)

        assert keycloak_admin_settings() == KeycloakAdminSettings(
            issuer=ISSUER, client_id=ADMIN_CLIENT_ID, client_secret=ADMIN_CLIENT_SECRET
        )

    @pytest.mark.parametrize("missing", sorted(SETTINGS))
    @pytest.mark.parametrize("unset_value", [None, ""])
    def test_a_missing_setting_is_refused_by_name(self, monkeypatch, missing, unset_value):
        set_settings(monkeypatch, **{missing: unset_value})

        with pytest.raises(KeycloakAdminNotConfigured) as refused:
            keycloak_admin_settings()

        assert str(refused.value) == f"{missing} is not set"

    def test_an_issuer_that_names_no_realm_is_refused(self, monkeypatch):
        set_settings(monkeypatch, **{ISSUER_VARIABLE: "http://keycloak-test.localhost:18180"})

        with pytest.raises(KeycloakAdminNotConfigured) as refused:
            keycloak_admin_settings()

        assert str(refused.value) == f"{ISSUER_VARIABLE} is not <origin>/realms/<realm>"

    def test_the_secret_is_not_in_the_repr(self):
        settings = KeycloakAdminSettings(issuer=ISSUER, client_id=ADMIN_CLIENT_ID, client_secret=ADMIN_CLIENT_SECRET)

        assert ADMIN_CLIENT_SECRET not in repr(settings)


@pytest.mark.anyio
class TestCredentialCheck:
    async def test_a_working_credential_takes_a_token_and_reads_the_admin_api(self):
        api = RecordedAdminApi(["token", *ROLE_CHECK])

        await api.api().check_credentials()

        account = RECORDED["service_account_find"]["body"][0]["id"]
        realm_management = RECORDED["service_account_role_mappings"]["body"]["clientMappings"]["realm-management"]["id"]
        assert api.calls() == [
            ("POST", TOKEN_PATH),
            ("GET", admin_path("/organizations")),
            ("GET", admin_path("/users")),
            ("GET", admin_path(f"/users/{account}/role-mappings")),
            ("GET", admin_path(f"/users/{account}/role-mappings/clients/{realm_management}/composite")),
        ]
        assert api.sent[1].query == {"first": "0", "max": "1"}
        assert api.sent[2].query == {"username": f"service-account-{ADMIN_CLIENT_ID}", "exact": "true"}

    async def test_the_read_only_directory_client_lacks_the_roles(self):
        """Keycloak accepts its credential and lets it read: only the role check refuses it."""
        api = RecordedAdminApi(
            [
                "directory_token",
                "directory_credential_check",
                "directory_service_account_find",
                "directory_service_account_role_mappings",
            ]
        )

        with pytest.raises(KeycloakAdminRolesMissing) as refused:
            await api.api(client_id=DIRECTORY_CLIENT_ID, secret=DIRECTORY_CLIENT_SECRET).check_credentials()

        assert (refused.value.status, refused.value.missing) == (403, keycloak_admin.REQUIRED_ROLES)
        assert api.script == []

    async def test_a_service_account_with_one_of_the_two_roles_lacks_the_other(self):
        held = [role for role in RECORDED["service_account_roles"]["body"] if role["name"] == "manage-users"]
        api = RecordedAdminApi(["token", *ROLE_CHECK[:-1], httpx.Response(200, json=held)])

        with pytest.raises(KeycloakAdminRolesMissing) as refused:
            await api.api().check_credentials()

        assert refused.value.missing == frozenset({"manage-organizations"})

    async def test_a_service_account_that_cannot_be_found_lacks_the_roles(self):
        api = RecordedAdminApi(["token", "credential_check", httpx.Response(200, json=[])])

        with pytest.raises(KeycloakAdminRolesMissing) as refused:
            await api.api().check_credentials()

        assert refused.value.missing == keycloak_admin.REQUIRED_ROLES

    async def test_roles_that_are_not_a_list_are_an_error(self):
        api = RecordedAdminApi(["token", *ROLE_CHECK[:-1], httpx.Response(200, json={"name": "manage-users"})])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().check_credentials()

        assert not isinstance(refused.value, KeycloakAdminRolesMissing)
        assert refused.value.status is None

    @pytest.mark.parametrize("answer", ["token_wrong_secret", "token_unknown_client"])
    async def test_a_refused_credential_raises_with_the_status(self, answer):
        api = RecordedAdminApi([answer])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api(secret="wrong").check_credentials()

        assert refused.value.status == 401

    async def test_a_token_the_admin_api_refuses_raises(self):
        """A client without the kc-admin-api scope gets a token the Admin API answers 401."""
        api = RecordedAdminApi(["token", "admin_api_bad_token", "token", "admin_api_bad_token"])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().check_credentials()

        assert refused.value.status == 401

    async def test_a_service_account_without_the_roles_raises(self):
        api = RecordedAdminApi(["token", httpx.Response(403, json={"error": "HTTP 403 Forbidden"})])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().check_credentials()

        assert refused.value.status == 403

    async def test_no_answer_raises_with_no_status(self):
        api = RecordedAdminApi([httpx.ConnectError("connection refused")])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().check_credentials()

        assert refused.value.status is None


@pytest.mark.anyio
class TestConnect:
    async def test_checks_the_credential_before_answering(self, monkeypatch):
        set_settings(monkeypatch)
        api = RecordedAdminApi(["token", *ROLE_CHECK])
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(api.handle), timeout=keycloak_admin.ADMIN_TIMEOUT_SECONDS
        )

        connected = await connect_keycloak_admin(http)

        assert connected.settings == keycloak_admin_settings()
        assert api.script == []

    async def test_a_refused_credential_raises(self, monkeypatch):
        set_settings(monkeypatch, **{ADMIN_CREDENTIAL_VARIABLE: "wrong"})
        api = RecordedAdminApi(["token_wrong_secret"])
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(api.handle), timeout=keycloak_admin.ADMIN_TIMEOUT_SECONDS
        )

        with pytest.raises(KeycloakAdminError):
            await connect_keycloak_admin(http)

    async def test_a_missing_setting_raises_before_any_call(self, monkeypatch):
        set_settings(monkeypatch, **{ADMIN_CREDENTIAL_VARIABLE: None})
        api = RecordedAdminApi([])
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(api.handle), timeout=keycloak_admin.ADMIN_TIMEOUT_SECONDS
        )

        with pytest.raises(KeycloakAdminNotConfigured):
            await connect_keycloak_admin(http)

        assert api.sent == []


@pytest.mark.anyio
class TestReadsById:
    """The reads the adapter makes by Keycloak id: a linked user, an organization with its attributes."""

    async def test_get_user_answers_the_user_with_that_id(self):
        user = RECORDED["user_get"]["body"]
        api = RecordedAdminApi(["token", "user_get"])

        found = await api.api().get_user(user["id"])

        assert found == user
        assert api.calls() == [("POST", TOKEN_PATH), ("GET", admin_path(f"/users/{user['id']}"))]

    async def test_get_user_answers_none_for_a_user_the_realm_lacks(self):
        gone = RECORDED["user_get"]["body"]["id"]
        api = RecordedAdminApi(["token", "user_get_none"])

        assert await api.api().get_user(gone) is None

    async def test_get_user_does_not_read_a_realm_wide_404_as_no_user(self):
        api = RecordedAdminApi(["token", httpx.Response(404, json={"error": "Realm not found."})])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().get_user("c0b53945-7a52-4072-b076-dfdacf5d9700")

        assert refused.value.status == 404

    async def test_get_user_refuses_an_answer_for_another_id(self):
        api = RecordedAdminApi(["token", httpx.Response(200, json=RECORDED["user_get"]["body"])])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().get_user("another-id")

        assert refused.value.status is None

    async def test_get_organization_answers_it_with_its_attributes(self):
        organization = RECORDED["organization_get_marked"]["body"]
        api = RecordedAdminApi(["token", "organization_get_marked"])

        found = await api.api().get_organization(organization["id"])

        assert found == organization
        assert api.calls() == [("POST", TOKEN_PATH), ("GET", admin_path(f"/organizations/{organization['id']}"))]

    async def test_get_organization_refuses_an_organization_not_there(self):
        api = RecordedAdminApi(["token", httpx.Response(404, json={"errorMessage": "Organization not found."})])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().get_organization("49825999-d1c8-41ee-a4a8-f3590a592959")

        assert refused.value.status == 404
