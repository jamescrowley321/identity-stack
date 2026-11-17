"""KeycloakSyncAdapter against the Admin API answers recorded from keycloak-test.

Every operation is driven through ``KeycloakAdminApi`` and an
``httpx.MockTransport`` that replays the recorded answers in order
(``tests/helpers/keycloak_admin_recorded.py``). The requests the adapter sends
are compared whole.
"""

import base64
import logging
import uuid

import httpx
import pytest
from expression import Ok

from app.services.adapters.base import SyncError
from app.services.adapters.keycloak import KeycloakSyncAdapter
from tests.helpers.keycloak_admin_recorded import (
    ADMIN_CLIENT_ID,
    ADMIN_CLIENT_SECRET,
    KEYCLOAK_USER_ID,
    ORGANIZATION_ID,
    RECORDED,
    RECORDED_TOKEN,
    TENANT_ID,
    TOKEN_PATH,
    USER_ID,
    RecordedAdminApi,
    Sent,
    admin_path,
)

pytestmark = pytest.mark.anyio

TENANT = uuid.UUID(TENANT_ID)
USER = uuid.UUID(USER_ID)
ROLE = uuid.UUID("40e00000-0000-4000-8000-000000000586")
BEARER = f"Bearer {RECORDED_TOKEN}"
ORGANIZATIONS = admin_path("/organizations")
ORGANIZATION = admin_path(f"/organizations/{ORGANIZATION_ID}")
MEMBERS = admin_path(f"/organizations/{ORGANIZATION_ID}/members")
MEMBER = admin_path(f"/organizations/{ORGANIZATION_ID}/members/{KEYCLOAK_USER_ID}")
USERS = admin_path("/users")
USER_PATH = admin_path(f"/users/{KEYCLOAK_USER_ID}")
FIND_ORGANIZATION = {"q": f"alias:{TENANT_ID}", "exact": "true"}
FIND_USER = {"username": USER_ID, "exact": "true"}
# A 404 Keycloak answers for a realm-wide fault, not for an absent object (probed on 26.8.2).
LEFT = {"email": "renamed@recorded.test", "status": "active"}


def organizations_disabled() -> httpx.Response:
    return httpx.Response(404, json={"errorMessage": "Organizations not enabled for this realm."})


BASIC = "Basic " + base64.b64encode(f"{ADMIN_CLIENT_ID}:{ADMIN_CLIENT_SECRET}".encode()).decode()


def token_request() -> Sent:
    return Sent("POST", TOKEN_PATH, {}, None, BASIC)


def find_organization() -> Sent:
    return Sent("GET", ORGANIZATIONS, FIND_ORGANIZATION, None, BEARER)


def find_user() -> Sent:
    return Sent("GET", USERS, FIND_USER, None, BEARER)


def adapter(api: RecordedAdminApi) -> KeycloakSyncAdapter:
    return KeycloakSyncAdapter(api.api())


def refused(result, operation: str, status: int | None) -> SyncError:
    """The result's error, after checking its operation and the HTTP status it carries."""
    assert result.is_error()
    error = result.error
    assert isinstance(error, SyncError)
    assert (error.operation, (error.context or {}).get("status")) == (operation, status)
    return error


class TestSyncTenant:
    async def test_creates_the_organization_with_the_tenant_id_as_alias(self):
        api = RecordedAdminApi(["token", "organization_find_none", "organization_create"])

        result = await adapter(api).sync_tenant(
            tenant_id=TENANT, data={"name": "Recorded Co", "self_provisioning_domains": []}
        )

        assert result == Ok(None)
        assert api.sent == [
            token_request(),
            find_organization(),
            Sent("POST", ORGANIZATIONS, {}, {"name": "Recorded Co", "alias": TENANT_ID}, BEARER),
        ]

    async def test_updates_the_organization_it_finds(self):
        api = RecordedAdminApi(["token", "organization_find", "organization_update"])

        result = await adapter(api).sync_tenant(
            tenant_id=TENANT, data={"name": "Recorded Co Renamed", "self_provisioning_domains": ["recorded.test"]}
        )

        assert result == Ok(None)
        assert api.sent[2] == Sent(
            "PUT",
            ORGANIZATION,
            {},
            {"name": "Recorded Co Renamed", "alias": TENANT_ID, "domains": [{"name": "recorded.test"}]},
            BEARER,
        )

    async def test_an_update_without_domains_leaves_the_organizations_domains_and_attributes(self):
        """Keycloak keeps what a PUT leaves out, so the realm admin's ``type:`` markers survive."""
        api = RecordedAdminApi(["token", "organization_find", "organization_update"])

        await adapter(api).sync_tenant(tenant_id=TENANT, data={"name": "Recorded Co Renamed"})

        assert api.sent[2].body == {"name": "Recorded Co Renamed", "alias": TENANT_ID}

    async def test_a_domain_another_organization_holds_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find", "organization_update_domain_taken"])

        result = await adapter(api).sync_tenant(
            tenant_id=TENANT, data={"name": "Recorded Co Renamed", "self_provisioning_domains": ["acme.test"]}
        )

        error = refused(result, "sync_tenant", 400)
        assert error.context == {"tenant_id": TENANT_ID, "status": 400}

    async def test_a_name_another_organization_holds_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find_none", "organization_create_name_taken"])

        result = await adapter(api).sync_tenant(tenant_id=TENANT, data={"name": "Recorded Co"})

        refused(result, "sync_tenant", 409)

    async def test_no_name_is_an_error_without_a_call(self):
        api = RecordedAdminApi([])

        result = await adapter(api).sync_tenant(tenant_id=TENANT, data={})

        refused(result, "sync_tenant", None)
        assert api.sent == []


class TestDeleteTenant:
    async def test_deletes_the_organization(self):
        api = RecordedAdminApi(["token", "organization_find", "organization_delete"])

        assert await adapter(api).delete_tenant(tenant_id=TENANT) == Ok(None)
        assert api.calls()[1:] == [("GET", ORGANIZATIONS), ("DELETE", ORGANIZATION)]

    async def test_an_organization_that_is_not_there_is_already_deleted(self):
        api = RecordedAdminApi(["token", "organization_find_none"])

        assert await adapter(api).delete_tenant(tenant_id=TENANT) == Ok(None)
        assert api.sent == [token_request(), find_organization()]

    async def test_an_organization_deleted_in_between_is_deleted(self):
        api = RecordedAdminApi(["token", "organization_find", "organization_delete_again"])

        assert await adapter(api).delete_tenant(tenant_id=TENANT) == Ok(None)

    async def test_a_realm_wide_404_is_an_error_not_a_deletion(self):
        api = RecordedAdminApi(["token", "organization_find", organizations_disabled()])

        refused(await adapter(api).delete_tenant(tenant_id=TENANT), "delete_tenant", 404)


class TestSyncUser:
    async def test_creates_the_user_with_the_user_id_as_username(self):
        api = RecordedAdminApi(["token", "user_find_none", "user_create"])

        result = await adapter(api).sync_user(
            user_id=USER, data={"email": "recorded@recorded.test", "status": "active"}
        )

        assert result == Ok(None)
        assert api.sent == [
            token_request(),
            find_user(),
            Sent("POST", USERS, {}, {"username": USER_ID, "email": "recorded@recorded.test", "enabled": True}, BEARER),
        ]

    @pytest.mark.parametrize(("status", "enabled"), [("active", True), ("provisioned", True), ("inactive", False)])
    async def test_updates_email_and_enabled_of_the_user_it_finds(self, status, enabled):
        api = RecordedAdminApi(["token", "user_find", "user_update"])

        result = await adapter(api).sync_user(user_id=USER, data={"email": "renamed@recorded.test", "status": status})

        assert result == Ok(None)
        assert api.sent[2] == Sent("PUT", USER_PATH, {}, {"email": "renamed@recorded.test", "enabled": enabled}, BEARER)

    async def test_an_email_another_keycloak_user_holds_is_an_error(self):
        """A user who signed up in Keycloak is not taken over by email."""
        api = RecordedAdminApi(["token", "user_find_none", "user_create_email_taken"])

        result = await adapter(api).sync_user(user_id=USER, data={"email": "alice@acme.test", "status": "active"})

        refused(result, "sync_user", 409)

    @pytest.mark.parametrize(
        "data", [{}, {"email": "a@b.test"}, {"status": "active"}, {"email": "a@b.test", "status": "deleted"}]
    )
    async def test_incomplete_or_unknown_data_is_an_error_without_a_call(self, data):
        api = RecordedAdminApi([])

        refused(await adapter(api).sync_user(user_id=USER, data=data), "sync_user", None)
        assert api.sent == []


class TestDeleteUser:
    async def test_deletes_the_user(self):
        api = RecordedAdminApi(["token", "user_find", "user_delete"])

        assert await adapter(api).delete_user(user_id=USER) == Ok(None)
        assert api.calls()[1:] == [("GET", USERS), ("DELETE", USER_PATH)]

    async def test_a_user_that_is_not_there_is_already_deleted(self):
        api = RecordedAdminApi(["token", "user_find_none"])

        assert await adapter(api).delete_user(user_id=USER) == Ok(None)
        assert api.sent == [token_request(), find_user()]

    async def test_a_user_deleted_in_between_is_deleted(self):
        api = RecordedAdminApi(["token", "user_find", "user_delete_again"])

        assert await adapter(api).delete_user(user_id=USER) == Ok(None)

    async def test_a_realm_wide_404_is_an_error_not_a_deletion(self):
        not_found = httpx.Response(404, json={"error": "Realm not found."})
        api = RecordedAdminApi(["token", "user_find", not_found])

        refused(await adapter(api).delete_user(user_id=USER), "delete_user", 404)


class TestMembers:
    async def test_a_role_assignment_adds_the_user_to_the_tenants_organization(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find", "member_add"])

        result = await adapter(api).sync_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={"role_name": "member"}
        )

        assert result == Ok(None)
        assert api.sent[1:] == [find_organization(), find_user(), Sent("POST", MEMBERS, {}, KEYCLOAK_USER_ID, BEARER)]

    async def test_a_user_already_a_member_is_added(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find", "member_add_again"])

        result = await adapter(api).sync_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE)

        assert result == Ok(None)

    async def test_adding_to_a_tenant_not_pushed_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find_none"])

        error = refused(
            await adapter(api).sync_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE),
            "sync_role_assignment",
            None,
        )
        assert error.context == {"tenant_id": TENANT_ID, "user_id": USER_ID, "status": None}

    async def test_adding_a_user_not_pushed_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find_none"])

        refused(
            await adapter(api).sync_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE),
            "sync_role_assignment",
            None,
        )

    async def test_adding_to_an_organization_deleted_in_between_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find", "member_add_unknown_organization"])

        refused(
            await adapter(api).sync_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE),
            "sync_role_assignment",
            404,
        )

    async def test_leaving_a_tenant_removes_the_user_from_its_organization(self):
        api = RecordedAdminApi(["token", "user_find", "user_update", "organization_find", "member_remove"])

        result = await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID})

        assert result == Ok(None)
        assert api.calls()[1:] == [("GET", USERS), ("PUT", USER_PATH), ("GET", ORGANIZATIONS), ("DELETE", MEMBER)]
        assert api.sent[3] == find_organization()

    async def test_leaving_a_tenant_the_user_is_not_a_member_of_in_keycloak_is_done(self):
        api = RecordedAdminApi(["token", "user_find", "user_update", "organization_find", "member_remove_again"])

        assert await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID}) == Ok(None)

    async def test_leaving_a_tenant_not_pushed_is_done(self):
        api = RecordedAdminApi(["token", "user_find", "user_update", "organization_find_none"])

        assert await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID}) == Ok(None)
        assert api.script == []

    async def test_a_user_created_now_belongs_to_no_organization_to_leave(self):
        api = RecordedAdminApi(["token", "user_find_none", "user_create"])

        assert await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID}) == Ok(None)
        assert api.script == []

    async def test_a_realm_wide_404_on_leaving_is_an_error(self):
        api = RecordedAdminApi(["token", "user_find", "user_update", "organization_find", organizations_disabled()])

        result = await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID})

        refused(result, "sync_user", 404)

    async def test_a_left_tenant_id_that_is_not_a_uuid_is_an_error_without_a_call(self):
        api = RecordedAdminApi([])

        result = await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": "acme"})

        refused(result, "sync_user", None)
        assert api.sent == []

    async def test_removing_a_role_assignment_calls_nothing(self):
        """Another role may still hold the membership: leaving a tenant goes through sync_user."""
        api = RecordedAdminApi([])

        result = await adapter(api).delete_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE)

        assert result == Ok(None)
        assert api.sent == []


class TestCanonicalOnly:
    async def test_roles_and_permissions_call_nothing(self):
        api = RecordedAdminApi([])
        sync = adapter(api)

        results = [
            await sync.sync_role(role_id=ROLE, data={"name": "member"}),
            await sync.delete_role(role_id=ROLE),
            await sync.sync_permission(permission_id=ROLE, data={"name": "documents:read"}),
            await sync.delete_permission(permission_id=ROLE),
        ]

        assert results == [Ok(None)] * len(results)
        assert api.sent == []


class TestUpstreamFailures:
    async def test_a_refused_service_account_grant_is_an_error(self):
        api = RecordedAdminApi(["token_wrong_secret"])

        refused(await adapter(api).delete_tenant(tenant_id=TENANT), "delete_tenant", 401)

    async def test_no_answer_is_an_error_and_nothing_raises(self):
        api = RecordedAdminApi([httpx.ConnectError("connection refused")])

        refused(
            await adapter(api).sync_user(user_id=USER, data={"email": "a@b.test", "status": "active"}),
            "sync_user",
            None,
        )

    async def test_an_answer_that_is_not_json_is_an_error(self):
        api = RecordedAdminApi(["token", httpx.Response(200, content=b"<html>")])

        refused(await adapter(api).delete_tenant(tenant_id=TENANT), "delete_tenant", None)

    async def test_a_forbidden_call_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find_none", "organization_create_forbidden"])

        refused(await adapter(api).sync_tenant(tenant_id=TENANT, data={"name": "Recorded Co"}), "sync_tenant", 403)

    async def test_an_admin_api_401_is_asked_once_more_with_a_new_token(self):
        api = RecordedAdminApi(["token", "admin_api_bad_token", "token", "organization_find_none"])

        assert await adapter(api).delete_tenant(tenant_id=TENANT) == Ok(None)
        assert api.calls() == [
            ("POST", TOKEN_PATH),
            ("GET", ORGANIZATIONS),
            ("POST", TOKEN_PATH),
            ("GET", ORGANIZATIONS),
        ]

    async def test_a_second_401_is_an_error(self):
        api = RecordedAdminApi(["token", "admin_api_bad_token", "token", "admin_api_bad_token"])

        refused(await adapter(api).delete_tenant(tenant_id=TENANT), "delete_tenant", 401)

    async def test_the_token_is_kept_between_calls(self):
        api = RecordedAdminApi(["token", "organization_find_none", "organization_find_none"])
        sync = adapter(api)

        await sync.delete_tenant(tenant_id=TENANT)
        await sync.delete_tenant(tenant_id=TENANT)

        assert [call for call in api.calls() if call == ("POST", TOKEN_PATH)] == [("POST", TOKEN_PATH)]

    async def test_the_grant_carries_the_client_credentials_by_basic_auth(self):
        seen = []

        def handle(request):
            seen.append((request.headers.get("authorization"), request.content))
            return httpx.Response(401, json=RECORDED["token_wrong_secret"]["body"])

        api = RecordedAdminApi([])
        api.handle = handle

        await adapter(api).delete_tenant(tenant_id=TENANT)

        assert seen == [(BASIC, b"grant_type=client_credentials")]

    async def test_neither_the_secret_nor_the_token_is_logged(self, caplog):
        caplog.set_level(logging.DEBUG)
        api = RecordedAdminApi(["token", "admin_api_bad_token", "token", "admin_api_bad_token", "token_wrong_secret"])
        sync = adapter(api)

        await sync.delete_tenant(tenant_id=TENANT)
        sync._api._token.clear()
        await sync.delete_tenant(tenant_id=TENANT)

        logged = caplog.text
        assert ADMIN_CLIENT_SECRET not in logged
        assert RECORDED_TOKEN not in logged
        assert ADMIN_CLIENT_SECRET not in repr(sync._api)
