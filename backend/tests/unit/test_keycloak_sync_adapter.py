"""KeycloakSyncAdapter against the Admin API answers recorded from keycloak-test.

Every operation is driven through ``KeycloakAdminApi`` and an
``httpx.MockTransport`` that replays the recorded answers in order
(``tests/helpers/keycloak_admin_recorded.py``). The requests the adapter sends
are compared whole.
"""

import base64
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from expression import Ok

from app.services.adapters.base import LAST_ROLE_IN_TENANT as LAST_ROLE
from app.services.adapters.base import STATUS_CHANGED, UNANSWERED, SyncError
from app.services.adapters.keycloak import USER_NOT_IN_KEYCLOAK, KeycloakSyncAdapter, LocalDatabaseError
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
# The organization as the recorded search found it (responses.json "organization_find").
FOUND_ORGANIZATION = RECORDED["organization_find"]["body"][0]
# The email the recorded user holds (responses.json "user_find").
RECORDED_EMAIL = RECORDED["user_find"]["body"][0]["email"]


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
            Sent("POST", ORGANIZATIONS, {}, {"name": "Recorded Co", "alias": TENANT_ID, "domains": []}, BEARER),
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
            {**FOUND_ORGANIZATION, "name": "Recorded Co Renamed", "domains": [{"name": "recorded.test"}]},
            BEARER,
        )

    async def test_an_update_without_domains_leaves_the_organizations_domains_and_attributes(self):
        """Keycloak keeps the attributes a PUT leaves out, so the realm admin's ``type:`` markers survive."""
        api = RecordedAdminApi(["token", "organization_find", "organization_update"])

        await adapter(api).sync_tenant(tenant_id=TENANT, data={"name": "Recorded Co Renamed"})

        assert api.sent[2].body == {**FOUND_ORGANIZATION, "name": "Recorded Co Renamed"}

    async def test_an_update_keeps_the_fields_the_adapter_does_not_own(self):
        """Keycloak resets ``enabled``, ``description`` and ``redirectUrl`` a PUT leaves out (probed on 26.8.2).

        The organization as found is sent back with only name, alias and domains
        replaced, so one a realm admin disabled stays disabled. Its attributes
        are never sent: Keycloak keeps them.
        """
        found = {
            "id": ORGANIZATION_ID,
            "name": "Recorded Co",
            "alias": TENANT_ID,
            "enabled": False,
            "description": "Kept by the realm admin",
            "redirectUrl": "http://localhost:3000/welcome",
            "domains": [{"name": "kept.test", "verified": True, "autoRedirect": False}],
            "attributes": {"type:tenant-admin": ["someone"]},
        }
        api = RecordedAdminApi(["token", httpx.Response(200, json=[found]), "organization_update"])

        result = await adapter(api).sync_tenant(tenant_id=TENANT, data={"name": "Recorded Co Renamed"})

        assert result == Ok(None)
        assert api.sent[2].body == {
            "id": ORGANIZATION_ID,
            "name": "Recorded Co Renamed",
            "alias": TENANT_ID,
            "enabled": False,
            "description": "Kept by the realm admin",
            "redirectUrl": "http://localhost:3000/welcome",
            "domains": [{"name": "kept.test", "verified": True, "autoRedirect": False}],
        }

    async def test_an_empty_domain_list_clears_the_organizations_domains(self):
        """``[]`` is "no self-provisioning domains", not "leave them": Keycloak drops the old ones."""
        api = RecordedAdminApi(["token", "organization_find", "organization_update"])

        result = await adapter(api).sync_tenant(
            tenant_id=TENANT, data={"name": "Recorded Co Renamed", "self_provisioning_domains": []}
        )

        assert result == Ok(None)
        assert api.sent[2].body == {**FOUND_ORGANIZATION, "name": "Recorded Co Renamed", "domains": []}

    @pytest.mark.parametrize("domains", ["acme.test", None, ["acme.test", 7], [""], {"acme.test": True}])
    async def test_domains_that_are_not_a_list_of_names_are_an_error_without_a_call(self, domains):
        api = RecordedAdminApi([])

        result = await adapter(api).sync_tenant(
            tenant_id=TENANT, data={"name": "Recorded Co", "self_provisioning_domains": domains}
        )

        refused(result, "sync_tenant", None)
        assert api.sent == []

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
    async def test_a_status_change_sets_enabled_of_the_user_it_finds(self, status, enabled):
        api = RecordedAdminApi(["token", "user_find", "user_update"])

        result = await adapter(api).sync_user(
            user_id=USER, data={"email": RECORDED_EMAIL, "status": status, STATUS_CHANGED: True}
        )

        assert result == Ok(None)
        assert api.sent[2] == Sent("PUT", USER_PATH, {}, {"email": RECORDED_EMAIL, "enabled": enabled}, BEARER)

    @pytest.mark.parametrize("status", ["active", "provisioned", "inactive"])
    async def test_a_profile_update_with_the_same_email_sends_nothing(self, status):
        """Without a status change ``enabled`` is not sent, so a realm admin's disable stands."""
        api = RecordedAdminApi(["token", "user_find"])

        result = await adapter(api).sync_user(user_id=USER, data={"email": RECORDED_EMAIL, "status": status})

        assert result == Ok(None)
        assert api.calls()[1:] == [("GET", USERS)]

    async def test_a_changed_email_is_no_longer_verified(self):
        """The realm's verified email links identities across providers (#559): a new address starts unverified."""
        api = RecordedAdminApi(["token", "user_find", "user_update"])

        result = await adapter(api).sync_user(user_id=USER, data={"email": "renamed@recorded.test", "status": "active"})

        assert result == Ok(None)
        assert api.sent[2] == Sent(
            "PUT", USER_PATH, {}, {"email": "renamed@recorded.test", "emailVerified": False}, BEARER
        )

    async def test_the_same_email_in_other_letter_case_keeps_its_verification(self):
        """Keycloak stores emails in lower case, so this is the address it holds."""
        api = RecordedAdminApi(["token", "user_find", "user_update"])

        await adapter(api).sync_user(
            user_id=USER, data={"email": RECORDED_EMAIL.upper(), "status": "active", STATUS_CHANGED: True}
        )

        assert api.sent[2].body == {"email": RECORDED_EMAIL.upper(), "enabled": True}

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
        api = RecordedAdminApi(
            ["token", "user_find", "organization_find", "organization_get", "member_remove", "user_update"]
        )

        result = await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID})

        assert result == Ok(None)
        assert api.calls()[1:] == [
            ("GET", USERS),
            ("GET", ORGANIZATIONS),
            ("GET", ORGANIZATION),
            ("DELETE", MEMBER),
            ("PUT", USER_PATH),
        ]
        assert api.sent[2] == find_organization()

    async def test_leaving_a_tenant_does_not_push_enabled(self):
        """Offboarding must not undo a realm admin's disable: only a changed email is pushed."""
        api = RecordedAdminApi(
            ["token", "user_find", "organization_find", "organization_get", "member_remove", "user_update"]
        )

        result = await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID})

        assert result == Ok(None)
        assert [sent.body for sent in api.sent if sent.method == "PUT"] == [
            {"email": LEFT["email"], "emailVerified": False}
        ]

    async def test_leaving_a_tenant_with_the_same_email_sends_no_profile_update(self):
        api = RecordedAdminApi(["token", "user_find", "organization_find", "organization_get", "member_remove"])

        result = await adapter(api).sync_user(
            user_id=USER, data={"email": RECORDED_EMAIL, "status": "active", "left_tenant_id": TENANT_ID}
        )

        assert result == Ok(None)
        assert api.calls()[1:] == [("GET", USERS), ("GET", ORGANIZATIONS), ("GET", ORGANIZATION), ("DELETE", MEMBER)]

    async def test_a_refused_profile_update_still_ends_the_membership(self):
        """The membership is what authorizes the user for the tenant: it ends before the profile is pushed."""
        email_taken = httpx.Response(409, json={"errorMessage": "User exists with same email"})
        api = RecordedAdminApi(
            ["token", "user_find", "organization_find", "organization_get", "member_remove", email_taken]
        )

        result = await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID})

        refused(result, "sync_user", 409)
        assert api.calls()[1:] == [
            ("GET", USERS),
            ("GET", ORGANIZATIONS),
            ("GET", ORGANIZATION),
            ("DELETE", MEMBER),
            ("PUT", USER_PATH),
        ]

    async def test_leaving_a_tenant_the_user_is_not_a_member_of_in_keycloak_is_done(self):
        api = RecordedAdminApi(
            ["token", "user_find", "organization_find", "organization_get", "member_remove_again", "user_update"]
        )

        assert await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID}) == Ok(None)

    async def test_leaving_a_tenant_not_pushed_is_done(self):
        api = RecordedAdminApi(["token", "user_find", "organization_find_none", "user_update"])

        assert await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID}) == Ok(None)
        assert api.script == []

    async def test_leaving_a_tenant_does_not_create_a_user_keycloak_lacks_and_says_so(self):
        """Offboarding never provisions an account. A user the adapter did not create may hold the
        membership under another username, so the membership not ended is an error, not done."""
        api = RecordedAdminApi(["token", "user_find_none"])

        error = refused(
            await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID}),
            "sync_user",
            None,
        )

        assert (error.message, api.script) == (USER_NOT_IN_KEYCLOAK, [])

    @pytest.mark.parametrize("status", ["active", "provisioned", "inactive"])
    async def test_a_status_change_does_not_create_a_user_keycloak_lacks_and_says_so(self, status):
        """A deactivation must not leave a disabled shadow account beside the user's real one
        (login-page sign-up, JIT-linked) and report success: nothing changed, so it is an error."""
        api = RecordedAdminApi(["token", "user_find_none"])

        error = refused(
            await adapter(api).sync_user(
                user_id=USER, data={"email": RECORDED_EMAIL, "status": status, STATUS_CHANGED: True}
            ),
            "sync_user",
            None,
        )

        assert (error.message, api.script) == (USER_NOT_IN_KEYCLOAK, [])

    async def test_a_realm_wide_404_on_leaving_is_an_error(self):
        api = RecordedAdminApi(
            ["token", "user_find", "organization_find", "organization_get", organizations_disabled()]
        )

        result = await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": TENANT_ID})

        refused(result, "sync_user", 404)

    async def test_a_left_tenant_id_that_is_not_a_uuid_is_an_error_without_a_call(self):
        api = RecordedAdminApi([])

        result = await adapter(api).sync_user(user_id=USER, data={**LEFT, "left_tenant_id": "acme"})

        refused(result, "sync_user", None)
        assert api.sent == []

    @pytest.mark.parametrize("data", [None, {"role_name": "member"}, {"role_name": "member", LAST_ROLE: False}])
    async def test_removing_a_role_the_user_holds_others_beside_calls_nothing(self, data):
        """Another role in the tenant still holds the membership."""
        api = RecordedAdminApi([])

        result = await adapter(api).delete_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE, data=data)

        assert result == Ok(None)
        assert api.sent == []

    async def test_removing_the_last_role_in_the_tenant_ends_the_membership(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find", "organization_get", "member_remove"])

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={"role_name": "member", LAST_ROLE: True}
        )

        assert result == Ok(None)
        assert api.sent[1:] == [
            find_organization(),
            find_user(),
            Sent("GET", ORGANIZATION, {}, None, BEARER),
            Sent("DELETE", MEMBER, {}, None, BEARER),
        ]

    @pytest.mark.parametrize(
        "script",
        [
            ["token", "organization_find_none"],
            ["token", "organization_find", "user_find", "organization_get", "member_remove_again"],
        ],
        ids=["tenant-not-pushed", "not-a-member"],
    )
    async def test_removing_the_last_role_where_there_is_no_membership_is_done(self, script):
        api = RecordedAdminApi(list(script))

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
        )

        assert result == Ok(None)
        assert api.script == []

    async def test_removing_the_last_role_of_a_user_keycloak_lacks_is_an_error(self):
        """A user the adapter did not create may hold the membership under another username."""
        api = RecordedAdminApi(["token", "organization_find", "user_find_none"])

        error = refused(
            await adapter(api).delete_role_assignment(
                user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
            ),
            "delete_role_assignment",
            None,
        )

        assert (error.message, api.script) == (USER_NOT_IN_KEYCLOAK, [])

    async def test_a_realm_wide_404_on_removing_the_last_role_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find", organizations_disabled()])

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
        )

        error = refused(result, "delete_role_assignment", 404)
        assert error.context == {"tenant_id": TENANT_ID, "user_id": USER_ID, "status": 404}


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


# The organization as GET /organizations/{id} answers it with a realm admin's grant markers
# listing the recorded member (responses.json "organization_get_marked").
MARKED_ORGANIZATION = RECORDED["organization_get_marked"]["body"]
# The recorded user, as GET /users/{id} answers it.
ACCOUNT = RECORDED["user_get"]["body"]
# A Keycloak account the adapter did not create: the canonical user signed in through Keycloak and was JIT-linked.
LINKED_ACCOUNT = {**ACCOUNT, "username": "alice", "email": "alice@acme.test"}


def linked_to(subject: str | None):
    asked = []

    async def linked_subject(user_id: uuid.UUID) -> str | None:
        asked.append(user_id)
        return subject

    linked_subject.asked = asked
    return linked_subject


def get_account() -> Sent:
    return Sent("GET", USER_PATH, {}, None, BEARER)


class TestEndingAMembershipTrimsTheGrantMarkers:
    """A realm admin's ``type:`` marker for the member must not outlive the membership:
    a later membership would bring the grant back."""

    TRIMMED = {
        **{key: value for key, value in MARKED_ORGANIZATION.items() if key != "attributes"},
        "attributes": {"note": ["kept"], "type:tenant-admin": ["another-member"]},
    }

    async def test_the_last_role_takes_the_member_out_of_every_marker_before_ending_the_membership(self):
        api = RecordedAdminApi(
            ["token", "organization_find", "user_find", "organization_get_marked", "organization_trim", "member_remove"]
        )

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
        )

        assert result == Ok(None)
        assert api.sent[1:] == [
            find_organization(),
            find_user(),
            Sent("GET", ORGANIZATION, {}, None, BEARER),
            Sent("PUT", ORGANIZATION, {}, self.TRIMMED, BEARER),
            Sent("DELETE", MEMBER, {}, None, BEARER),
        ]

    async def test_leaving_a_tenant_trims_the_markers_too(self):
        api = RecordedAdminApi(
            ["token", "user_find", "organization_find", "organization_get_marked", "organization_trim", "member_remove"]
        )

        result = await adapter(api).sync_user(
            user_id=USER, data={"email": RECORDED_EMAIL, "status": "active", "left_tenant_id": TENANT_ID}
        )

        assert result == Ok(None)
        assert [sent.body for sent in api.sent if sent.method == "PUT"] == [self.TRIMMED]
        assert api.script == []

    async def test_markers_not_listing_the_member_are_not_written(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find", "organization_get", "member_remove"])

        await adapter(api).delete_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True})

        assert [sent for sent in api.sent if sent.method == "PUT"] == []

    async def test_a_refused_trim_leaves_the_membership_and_is_an_error(self):
        """Markers first: a member left behind keeps no grant the removal was meant to end with it."""
        forbidden = httpx.Response(403, json={"error": "HTTP 403 Forbidden"})
        api = RecordedAdminApi(["token", "organization_find", "user_find", "organization_get_marked", forbidden])

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
        )

        refused(result, "delete_role_assignment", 403)
        assert [sent for sent in api.sent if sent.method == "DELETE"] == []


class TestLinkedAccounts:
    """Offboarding reaches the account the canonical user's ``keycloak`` IdP link names."""

    async def test_the_last_role_ends_the_membership_of_the_linked_account(self):
        link = linked_to(ACCOUNT["id"])
        api = RecordedAdminApi(["token", "organization_find", "user_get", "organization_get", "member_remove"])

        result = await KeycloakSyncAdapter(api.api(), linked_subject=link).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
        )

        assert result == Ok(None)
        assert link.asked == [USER]
        assert api.sent[1:] == [
            find_organization(),
            get_account(),
            Sent("GET", ORGANIZATION, {}, None, BEARER),
            Sent("DELETE", MEMBER, {}, None, BEARER),
        ]

    async def test_a_link_to_an_account_keycloak_no_longer_has_falls_back_to_the_canonical_id(self):
        api = RecordedAdminApi(
            ["token", "organization_find", "user_get_none", "user_find", "organization_get", "member_remove"]
        )

        result = await KeycloakSyncAdapter(api.api(), linked_subject=linked_to(ACCOUNT["id"])).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
        )

        assert result == Ok(None)
        assert api.script == []

    async def test_no_linked_account_and_none_under_the_canonical_id_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find", "user_get_none", "user_find_none"])

        error = refused(
            await KeycloakSyncAdapter(api.api(), linked_subject=linked_to(ACCOUNT["id"])).delete_role_assignment(
                user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
            ),
            "delete_role_assignment",
            None,
        )

        assert error.message == USER_NOT_IN_KEYCLOAK

    async def test_a_deactivation_disables_the_linked_account_and_pushes_it_no_email(self):
        api = RecordedAdminApi(["token", httpx.Response(200, json=LINKED_ACCOUNT), "user_update"])

        result = await KeycloakSyncAdapter(api.api(), linked_subject=linked_to(ACCOUNT["id"])).sync_user(
            user_id=USER, data={"email": "canonical@acme.test", "status": "inactive", STATUS_CHANGED: True}
        )

        assert result == Ok(None)
        assert api.sent[1:] == [get_account(), Sent("PUT", USER_PATH, {}, {"enabled": False}, BEARER)]

    async def test_leaving_a_tenant_ends_the_linked_accounts_membership_and_pushes_it_no_email(self):
        api = RecordedAdminApi(
            [
                "token",
                httpx.Response(200, json=LINKED_ACCOUNT),
                "organization_find",
                "organization_get",
                "member_remove",
            ]
        )

        result = await KeycloakSyncAdapter(api.api(), linked_subject=linked_to(ACCOUNT["id"])).sync_user(
            user_id=USER, data={"email": "canonical@acme.test", "status": "active", "left_tenant_id": TENANT_ID}
        )

        assert result == Ok(None)
        assert api.calls()[1:] == [
            ("GET", USER_PATH),
            ("GET", ORGANIZATIONS),
            ("GET", ORGANIZATION),
            ("DELETE", MEMBER),
        ]

    async def test_a_role_assignment_adds_the_linked_account(self):
        api = RecordedAdminApi(["token", "organization_find", "user_get", "member_add"])

        result = await KeycloakSyncAdapter(api.api(), linked_subject=linked_to(ACCOUNT["id"])).sync_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE
        )

        assert result == Ok(None)
        assert api.sent[1:] == [find_organization(), get_account(), Sent("POST", MEMBERS, {}, ACCOUNT["id"], BEARER)]

    async def test_a_profile_update_of_a_linked_user_sends_nothing_and_creates_no_account(self):
        """The linked account is the user's: no second account under the canonical id, and no email pushed to it."""
        link = linked_to(ACCOUNT["id"])
        api = RecordedAdminApi(["token", httpx.Response(200, json=LINKED_ACCOUNT)])

        result = await KeycloakSyncAdapter(api.api(), linked_subject=link).sync_user(
            user_id=USER, data={"email": "canonical@acme.test", "status": "active"}
        )

        assert (result, link.asked) == (Ok(None), [USER])
        assert api.sent[1:] == [get_account()]

    async def test_a_new_user_with_no_link_is_created_under_the_canonical_id(self):
        link = linked_to(None)
        api = RecordedAdminApi(["token", "user_find_none", "user_create"])

        result = await KeycloakSyncAdapter(api.api(), linked_subject=link).sync_user(
            user_id=USER, data={"email": RECORDED_EMAIL, "status": "active"}
        )

        assert (result, link.asked) == (Ok(None), [USER])
        assert api.sent[1:] == [
            find_user(),
            Sent("POST", USERS, {}, {"username": USER_ID, "email": RECORDED_EMAIL, "enabled": True}, BEARER),
        ]

    @pytest.mark.parametrize(
        "service_account",
        [
            {**ACCOUNT, "username": "service-account-identity-stack-admin"},
            {**ACCOUNT, "serviceAccountClientId": "identity-stack-admin"},
        ],
        ids=["by-username", "by-client-id"],
    )
    async def test_a_link_naming_a_service_account_is_not_followed(self, service_account):
        """A client's service account is nobody's sign-in: no membership is written for it."""
        api = RecordedAdminApi(
            ["token", "organization_find", httpx.Response(200, json=service_account), "user_find_none"]
        )

        error = refused(
            await KeycloakSyncAdapter(api.api(), linked_subject=linked_to(ACCOUNT["id"])).sync_role_assignment(
                user_id=USER, tenant_id=TENANT, role_id=ROLE
            ),
            "sync_role_assignment",
            None,
        )

        assert error.message == "the user is not in Keycloak"
        assert [sent for sent in api.sent if sent.method == "POST" and sent.path == MEMBERS] == []


def recording_lock(api: RecordedAdminApi, held: list[tuple[str, int, int]]):
    """An organization lock that records, per hold, the alias and the requests sent while it was held."""

    @asynccontextmanager
    async def lock(alias: str) -> AsyncIterator[None]:
        entered = len(api.sent)
        try:
            yield
        finally:
            held.append((alias, entered, len(api.sent)))

    return lock


class TestOrganizationWritesAreSerialized:
    """Writes that send an organization back whole run under the organization's lock, so none undoes another."""

    async def test_ending_a_membership_reads_trims_and_removes_under_the_lock(self):
        api = RecordedAdminApi(
            ["token", "organization_find", "user_find", "organization_get_marked", "organization_trim", "member_remove"]
        )
        held: list[tuple[str, int, int]] = []

        result = await KeycloakSyncAdapter(
            api.api(), organization_lock=recording_lock(api, held)
        ).delete_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True})

        assert result == Ok(None)
        [(alias, entered, left)] = held
        assert alias == TENANT_ID
        assert api.calls()[entered:] == [("GET", ORGANIZATION), ("PUT", ORGANIZATION), ("DELETE", MEMBER)]
        assert left == len(api.sent)

    async def test_a_tenant_update_reads_and_writes_the_organization_under_the_lock(self):
        api = RecordedAdminApi(["token", "organization_find", "organization_update"])
        held: list[tuple[str, int, int]] = []

        result = await KeycloakSyncAdapter(api.api(), organization_lock=recording_lock(api, held)).sync_tenant(
            tenant_id=TENANT, data={"name": "Recorded Co Renamed"}
        )

        assert result == Ok(None)
        assert held == [(TENANT_ID, 0, len(api.sent))]

    async def test_leaving_a_tenant_ends_the_membership_under_the_lock(self):
        api = RecordedAdminApi(
            ["token", "user_find", "organization_find", "organization_get_marked", "organization_trim", "member_remove"]
        )
        held: list[tuple[str, int, int]] = []

        result = await KeycloakSyncAdapter(api.api(), organization_lock=recording_lock(api, held)).sync_user(
            user_id=USER, data={"email": RECORDED_EMAIL, "status": "active", "left_tenant_id": TENANT_ID}
        )

        assert result == Ok(None)
        [(alias, entered, left)] = held
        assert alias == TENANT_ID
        assert api.calls()[entered:left] == [("GET", ORGANIZATION), ("PUT", ORGANIZATION), ("DELETE", MEMBER)]


# What the wiring raises when the service's own database fails (dependencies/identity.py).
DATABASE_FAILURE = "the organization lock failed in the service's database: TimeoutError"


@asynccontextmanager
async def failing_lock(_alias: str) -> AsyncIterator[None]:
    raise LocalDatabaseError(DATABASE_FAILURE)
    yield  # pragma: no cover - never reached; makes this an async generator


async def failing_link(_user_id: uuid.UUID) -> str | None:
    raise LocalDatabaseError(DATABASE_FAILURE)


class TestTheServicesDatabaseFailing:
    """The push runs after the service committed: a failure of its own database is a SyncError, never raised."""

    async def test_a_lock_that_cannot_be_taken_ending_a_membership_is_a_sync_error(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find"])

        error = refused(
            await KeycloakSyncAdapter(api.api(), organization_lock=failing_lock).delete_role_assignment(
                user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
            ),
            "delete_role_assignment",
            None,
        )

        assert error.message == DATABASE_FAILURE
        assert error.context == {"tenant_id": TENANT_ID, "user_id": USER_ID, "status": None}
        assert [sent for sent in api.sent if sent.method in ("PUT", "DELETE")] == []

    async def test_a_lock_that_cannot_be_taken_leaving_a_tenant_is_a_sync_error(self):
        api = RecordedAdminApi(["token", "user_find", "organization_find"])

        error = refused(
            await KeycloakSyncAdapter(api.api(), organization_lock=failing_lock).sync_user(
                user_id=USER, data={"email": RECORDED_EMAIL, "status": "active", "left_tenant_id": TENANT_ID}
            ),
            "sync_user",
            None,
        )

        assert error.message == DATABASE_FAILURE

    async def test_a_lock_that_cannot_be_taken_updating_a_tenant_is_a_sync_error(self):
        api = RecordedAdminApi([])

        error = refused(
            await KeycloakSyncAdapter(api.api(), organization_lock=failing_lock).sync_tenant(
                tenant_id=TENANT, data={"name": "Recorded Co Renamed"}
            ),
            "sync_tenant",
            None,
        )

        assert error.message == DATABASE_FAILURE
        assert api.sent == []

    async def test_an_idp_link_that_cannot_be_read_is_a_sync_error(self):
        api = RecordedAdminApi(["token", "organization_find"])

        error = refused(
            await KeycloakSyncAdapter(api.api(), linked_subject=failing_link).delete_role_assignment(
                user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
            ),
            "delete_role_assignment",
            None,
        )

        assert error.message == DATABASE_FAILURE

    async def test_the_provider_was_asked_nothing_so_the_failure_is_not_unanswered(self):
        """delete_role stops its pushes only at a provider that gave no answer; this one is told and the rest go on."""
        api = RecordedAdminApi(["token", "organization_find", "user_find"])

        error = refused(
            await KeycloakSyncAdapter(api.api(), organization_lock=failing_lock).delete_role_assignment(
                user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE: True}
            ),
            "delete_role_assignment",
            None,
        )

        assert UNANSWERED not in error.context


class TestUpstreamFailures:
    async def test_no_answer_is_marked_unanswered(self):
        api = RecordedAdminApi([httpx.ConnectError("connection refused")])

        error = refused(await adapter(api).delete_tenant(tenant_id=TENANT), "delete_tenant", None)

        assert error.context == {"tenant_id": TENANT_ID, "status": None, UNANSWERED: True}

    async def test_a_refusal_is_not_marked_unanswered(self):
        api = RecordedAdminApi(["token_wrong_secret"])

        error = refused(await adapter(api).delete_tenant(tenant_id=TENANT), "delete_tenant", 401)

        assert UNANSWERED not in error.context

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
