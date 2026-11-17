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
    KeycloakAdminSettings,
    connect_keycloak_admin,
    keycloak_admin_settings,
)
from tests.helpers.keycloak_admin_recorded import (
    ADMIN_CLIENT_ID,
    ADMIN_CLIENT_SECRET,
    ISSUER,
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
        api = RecordedAdminApi(["token", "credential_check"])

        await api.api().check_credentials()

        assert api.calls() == [("POST", TOKEN_PATH), ("GET", admin_path("/organizations"))]
        assert api.sent[1].query == {"first": "0", "max": "1"}

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
        api = RecordedAdminApi(["token", "credential_check"])
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
