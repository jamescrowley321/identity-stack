"""KeycloakAdminApi settings and the startup credential check, against recorded answers."""

import base64
import logging
from urllib.parse import urlsplit

import httpx
import pytest

from app.services import keycloak_admin
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    API_CLIENT_VARIABLE,
    ISSUER_VARIABLE,
    KeycloakAdminError,
    KeycloakAdminNotConfigured,
    KeycloakAdminRolesMissing,
    KeycloakAdminSettings,
    KeycloakApiClientNotManaged,
    connect_keycloak_admin,
    keycloak_admin_settings,
)
from tests.helpers.keycloak_admin_recorded import (
    ADMIN_CLIENT_ID,
    ADMIN_CLIENT_SECRET,
    API_CLIENT_ID,
    ISSUER,
    KEYCLOAK_USER_ID,
    ORGANIZATION_ID,
    RECORDED,
    RECORDED_TOKEN,
    ROLE_CHECK,
    TOKEN_PATH,
    RecordedAdminApi,
    admin_path,
)

SETTINGS = {
    ISSUER_VARIABLE: ISSUER,
    ADMIN_CLIENT_ID_VARIABLE: ADMIN_CLIENT_ID,
    ADMIN_CREDENTIAL_VARIABLE: ADMIN_CLIENT_SECRET,
    API_CLIENT_VARIABLE: API_CLIENT_ID,
}
# The Keycloak ids of the recorded organization group and role client role.
GROUP_ID = RECORDED["organization_groups"]["body"][0]["id"]
ROLE_KEYCLOAK_ID = next(
    role["id"] for role in RECORDED["client_roles_created"]["body"] if role["name"] == "recorded-role"
)
# The answers of the startup check before it reads the service account's roles.
BEFORE_ROLES = ROLE_CHECK[: ROLE_CHECK.index("service_account_roles")]
# The admin client's credential as the grant sends it.
BASIC = "Basic " + base64.b64encode(f"{ADMIN_CLIENT_ID}:{ADMIN_CLIENT_SECRET}".encode()).decode()


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
            issuer=ISSUER, client_id=ADMIN_CLIENT_ID, client_secret=ADMIN_CLIENT_SECRET, api_client_id=API_CLIENT_ID
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
        settings = KeycloakAdminSettings(
            issuer=ISSUER, client_id=ADMIN_CLIENT_ID, client_secret=ADMIN_CLIENT_SECRET, api_client_id=API_CLIENT_ID
        )

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
            ("GET", admin_path("/clients")),
        ]
        assert api.sent[1].query == {"first": "0", "max": "1"}
        assert api.sent[2].query == {"username": f"service-account-{ADMIN_CLIENT_ID}", "exact": "true"}
        assert api.sent[5].query == {"clientId": API_CLIENT_ID}

    def test_the_recorded_service_account_holds_exactly_the_required_roles(self):
        held = {role["name"] for role in RECORDED["service_account_roles"]["body"]}

        assert held == keycloak_admin.REQUIRED_ROLES

    async def test_an_api_client_the_service_account_cannot_see_is_refused(self):
        """Without query-clients or the fine-grained ``view``, Keycloak answers the lookup as if it were not there."""
        api = RecordedAdminApi(["token", *ROLE_CHECK[:-1], "api_client_find_none"])

        with pytest.raises(KeycloakApiClientNotManaged) as refused:
            await api.api().check_credentials()

        assert (refused.value.status, refused.value.found, refused.value.missing) == (
            403,
            False,
            frozenset({"view", "manage"}),
        )

    async def test_an_api_client_whose_roles_it_may_not_manage_is_refused(self):
        client = {
            **RECORDED["api_client_find"]["body"][0],
            "access": {"view": True, "configure": False, "manage": False},
        }
        api = RecordedAdminApi(["token", *ROLE_CHECK[:-1], httpx.Response(200, json=[client])])

        with pytest.raises(KeycloakApiClientNotManaged) as refused:
            await api.api().check_credentials()

        assert (refused.value.found, refused.value.missing) == (True, frozenset({"manage"}))

    async def test_a_service_account_with_some_of_the_roles_lacks_the_others(self):
        held = [role for role in RECORDED["service_account_roles"]["body"] if role["name"] == "manage-users"]
        api = RecordedAdminApi(["token", *BEFORE_ROLES, httpx.Response(200, json=held)])

        with pytest.raises(KeycloakAdminRolesMissing) as refused:
            await api.api().check_credentials()

        assert refused.value.missing == frozenset(
            {"create-client", "manage-organizations", "query-clients", "view-events"}
        )

    async def test_a_service_account_that_cannot_be_found_lacks_the_roles(self):
        api = RecordedAdminApi(["token", "credential_check", httpx.Response(200, json=[])])

        with pytest.raises(KeycloakAdminRolesMissing) as refused:
            await api.api().check_credentials()

        assert refused.value.missing == keycloak_admin.REQUIRED_ROLES

    async def test_roles_that_are_not_a_list_are_an_error(self):
        api = RecordedAdminApi(["token", *BEFORE_ROLES, httpx.Response(200, json={"name": "manage-users"})])

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
    """The read the adapter makes by Keycloak id: a linked user."""

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


@pytest.mark.anyio
class TestRemoveMember:
    """A 404 ends a membership only once the user is confirmed not to be a member (#619)."""

    async def test_a_removed_member_is_removed(self):
        api = RecordedAdminApi(["token", "member_remove"])

        await api.api().remove_member(ORGANIZATION_ID, KEYCLOAK_USER_ID)

        assert api.script == []

    async def test_the_generic_404_counts_once_the_users_organizations_lack_this_one(self):
        api = RecordedAdminApi(["token", "member_remove_again", "member_organizations_none"])

        await api.api().remove_member(ORGANIZATION_ID, KEYCLOAK_USER_ID)

        assert api.calls()[-1] == ("GET", admin_path(f"/organizations/members/{KEYCLOAK_USER_ID}/organizations"))

    async def test_the_generic_404_for_a_member_still_there_is_an_error(self):
        """A route that no longer matches answers the same 404: the revocation must not pass as done."""
        api = RecordedAdminApi(["token", "member_remove_again", "member_organizations"])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().remove_member(ORGANIZATION_ID, KEYCLOAK_USER_ID)

        assert refused.value.status == 404

    async def test_the_generic_404_for_a_user_the_realm_lacks_counts(self):
        api = RecordedAdminApi(["token", "member_remove_again", "member_organizations_unknown_user", "user_get_none"])

        await api.api().remove_member(ORGANIZATION_ID, KEYCLOAK_USER_ID)

        assert api.script == []

    async def test_the_generic_404_twice_for_a_user_the_realm_has_is_an_error(self):
        api = RecordedAdminApi(["token", "member_remove_again", "member_organizations_unknown_user", "user_get"])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().remove_member(ORGANIZATION_ID, RECORDED["user_get"]["body"]["id"])

        assert refused.value.status == 404

    async def test_an_organization_gone_is_removed(self):
        api = RecordedAdminApi(["token", httpx.Response(404, json={"errorMessage": "Organization not found."})])

        await api.api().remove_member(ORGANIZATION_ID, KEYCLOAK_USER_ID)

        assert api.script == []


@pytest.mark.anyio
class TestIdempotentWrites:
    """The answers Keycloak gives a write already made count as done; any other refusal raises."""

    @pytest.mark.parametrize(
        ("answer", "call"),
        [
            ("group_member_add_again", lambda api: api.add_group_member(ORGANIZATION_ID, GROUP_ID, KEYCLOAK_USER_ID)),
            (
                "group_member_remove_again",
                lambda api: api.remove_group_member(ORGANIZATION_ID, GROUP_ID, KEYCLOAK_USER_ID),
            ),
            ("organization_group_delete_again", lambda api: api.delete_organization_group(ORGANIZATION_ID, GROUP_ID)),
            ("role_delete_again", lambda api: api.delete_role(ROLE_KEYCLOAK_ID)),
            ("member_add_again", lambda api: api.add_member(ORGANIZATION_ID, KEYCLOAK_USER_ID)),
        ],
        ids=["group member added", "group member removed", "group deleted", "role deleted", "member added"],
    )
    async def test_a_repeat_is_done(self, answer, call):
        api = RecordedAdminApi(["token", answer])

        await call(api.api())

        assert api.script == []

    @pytest.mark.parametrize(
        ("answer", "call"),
        [
            ("organization_group_create_taken", lambda api: api.create_organization_group(ORGANIZATION_ID, {})),
            ("client_role_create_taken", lambda api: api.create_client_role({})),
        ],
        ids=["group name taken", "client role name taken"],
    )
    async def test_a_name_taken_is_an_error(self, answer, call):
        steps = ["token", answer] if answer != "client_role_create_taken" else ["token", "api_client_find", answer]
        api = RecordedAdminApi(steps)

        with pytest.raises(KeycloakAdminError) as refused:
            await call(api.api())

        assert refused.value.status == 409

    async def test_a_group_members_call_for_a_group_gone_is_an_error_when_adding(self):
        api = RecordedAdminApi(["token", httpx.Response(404, json={"errorMessage": "Group does not exist"})])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().add_group_member(ORGANIZATION_ID, GROUP_ID, KEYCLOAK_USER_ID)

        assert refused.value.status == 404

    async def test_the_api_client_is_looked_up_once(self):
        api = RecordedAdminApi(["token", "api_client_find", "client_roles", "client_roles_created"])
        connected = api.api()

        first = await connected.client_roles()
        second = await connected.client_roles()

        client = RECORDED["api_client_find"]["body"][0]["id"]
        assert api.calls()[1:] == [
            ("GET", admin_path("/clients")),
            ("GET", admin_path(f"/clients/{client}/roles")),
            ("GET", admin_path(f"/clients/{client}/roles")),
        ]
        assert (first, second) == (RECORDED["client_roles"]["body"], RECORDED["client_roles_created"]["body"])

    async def test_lists_are_read_a_page_at_a_time(self, monkeypatch):
        monkeypatch.setattr(keycloak_admin, "PAGE_SIZE", 1)
        organizations = RECORDED["organizations_list"]["body"]
        pages = [httpx.Response(200, json=[organization]) for organization in organizations]
        api = RecordedAdminApi(["token", *pages, httpx.Response(200, json=[])])

        found = await api.api().list_organizations()

        assert found == organizations
        assert [sent.query["first"] for sent in api.sent[1:]] == [str(index) for index in range(len(organizations) + 1)]

    async def test_a_list_whose_paging_is_ignored_is_an_error_not_an_endless_loop(self, monkeypatch):
        monkeypatch.setattr(keycloak_admin, "PAGE_SIZE", 1)
        same_page = RECORDED["organizations_list"]["body"][:1]
        api = RecordedAdminApi(["token", httpx.Response(200, json=same_page), httpx.Response(200, json=same_page)])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().list_organizations()

        assert refused.value.status == 200


REGISTERED = {
    "id": "kc-client",
    "clientId": "identity-stack-machine-1",
    "secret": "s",
    "registrationAccessToken": "rat",
}
REGISTRATION_PATH = urlsplit(ISSUER).path


class TestClientRegistration:
    """The calls behind machine credentials: registered under the issuer, deleted with the registration token."""

    async def test_registers_as_the_service_account_under_the_issuer(self):
        api = RecordedAdminApi(["token", httpx.Response(201, json=REGISTERED)])

        created = await api.api().register_client({"clientId": "identity-stack-machine-1"})

        assert created == REGISTERED
        sent = api.sent[-1]
        assert (sent.method, sent.path, sent.body, sent.authorization) == (
            "POST",
            f"{REGISTRATION_PATH}/clients-registrations/default",
            {"clientId": "identity-stack-machine-1"},
            "Bearer recorded-access-token",
        )

    @pytest.mark.parametrize("missing", sorted(REGISTERED))
    async def test_an_answer_without_the_created_client_is_an_error(self, missing):
        answer = {key: value for key, value in REGISTERED.items() if key != missing}
        api = RecordedAdminApi(["token", httpx.Response(201, json=answer)])

        with pytest.raises(KeycloakAdminError):
            await api.api().register_client({})

    async def test_a_refused_registration_is_an_error_with_its_status(self):
        api = RecordedAdminApi(["token", httpx.Response(403, json={"error": "insufficient_scope"})])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().register_client({})

        assert refused.value.status == 403

    @pytest.mark.parametrize(("status", "deleted"), [(204, True), (401, False)])
    async def test_unregisters_with_the_registration_token(self, status, deleted):
        api = RecordedAdminApi([httpx.Response(status)])

        assert await api.api().unregister_client("identity-stack-machine-1", "rat") is deleted
        sent = api.sent[-1]
        assert (sent.method, sent.path, sent.authorization) == (
            "DELETE",
            f"{REGISTRATION_PATH}/clients-registrations/default/identity-stack-machine-1",
            "Bearer rat",
        )

    async def test_any_other_answer_to_the_delete_is_an_error(self):
        api = RecordedAdminApi([httpx.Response(500)])

        with pytest.raises(KeycloakAdminError) as refused:
            await api.api().unregister_client("identity-stack-machine-1", "rat")

        assert refused.value.status == 500


@pytest.mark.anyio
class TestTheCredential:
    """The secret goes only to the grant, by Basic auth; the token is reused; neither reaches the logs."""

    async def test_the_grant_carries_the_client_credentials_by_basic_auth(self):
        api = RecordedAdminApi(["token_wrong_secret"])

        with pytest.raises(KeycloakAdminError):
            await api.api().check_credentials()

        assert (api.sent[0].authorization, api.sent[0].body) == (BASIC, None)

    async def test_the_token_is_kept_between_calls(self):
        api = RecordedAdminApi(["token", "organization_find_none", "organization_find_none"])
        connected = api.api()

        await connected.find_organization(ORGANIZATION_ID)
        await connected.find_organization(ORGANIZATION_ID)

        assert [call for call in api.calls() if call == ("POST", TOKEN_PATH)] == [("POST", TOKEN_PATH)]
        assert {sent.authorization for sent in api.sent[1:]} == {f"Bearer {RECORDED_TOKEN}"}

    async def test_neither_the_secret_nor_the_token_is_logged(self, caplog):
        caplog.set_level(logging.DEBUG)
        api = RecordedAdminApi(
            [
                # A token the Admin API refuses twice.
                "token",
                "admin_api_bad_token",
                "token",
                "admin_api_bad_token",
                # A refused grant, no answer from the grant, and a grant answer that is not JSON.
                "token_wrong_secret",
                httpx.ConnectError("connection refused"),
                httpx.Response(200, text="not json"),
                # No answer from the Admin API, then a refusal of a call.
                "token",
                httpx.ConnectError("connection refused"),
                "token",
                httpx.Response(500, json={"error": "unknown_error"}),
            ]
        )

        for _ in range(6):
            connected = api.api()
            with pytest.raises(KeycloakAdminError):
                await connected.check_credentials()

        organizations = "/organizations"
        assert [record.getMessage() for record in caplog.records if record.name == keycloak_admin.logger.name] == [
            f"keycloak admin: GET {organizations} answered 401; asking once more with a new token",
            f"keycloak admin: GET {organizations} answered 401 again",
            "keycloak admin: the service-account grant answered 401",
            "keycloak admin: no answer from the grant endpoint (ConnectError)",
            "keycloak admin: the grant endpoint answered something that is not JSON",
            f"keycloak admin: GET {organizations} did not answer (ConnectError)",
            f'keycloak admin: GET {organizations} answered 500: {{"error":"unknown_error"}}',
        ]
        logged = caplog.text
        for secret in (ADMIN_CLIENT_SECRET, RECORDED_TOKEN, BASIC):
            assert secret not in logged
            assert secret not in repr(connected)
