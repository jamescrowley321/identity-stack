"""KeycloakSyncAdapter against the Admin API answers recorded from keycloak-test.

Every operation is driven through ``KeycloakAdminApi`` and an
``httpx.MockTransport`` that replays the recorded answers in order
(``tests/helpers/keycloak_admin_recorded.py``). The canonical links are an
in-memory ``KeycloakLinks``.
"""

import uuid

import httpx
import pytest
from expression import Ok

from app.services.adapters.base import CREATED_IN_TENANT, LAST_ROLE_IN_TENANT, STATUS_CHANGED, UNANSWERED, SyncError
from app.services.adapters.keycloak import (
    PERMISSION_ATTRIBUTE,
    ROLE_ATTRIBUTE,
    KeycloakSyncAdapter,
    LinkRefused,
    LocalDatabaseError,
    Refusal,
    role_client_name,
)
from tests.helpers.keycloak_admin_recorded import (
    KEYCLOAK_USER_ID,
    ORGANIZATION_ID,
    PERMISSION_ID,
    RECORDED,
    ROLE_ID,
    TENANT_ID,
    USER_ID,
    RecordedAdminApi,
    Sent,
    admin_path,
)
from tests.helpers.keycloak_links import MemoryKeycloakLinks as Links

pytestmark = pytest.mark.anyio

TENANT = uuid.UUID(TENANT_ID)
USER = uuid.UUID(USER_ID)
ROLE = uuid.UUID(ROLE_ID)
PERMISSION = uuid.UUID(PERMISSION_ID)
FOUND_ORGANIZATION = RECORDED["organization_find"]["body"][0]
FOUND_USER = RECORDED["user_find"]["body"][0]
CLIENT_ROLES = {role["name"]: role for role in RECORDED["client_roles_created"]["body"]}
ROLE_CLIENT_ROLE = CLIENT_ROLES["recorded-role"]
PERMISSION_CLIENT_ROLE = CLIENT_ROLES["recorded:read"]
GROUP = RECORDED["organization_groups"]["body"][0]
API_CLIENT = RECORDED["api_client_find"]["body"][0]["id"]
ORGANIZATIONS = admin_path("/organizations")
ORGANIZATION = admin_path(f"/organizations/{ORGANIZATION_ID}")
GROUPS = admin_path(f"/organizations/{ORGANIZATION_ID}/groups")
CLIENT_ROLES_PATH = admin_path(f"/clients/{API_CLIENT}/roles")
# What the role's client role carries, as recorded.
ROLE_DATA = {"name": "recorded-role", "description": "Recorded role", "tenant_id": None}
PERMISSION_DATA = {"name": "recorded:read", "description": "Recorded"}
# Keycloak's answer to a write of a role other than the recorded one (role_update and role_delete name that one).
NO_CONTENT = httpx.Response(204)
# The organization as one listing and one group listing would answer it, for flows across organizations.
ONE_ORGANIZATION = httpx.Response(200, json=[{"id": ORGANIZATION_ID, "alias": TENANT_ID, "name": "Recorded Co"}])


def adapter(api: RecordedAdminApi, links: Links | None = None) -> KeycloakSyncAdapter:
    return KeycloakSyncAdapter(api.api(), Links() if links is None else links)


def refused(result, operation: str, refusal: Refusal | None = None, status: int | None = None) -> SyncError:
    """The result's error, after checking its operation, its HTTP status and the adapter's refusal."""
    assert result.is_error()
    error = result.error
    assert (error.operation, error.context.get("status"), error.context.get("refusal")) == (
        operation,
        status,
        refusal,
    )
    return error


def writes(api: RecordedAdminApi) -> list[tuple[str, str]]:
    """The calls that change the realm: every one but the token grant and the reads."""
    return [(sent.method, sent.path) for sent in api.sent[1:] if sent.method != "GET"]


class TestTenants:
    async def test_a_new_tenant_gets_an_organization_named_by_its_id_and_is_linked_to_it(self):
        api = RecordedAdminApi(["token", "organization_find_none", "organization_create"])
        links = Links()

        result = await adapter(api, links).sync_tenant(
            tenant_id=TENANT, data={"name": "Recorded Co", "self_provisioning_domains": ["recorded.test"]}
        )

        assert result == Ok(None)
        assert api.sent[2] == Sent(
            "POST",
            ORGANIZATIONS,
            {},
            {"name": "Recorded Co", "alias": TENANT_ID, "domains": [{"name": "recorded.test"}]},
            api.sent[2].authorization,
        )
        assert links.aliases == {TENANT: TENANT_ID}

    async def test_an_organization_that_matches_is_not_written(self):
        api = RecordedAdminApi(["token", "organization_find"])
        links = Links(aliases={TENANT: TENANT_ID})

        result = await adapter(api, links).sync_tenant(tenant_id=TENANT, data={"name": FOUND_ORGANIZATION["name"]})

        assert (result, writes(api)) == (Ok(None), [])

    async def test_an_update_sends_the_organization_back_with_the_tenants_fields(self):
        api = RecordedAdminApi(["token", "organization_find", "organization_update"])
        links = Links(aliases={TENANT: TENANT_ID})

        result = await adapter(api, links).sync_tenant(
            tenant_id=TENANT,
            data={"name": "Renamed", "self_provisioning_domains": ["recorded.test"], "status": "suspended"},
        )

        assert result == Ok(None)
        assert api.sent[2].body == {
            **FOUND_ORGANIZATION,
            "name": "Renamed",
            "domains": [{"name": "recorded.test"}],
            "enabled": False,
        }

    async def test_a_linked_tenant_reaches_the_organization_it_is_linked_to(self):
        api = RecordedAdminApi(["token", "organization_find_none", "organization_create"])
        links = Links(aliases={TENANT: "acme-alias"})

        await adapter(api, links).sync_tenant(tenant_id=TENANT, data={"name": "Recorded Co"})

        assert api.sent[1].query == {"q": "alias:acme-alias", "exact": "true"}
        assert api.sent[2].body == {"name": "Recorded Co", "alias": "acme-alias"}

    async def test_a_tenant_linked_to_another_provider_is_refused_without_a_call(self):
        api = RecordedAdminApi([])

        result = await adapter(api, Links(elsewhere={TENANT})).sync_tenant(tenant_id=TENANT, data={"name": "Co"})

        refused(result, "sync_tenant", Refusal.TENANT_LINKED_ELSEWHERE)
        assert api.sent == []

    @pytest.mark.parametrize(
        ("data", "refusal"),
        [
            ({}, Refusal.MISSING_NAME),
            ({"name": "Co", "self_provisioning_domains": "recorded.test"}, Refusal.BAD_DOMAINS),
            ({"name": "Co", "status": "archived"}, Refusal.UNKNOWN_STATUS),
        ],
        ids=["no name", "domains not a list", "unknown status"],
    )
    async def test_bad_data_is_refused_without_a_call(self, data, refusal):
        api = RecordedAdminApi([])

        refused(await adapter(api).sync_tenant(tenant_id=TENANT, data=data), "sync_tenant", refusal)
        assert api.sent == []

    async def test_delete_deletes_the_organization_and_a_repeat_finds_nothing_to_delete(self):
        api = RecordedAdminApi(["token", "organization_find", "organization_delete", "organization_find_none"])
        sync = adapter(api)

        assert [await sync.delete_tenant(tenant_id=TENANT) for _ in range(2)] == [Ok(None), Ok(None)]
        assert writes(api) == [("DELETE", ORGANIZATION)]


class TestUsers:
    async def test_a_user_created_in_a_tenant_is_created_and_linked(self):
        api = RecordedAdminApi(["token", "user_find_none", "user_create", "user_find"])
        links = Links()
        data = {
            "email": "recorded@recorded.test",
            "status": "active",
            "given_name": "Rec",
            "family_name": "Orded",
            CREATED_IN_TENANT: TENANT_ID,
        }

        result = await adapter(api, links).sync_user(user_id=USER, data=data)

        assert result == Ok(None)
        assert api.sent[2].body == {
            "username": USER_ID,
            "email": "recorded@recorded.test",
            "enabled": True,
            "firstName": "Rec",
            "lastName": "Orded",
        }
        assert links.linked_users == {USER: (KEYCLOAK_USER_ID, "recorded@recorded.test")}

    async def test_a_create_whose_link_was_lost_is_linked_by_the_repeat(self):
        api = RecordedAdminApi(["token", "user_find", "user_find"])
        links = Links()
        data = {"email": FOUND_USER["email"], "status": "active", CREATED_IN_TENANT: TENANT_ID}

        result = await adapter(api, links).sync_user(user_id=USER, data=data)

        assert (result, writes(api)) == (Ok(None), [])
        assert links.linked_users == {USER: (KEYCLOAK_USER_ID, FOUND_USER["email"])}

    async def test_an_unchanged_profile_writes_nothing(self):
        api = RecordedAdminApi(["token", "user_find"])

        result = await adapter(api).sync_user(user_id=USER, data={"email": FOUND_USER["email"], "status": "active"})

        assert (result, writes(api)) == (Ok(None), [])

    async def test_a_changed_email_and_name_are_pushed_and_the_email_is_no_longer_verified(self):
        api = RecordedAdminApi(["token", "user_find", "user_update"])

        await adapter(api).sync_user(
            user_id=USER, data={"email": "renamed@recorded.test", "status": "inactive", "given_name": "Rec"}
        )

        # No STATUS_CHANGED: the status is not pushed, so a realm admin's enable or disable stands.
        assert api.sent[2].body == {"email": "renamed@recorded.test", "emailVerified": False, "firstName": "Rec"}

    async def test_a_status_change_pushes_enabled(self):
        api = RecordedAdminApi(["token", "user_find", "user_update"])

        await adapter(api).sync_user(
            user_id=USER, data={"email": FOUND_USER["email"], "status": "inactive", STATUS_CHANGED: True}
        )

        assert api.sent[2].body == {"enabled": False}

    @pytest.mark.parametrize(
        "data",
        [
            {"email": "a@recorded.test", "status": "inactive", STATUS_CHANGED: True},
            {"email": "a@recorded.test", "status": "active", "left_tenant_id": TENANT_ID},
        ],
        ids=["status change", "leaving a tenant"],
    )
    async def test_a_user_keycloak_lacks_is_an_error_and_no_account_is_created(self, data):
        api = RecordedAdminApi(["token", "user_find_none"])

        refused(await adapter(api).sync_user(user_id=USER, data=data), "sync_user", Refusal.USER_NOT_IN_KEYCLOAK)
        assert writes(api) == []

    async def test_leaving_a_tenant_ends_the_membership_and_pushes_no_status(self):
        api = RecordedAdminApi(["token", "user_find", "organization_find", "member_remove"])

        result = await adapter(api).sync_user(
            user_id=USER, data={"email": FOUND_USER["email"], "status": "inactive", "left_tenant_id": TENANT_ID}
        )

        assert (result, writes(api)) == (
            Ok(None),
            [("DELETE", admin_path(f"/organizations/{ORGANIZATION_ID}/members/{KEYCLOAK_USER_ID}"))],
        )

    async def test_a_linked_account_gets_no_email_or_name(self):
        account = {**RECORDED["user_get"]["body"], "username": "signed-up-in-keycloak"}
        api = RecordedAdminApi(["token", httpx.Response(200, json=account)])
        links = Links(subjects={USER: account["id"]})

        result = await adapter(api, links).sync_user(
            user_id=USER, data={"email": "other@recorded.test", "status": "active", "given_name": "Other"}
        )

        assert (result, writes(api)) == (Ok(None), [])

    async def test_delete_deletes_the_account_and_a_repeat_finds_nothing(self):
        api = RecordedAdminApi(["token", "user_find", "user_delete", "user_find_none"])
        sync = adapter(api)

        assert [await sync.delete_user(user_id=USER) for _ in range(2)] == [Ok(None), Ok(None)]
        assert writes(api) == [("DELETE", admin_path(f"/users/{KEYCLOAK_USER_ID}"))]


class TestPermissions:
    async def test_a_new_permission_is_a_client_role_carrying_its_id(self):
        api = RecordedAdminApi(["token", "api_client_find", "client_roles", "client_role_create_permission"])

        result = await adapter(api).sync_permission(permission_id=PERMISSION, data=PERMISSION_DATA)

        assert result == Ok(None)
        assert (api.sent[3].path, api.sent[3].body) == (
            CLIENT_ROLES_PATH,
            {**PERMISSION_DATA, "attributes": {PERMISSION_ATTRIBUTE: [PERMISSION_ID]}},
        )

    async def test_a_permission_already_there_is_not_written(self):
        api = RecordedAdminApi(["token", "api_client_find", "client_roles_created"])

        result = await adapter(api).sync_permission(permission_id=PERMISSION, data=PERMISSION_DATA)

        assert (result, writes(api)) == (Ok(None), [])

    async def test_a_rename_renames_the_client_role_found_by_its_id(self):
        api = RecordedAdminApi(["token", "api_client_find", "client_roles_created", NO_CONTENT])

        await adapter(api).sync_permission(
            permission_id=PERMISSION,
            data={**PERMISSION_DATA, "name": "recorded:view", "previous_name": "recorded:read"},
        )

        assert (api.sent[3].path, api.sent[3].body) == (
            admin_path(f"/roles-by-id/{PERMISSION_CLIENT_ROLE['id']}"),
            {"name": "recorded:view", "description": "Recorded", "attributes": {PERMISSION_ATTRIBUTE: [PERMISSION_ID]}},
        )

    async def test_a_realm_client_role_of_that_name_without_an_id_is_stamped(self):
        """The realm import's permissions carry no canonical id: the first push adopts them."""
        api = RecordedAdminApi(["token", "api_client_find", "client_roles", NO_CONTENT])
        imported = CLIENT_ROLES["documents:read"]

        await adapter(api).sync_permission(permission_id=PERMISSION, data={"name": "documents:read"})

        assert api.sent[3].body == {
            "name": "documents:read",
            "description": imported.get("description", ""),
            "attributes": {PERMISSION_ATTRIBUTE: [PERMISSION_ID]},
        }

    async def test_a_name_another_canonical_entity_holds_is_refused(self):
        api = RecordedAdminApi(["token", "api_client_find", "client_roles_created"])

        result = await adapter(api).sync_permission(permission_id=uuid.uuid4(), data=PERMISSION_DATA)

        refused(result, "sync_permission", Refusal.NAME_HELD_BY_ANOTHER)
        assert writes(api) == []

    async def test_delete_deletes_the_client_role_and_a_repeat_finds_nothing(self):
        api = RecordedAdminApi(["token", "api_client_find", "client_roles_created", NO_CONTENT, "client_roles"])
        sync = adapter(api)

        results = [await sync.delete_permission(permission_id=PERMISSION, data={"name": "recorded:read"}) for _ in "12"]

        assert results == [Ok(None), Ok(None)]
        assert writes(api) == [("DELETE", admin_path(f"/roles-by-id/{PERMISSION_CLIENT_ROLE['id']}"))]


class TestRoles:
    async def test_a_new_role_is_a_composite_of_its_permissions(self):
        api = RecordedAdminApi(
            [
                "token",
                "api_client_find",
                "client_roles",
                "client_role_create_role",
                "client_roles_created",
                "role_composites_none",
                "composites_add",
            ]
        )

        result = await adapter(api).sync_role(role_id=ROLE, data={**ROLE_DATA, "permission_names": ["recorded:read"]})

        assert result == Ok(None)
        assert api.sent[3].body == {
            "name": "recorded-role",
            "description": "Recorded role",
            "attributes": {ROLE_ATTRIBUTE: [ROLE_ID]},
        }
        assert (api.sent[6].path, api.sent[6].body) == (
            admin_path(f"/roles-by-id/{ROLE_CLIENT_ROLE['id']}/composites"),
            [{"id": PERMISSION_CLIENT_ROLE["id"]}],
        )

    async def test_a_repeat_changes_nothing(self):
        api = RecordedAdminApi(
            ["token", "api_client_find", "client_roles_created", "client_roles_created", "role_composites"]
        )

        result = await adapter(api).sync_role(role_id=ROLE, data={**ROLE_DATA, "permission_names": ["recorded:read"]})

        assert (result, writes(api)) == (Ok(None), [])

    async def test_a_permission_taken_off_the_role_is_taken_out_of_its_composites(self):
        api = RecordedAdminApi(
            [
                "token",
                "api_client_find",
                "client_roles_created",
                "client_roles_created",
                "role_composites",
                "composites_remove",
            ]
        )

        await adapter(api).sync_role(role_id=ROLE, data={**ROLE_DATA, "permission_names": []})

        assert (api.sent[5].method, api.sent[5].body) == ("DELETE", [{"id": PERMISSION_CLIENT_ROLE["id"]}])

    async def test_a_permission_keycloak_lacks_is_refused_and_the_composites_are_kept(self):
        api = RecordedAdminApi(["token", "api_client_find", "client_roles_created", "client_roles_created"])

        result = await adapter(api).sync_role(role_id=ROLE, data={**ROLE_DATA, "permission_names": ["absent:read"]})

        refused(result, "sync_role", Refusal.PERMISSION_NOT_IN_KEYCLOAK)
        assert writes(api) == []

    async def test_a_tenants_own_role_is_named_with_its_tenant(self):
        assert role_client_name("editor", TENANT_ID) == f"editor@{TENANT_ID}"
        assert role_client_name("editor", None) == "editor"

    async def test_a_rename_renames_the_roles_groups_before_its_client_role(self):
        api = RecordedAdminApi(
            [
                "token",
                "api_client_find",
                "client_roles_created",
                ONE_ORGANIZATION,
                "organization_groups",
                "organization_group_update",
                "role_update",
            ]
        )

        result = await adapter(api).sync_role(
            role_id=ROLE, data={**ROLE_DATA, "name": "renamed-role", "previous_name": "recorded-role"}
        )

        assert result == Ok(None)
        assert [(sent.method, sent.path, sent.body) for sent in api.sent[5:]] == [
            ("PUT", f"{GROUPS}/{GROUP['id']}", {"name": "renamed-role", "attributes": {ROLE_ATTRIBUTE: [ROLE_ID]}}),
            (
                "PUT",
                admin_path(f"/roles-by-id/{ROLE_CLIENT_ROLE['id']}"),
                {"name": "renamed-role", "description": "Recorded role", "attributes": {ROLE_ATTRIBUTE: [ROLE_ID]}},
            ),
        ]

    async def test_delete_deletes_the_roles_groups_then_its_client_role(self):
        api = RecordedAdminApi(
            [
                "token",
                "api_client_find",
                "client_roles_created",
                ONE_ORGANIZATION,
                "organization_groups",
                "organization_group_delete",
                "role_delete",
            ]
        )

        result = await adapter(api).delete_role(role_id=ROLE, data={"name": "recorded-role", "tenant_id": None})

        assert (result, writes(api)) == (
            Ok(None),
            [
                ("DELETE", f"{GROUPS}/{GROUP['id']}"),
                ("DELETE", admin_path(f"/roles-by-id/{ROLE_CLIENT_ROLE['id']}")),
            ],
        )


class TestRoleAssignments:
    ASSIGN = ["token", "organization_find", "user_find", "api_client_find", "client_roles_created"]

    async def test_an_assignment_makes_the_user_a_member_of_the_organization_and_the_roles_group(self):
        api = RecordedAdminApi(
            [
                *self.ASSIGN,
                "member_add",
                "organization_groups_none",
                "organization_group_create",
                "organization_groups",
                "group_member_add",
            ]
        )

        result = await adapter(api).sync_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE)

        assert result == Ok(None)
        assert writes(api) == [
            ("POST", admin_path(f"/organizations/{ORGANIZATION_ID}/members")),
            ("POST", GROUPS),
            ("PUT", f"{GROUPS}/{GROUP['id']}/members/{KEYCLOAK_USER_ID}"),
        ]
        assert api.sent[7].body == {"name": "recorded-role", "attributes": {ROLE_ATTRIBUTE: [ROLE_ID]}}

    async def test_a_repeat_creates_no_group(self):
        api = RecordedAdminApi([*self.ASSIGN, "member_add_again", "organization_groups", "group_member_add_again"])

        result = await adapter(api).sync_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE)

        assert result == Ok(None)
        assert [path for _method, path in writes(api)] == [
            admin_path(f"/organizations/{ORGANIZATION_ID}/members"),
            f"{GROUPS}/{GROUP['id']}/members/{KEYCLOAK_USER_ID}",
        ]

    async def test_a_role_keycloak_lacks_is_refused_and_nothing_is_added(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find", "api_client_find", "client_roles"])

        result = await adapter(api).sync_role_assignment(user_id=USER, tenant_id=TENANT, role_id=uuid.uuid4())

        refused(result, "sync_role_assignment", Refusal.ROLE_NOT_IN_KEYCLOAK)
        assert writes(api) == []

    async def test_a_tenant_keycloak_lacks_is_refused(self):
        api = RecordedAdminApi(["token", "organization_find_none"])

        result = await adapter(api).sync_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE)

        refused(result, "sync_role_assignment", Refusal.NO_ORGANIZATION)

    async def test_unassigning_takes_the_user_out_of_the_group_and_a_repeat_is_done(self):
        removal = [*self.ASSIGN[1:], "organization_groups"]
        api = RecordedAdminApi(
            [
                "token",
                *removal,
                "group_member_remove",
                *removal[:2],
                "client_roles_created",
                "organization_groups",
                "group_member_remove_again",
            ]
        )
        sync = adapter(api)

        results = [
            await sync.delete_role_assignment(user_id=USER, tenant_id=TENANT, role_id=ROLE, data={"role_name": "x"})
            for _ in "12"
        ]

        assert results == [Ok(None), Ok(None)]
        assert writes(api) == [("DELETE", f"{GROUPS}/{GROUP['id']}/members/{KEYCLOAK_USER_ID}")] * 2

    async def test_the_last_role_in_the_tenant_ends_the_membership(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find", "member_remove"])

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE_IN_TENANT: True}
        )

        assert (result, writes(api)) == (
            Ok(None),
            [("DELETE", admin_path(f"/organizations/{ORGANIZATION_ID}/members/{KEYCLOAK_USER_ID}"))],
        )

    async def test_a_tenant_keycloak_lacks_has_nothing_to_end(self):
        api = RecordedAdminApi(["token", "organization_find_none"])

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE_IN_TENANT: True}
        )

        assert result == Ok(None)

    async def test_a_user_keycloak_lacks_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find", "user_find_none"])

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE_IN_TENANT: True}
        )

        refused(result, "delete_role_assignment", Refusal.USER_NOT_IN_KEYCLOAK)


class TestFailuresAreRecorded:
    """Every failure is an Error(SyncError) the services log and tell (``provider_sync``); none raises."""

    async def test_no_answer_is_unanswered_with_no_status(self):
        api = RecordedAdminApi(["token", httpx.ConnectError("connection refused")])

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE_IN_TENANT: True}
        )

        assert result.error.context == {
            "tenant_id": TENANT_ID,
            "user_id": USER_ID,
            "role_id": ROLE_ID,
            UNANSWERED: True,
            "status": None,
        }

    async def test_a_refusal_carries_keycloaks_status(self):
        api = RecordedAdminApi(["token", "organization_find", "organization_update_domain_taken"])

        result = await adapter(api, Links(aliases={TENANT: TENANT_ID})).sync_tenant(
            tenant_id=TENANT, data={"name": "Recorded Co", "self_provisioning_domains": ["acme.test"]}
        )

        error = refused(result, "sync_tenant", status=400)
        assert UNANSWERED not in error.context

    async def test_a_failed_membership_removal_is_an_error(self):
        """The revocation a still-present member answered 404 to is not done (#619)."""
        api = RecordedAdminApi(
            ["token", "organization_find", "user_find", "member_remove_again", "member_organizations"]
        )

        result = await adapter(api).delete_role_assignment(
            user_id=USER, tenant_id=TENANT, role_id=ROLE, data={LAST_ROLE_IN_TENANT: True}
        )

        refused(result, "delete_role_assignment", status=404)

    async def test_the_services_own_database_failing_is_an_error_without_a_call(self):
        api = RecordedAdminApi([])
        links = Links(
            failure=LocalDatabaseError("the IdP link read failed in the service's database: OperationalError")
        )

        result = await adapter(api, links).sync_user(user_id=USER, data={"email": "a@b.test", "status": "active"})

        assert result.error == SyncError(
            message="the IdP link read failed in the service's database: OperationalError",
            operation="sync_user",
            context={"user_id": USER_ID, "status": None},
        )
        assert api.sent == []

    async def test_a_link_the_canonical_side_refuses_is_an_error(self):
        api = RecordedAdminApi(["token", "organization_find_none", "organization_create"])

        class Refusing(Links):
            async def link_organization(self, tenant_id: uuid.UUID, alias: str) -> None:
                raise LinkRefused("tenant gone")

        result = await adapter(api, Refusing()).sync_tenant(tenant_id=TENANT, data={"name": "Recorded Co"})

        error = refused(result, "sync_tenant", Refusal.LINK_REFUSED)
        assert error.message == f"{Refusal.LINK_REFUSED.value}: tenant gone"
