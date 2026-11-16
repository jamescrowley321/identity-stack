"""The read-only Keycloak organization lookup: one service account, one call, every failure typed.

The Admin API is an ``httpx.MockTransport`` that records every request, so a
test can say what was asked and that nothing was asked at all.
"""

import base64

import pytest

from app.middleware.keycloak import KeycloakTokenModel
from app.middleware.providers import (
    KeycloakOrganizationLookup,
    KeycloakTenantMemberships,
    TenantMembership,
    keycloak_config,
)
from app.services.keycloak_directory import (
    GRANT_ATTRIBUTE_PREFIX,
    IssuerNamesNoRealm,
    KeycloakDirectory,
    KeycloakDirectoryError,
    MemberIdRefused,
    MemberUnknown,
    OrganizationMembership,
)
from tests.helpers.keycloak_admin_api import (
    ACME,
    GLOBEX,
    ISSUER,
    MEMBER_ID,
    NO_SUCH_ROUTE_BODY,
    ORGANIZATIONS_DISABLED_BODY,
    ORIGIN,
    OTHER_MEMBER_ID,
    REALM_NOT_FOUND_BODY,
    SERVICE_ACCOUNT_TOKEN,
    TENANT_ADMIN_MARKER,
    AdminApi,
    organization,
)
from tests.helpers.keycloak_model import (
    DIRECTORY_CLIENT_ID,
    DIRECTORY_CLIENT_SECRET,
    MEMBER_TYPE_CLIENT,
    TENANT_ADMIN_APP_CLIENT,
    TENANT_ADMIN_TYPE_CLIENT,
    model_settings,
)


class TestTheCall:
    async def test_one_lookup_asks_for_the_members_organizations_with_their_attributes(self):
        api = AdminApi([organization(ACME)])

        await api.directory().organizations_of(MEMBER_ID)

        (lookup,) = api.lookups()
        assert lookup.method == "GET"
        assert lookup.url.raw_path.decode() == (
            f"/admin/realms/identity-stack/organizations/members/{MEMBER_ID}/organizations?briefRepresentation=false"
        )
        assert lookup.headers["Authorization"] == f"Bearer {SERVICE_ACCOUNT_TOKEN}"

    async def test_the_service_account_token_comes_from_a_client_credentials_grant(self):
        api = AdminApi([organization(ACME)])

        await api.directory().organizations_of(MEMBER_ID)

        (grant,) = api.grants()
        assert grant.method == "POST"
        assert grant.content == b"grant_type=client_credentials"
        credentials = base64.b64encode(f"{DIRECTORY_CLIENT_ID}:{DIRECTORY_CLIENT_SECRET}".encode()).decode()
        assert grant.headers["Authorization"] == f"Basic {credentials}"

    async def test_the_token_is_kept_for_the_next_lookup(self):
        api = AdminApi([organization(ACME)])
        directory = api.directory()

        await directory.organizations_of(MEMBER_ID)
        await directory.organizations_of(OTHER_MEMBER_ID)

        assert len(api.grants()) == 1
        assert len(api.lookups()) == 2

    async def test_a_lookup_answered_401_is_asked_once_more_with_a_new_token(self):
        api = AdminApi([organization(ACME, name="Acme")], lookup_status=[401, 200])

        memberships = await api.directory().organizations_of(MEMBER_ID)

        assert memberships == (OrganizationMembership(alias=ACME, name="Acme", granted_types=frozenset()),)
        assert len(api.grants()) == 2
        assert len(api.lookups()) == 2

    async def test_a_second_401_is_a_failure_after_exactly_two_lookups(self):
        api = AdminApi([organization(ACME)], lookup_status=401)

        with pytest.raises(KeycloakDirectoryError) as failed:
            await api.directory().organizations_of(MEMBER_ID)

        assert failed.value.status == 401
        assert len(api.grants()) == 2
        assert len(api.lookups()) == 2

    async def test_a_token_refused_twice_is_not_kept(self):
        api = AdminApi([organization(ACME)], lookup_status=[401, 401, 200])
        directory = api.directory()

        with pytest.raises(KeycloakDirectoryError):
            await directory.organizations_of(MEMBER_ID)
        await directory.organizations_of(MEMBER_ID)

        assert len(api.grants()) == 3

    @pytest.mark.parametrize("status", [403, 404, 500])
    async def test_no_other_status_is_asked_again(self, status):
        api = AdminApi(lookup_status=status)

        with pytest.raises((KeycloakDirectoryError, MemberUnknown)):
            await api.directory().organizations_of(MEMBER_ID)

        assert len(api.lookups()) == 1

    async def test_the_answer_is_every_organization_with_the_types_its_markers_grant_the_member(self):
        api = AdminApi(
            [
                organization(ACME, name="Acme", attributes={TENANT_ADMIN_MARKER: [OTHER_MEMBER_ID, MEMBER_ID]}),
                organization(GLOBEX, name="Globex", attributes={TENANT_ADMIN_MARKER: [OTHER_MEMBER_ID]}),
            ]
        )

        memberships = await api.directory().organizations_of(MEMBER_ID)

        assert {membership.alias: membership for membership in memberships} == {
            ACME: OrganizationMembership(alias=ACME, name="Acme", granted_types=frozenset({TENANT_ADMIN_TYPE_CLIENT})),
            GLOBEX: OrganizationMembership(alias=GLOBEX, name="Globex", granted_types=frozenset()),
        }

    @pytest.mark.parametrize(
        "attributes",
        [
            None,
            [],
            "type:tenant-admin",
            {TENANT_ADMIN_MARKER: MEMBER_ID},
            {TENANT_ADMIN_MARKER: {MEMBER_ID: True}},
            {GRANT_ATTRIBUTE_PREFIX: [MEMBER_ID]},
            {TENANT_ADMIN_TYPE_CLIENT: [MEMBER_ID]},
            {f"{TENANT_ADMIN_MARKER}x": [OTHER_MEMBER_ID]},
        ],
        ids=repr,
    )
    async def test_a_marker_of_any_other_shape_grants_nothing(self, attributes):
        entry = organization(ACME)
        entry["attributes"] = attributes
        api = AdminApi([entry])

        (membership,) = await api.directory().organizations_of(MEMBER_ID)

        assert membership.granted_types == frozenset()

    async def test_an_organization_with_no_name_keeps_its_alias(self):
        entry = organization(ACME)
        del entry["name"]
        api = AdminApi([entry])

        (membership,) = await api.directory().organizations_of(MEMBER_ID)

        assert membership == OrganizationMembership(alias=ACME, name="", granted_types=frozenset())

    def test_two_directories_from_the_same_settings_are_equal(self):
        assert AdminApi().directory() == KeycloakDirectory(
            issuer=ISSUER, client_id=DIRECTORY_CLIENT_ID, client_secret=DIRECTORY_CLIENT_SECRET
        )


class TestTheMemberId:
    @pytest.mark.parametrize(
        "sub",
        [None, 7, "", " ", "a/b", "../users", "a%2Fb", "a?b=c", "a#b", "a b", "-leading", ".", "a" * 256, "a\nb"],
        ids=repr,
    )
    async def test_a_sub_that_is_not_a_well_formed_id_is_refused_before_any_call(self, sub):
        api = AdminApi([organization(ACME)])

        with pytest.raises(MemberIdRefused):
            await api.directory().organizations_of(sub)

        assert api.requests == []

    @pytest.mark.parametrize(
        ("sub", "in_the_path"),
        [
            (MEMBER_ID, MEMBER_ID),
            ("f:ldap-provider:42", "f%3Aldap-provider%3A42"),
            ("user.name_1", "user.name_1"),
            ("a" * 255, "a" * 255),
        ],
    )
    async def test_a_well_formed_id_is_url_encoded_into_the_path(self, sub, in_the_path):
        api = AdminApi([])

        await api.directory().organizations_of(sub)

        (lookup,) = api.lookups()
        assert lookup.url.raw_path.decode().split("/")[-2] == in_the_path


class TestEveryFailureIsTyped:
    @pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
    async def test_a_refused_grant_carries_its_status_and_no_lookup_is_made(self, status):
        api = AdminApi(token_status=status)

        with pytest.raises(KeycloakDirectoryError) as failed:
            await api.directory().organizations_of(MEMBER_ID)

        assert failed.value.status == status
        assert api.lookups() == []

    @pytest.mark.parametrize("status", [401, 403, 500, 503])
    async def test_a_refused_lookup_carries_its_status(self, status):
        api = AdminApi(lookup_status=status)

        with pytest.raises(KeycloakDirectoryError) as failed:
            await api.directory().organizations_of(MEMBER_ID)

        assert failed.value.status == status

    async def test_a_member_the_realm_does_not_know_is_typed_apart_and_logged_with_its_status(self, caplog):
        """The user was deleted from the realm while their token still lives: Keycloak answers 404."""
        api = AdminApi(lookup_status=404)

        with caplog.at_level("WARNING", logger="app.services.keycloak_directory"):
            with pytest.raises(MemberUnknown):
                await api.directory().organizations_of(MEMBER_ID)

        messages = [r.getMessage() for r in caplog.records if r.name == "app.services.keycloak_directory"]
        assert any("404" in message for message in messages), messages

    @pytest.mark.parametrize(
        "body",
        [
            ORGANIZATIONS_DISABLED_BODY,
            REALM_NOT_FOUND_BODY,
            NO_SUCH_ROUTE_BODY,
            b'{"error":"HTTP 404 Not Found","detail":"more"}',
            b"<html><body>404 Not Found</body></html>",
            b"",
        ],
        ids=["organizations-disabled", "realm-not-found", "no-such-route", "other-json", "proxy-html", "empty"],
    )
    async def test_any_other_404_is_a_directory_failure_not_an_unknown_member(self, body, caplog):
        """A realm-wide fault answers 404 too; only the unknown member's answer means no organizations."""
        api = AdminApi(lookup_status=404, lookup_body=body)

        with caplog.at_level("WARNING", logger="app.services.keycloak_directory"):
            with pytest.raises(KeycloakDirectoryError) as failed:
                await api.directory().organizations_of(MEMBER_ID)

        assert failed.value.status == 404
        assert not isinstance(failed.value, MemberUnknown)
        messages = [r.getMessage() for r in caplog.records if r.name == "app.services.keycloak_directory"]
        assert any("404" in message for message in messages), messages

    async def test_no_answer_at_all_has_no_status(self):
        api = AdminApi(transport_error=True)

        with pytest.raises(KeycloakDirectoryError) as failed:
            await api.directory().organizations_of(MEMBER_ID)

        assert failed.value.status is None

    @pytest.mark.parametrize(
        "token_body",
        [{"expires_in": 300}, {"access_token": "", "expires_in": 300}, {"access_token": 7}, ["not", "a", "map"]],
        ids=repr,
    )
    async def test_a_grant_with_no_usable_token_is_a_failure(self, token_body):
        api = AdminApi(token_body=token_body)

        with pytest.raises(KeycloakDirectoryError):
            await api.directory().organizations_of(MEMBER_ID)

        assert api.lookups() == []

    @pytest.mark.parametrize(
        "body",
        [b"not json", b'{"alias": "acme"}', b'"acme"', b'[{"name": "Acme"}]', b'[{"alias": ""}]', b'["acme"]'],
        ids=repr,
    )
    async def test_an_answer_that_is_not_a_list_of_organizations_is_a_failure(self, body):
        api = AdminApi(lookup_body=body)

        with pytest.raises(KeycloakDirectoryError) as failed:
            await api.directory().organizations_of(MEMBER_ID)

        assert failed.value.status is None

    @pytest.mark.parametrize("issuer", [ORIGIN, f"{ORIGIN}/realms/", f"{ISSUER}/extra", f"{ORIGIN}/auth/x"])
    def test_an_issuer_that_names_no_realm_builds_no_directory(self, issuer):
        with pytest.raises(IssuerNamesNoRealm):
            AdminApi([]).directory(issuer=issuer)

    @pytest.mark.parametrize(("token_status", "lookup_status"), [(401, 200), (200, 401), (200, 403)])
    async def test_the_log_names_the_status_and_not_the_secret(self, caplog, token_status, lookup_status):
        api = AdminApi(token_status=token_status, lookup_status=lookup_status)

        with caplog.at_level("WARNING", logger="app.services.keycloak_directory"):
            with pytest.raises(KeycloakDirectoryError):
                await api.directory().organizations_of(MEMBER_ID)

        messages = [r.getMessage() for r in caplog.records if r.name == "app.services.keycloak_directory"]
        assert any(str(token_status if token_status != 200 else lookup_status) in m for m in messages), messages
        assert not any(DIRECTORY_CLIENT_SECRET in m or SERVICE_ACCOUNT_TOKEN in m for m in messages)

    def test_the_secret_is_not_in_the_repr(self):
        assert DIRECTORY_CLIENT_SECRET not in repr(AdminApi().directory())


def _model() -> KeycloakTokenModel:
    provider = keycloak_config(
        ISSUER,
        frozenset({MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT}),
        require_https=False,
        **model_settings(MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT),
    )
    return provider.tenant_claims.model


def _tenant_admin_claims(organization: str, *, sub: object = MEMBER_ID) -> dict:
    return {
        "sub": sub,
        "aud": TENANT_ADMIN_TYPE_CLIENT,
        "azp": TENANT_ADMIN_APP_CLIENT,
        "organization": [organization],
        "resource_access": {TENANT_ADMIN_TYPE_CLIENT: {"roles": ["settings:manage"]}},
    }


class TestTheLookupConfirmsTheGrantAndNotOnlyTheMembership:
    """The owner's case on #559: client roles are realm-wide, the per-organization marker is not."""

    async def test_a_tenant_admin_of_the_tokens_one_organization_is_confirmed(self):
        api = AdminApi([organization(ACME, attributes={TENANT_ADMIN_MARKER: [MEMBER_ID]})])

        answer = await KeycloakOrganizationLookup(_model(), api.directory())(_tenant_admin_claims(ACME))

        assert answer == [ACME]

    async def test_a_tenant_admin_who_moved_to_an_organization_that_does_not_mark_them_is_not(self):
        """Admin of A, joins B as a member, leaves A: the roles stay, B grants nothing."""
        api = AdminApi([organization(GLOBEX, attributes={TENANT_ADMIN_MARKER: [OTHER_MEMBER_ID]})])

        answer = await KeycloakOrganizationLookup(_model(), api.directory())(_tenant_admin_claims(GLOBEX))

        assert answer == []

    async def test_a_marker_for_another_type_does_not_grant_this_one(self):
        api = AdminApi([organization(ACME, attributes={f"{GRANT_ATTRIBUTE_PREFIX}member": [MEMBER_ID]})])

        answer = await KeycloakOrganizationLookup(_model(), api.directory())(_tenant_admin_claims(ACME))

        assert answer == []

    async def test_every_other_membership_counts_against_the_caller(self):
        """Decision 10 stays: marked in the token's organization, but in two organizations."""
        api = AdminApi(
            [
                organization(ACME, attributes={TENANT_ADMIN_MARKER: [MEMBER_ID]}),
                organization(GLOBEX),
            ]
        )

        answer = await KeycloakOrganizationLookup(_model(), api.directory())(_tenant_admin_claims(ACME))

        assert sorted(answer) == [ACME, GLOBEX]

    @pytest.mark.parametrize("sub", [None, "a/b", ""], ids=repr)
    async def test_a_sub_that_is_not_an_id_belongs_to_no_organization_and_nothing_is_asked(self, sub):
        api = AdminApi([organization(ACME, attributes={TENANT_ADMIN_MARKER: [MEMBER_ID]})])

        answer = await KeycloakOrganizationLookup(_model(), api.directory())(_tenant_admin_claims(ACME, sub=sub))

        assert answer == []
        assert api.requests == []

    async def test_a_member_the_realm_does_not_know_belongs_to_no_organization(self):
        api = AdminApi(lookup_status=404)

        answer = await KeycloakOrganizationLookup(_model(), api.directory())(_tenant_admin_claims(ACME))

        assert answer == []

    async def test_a_failure_is_raised_for_the_resolver_to_answer_502(self):
        api = AdminApi(lookup_status=503)

        with pytest.raises(KeycloakDirectoryError):
            await KeycloakOrganizationLookup(_model(), api.directory())(_tenant_admin_claims(ACME))


class TestTheMembershipList:
    async def test_every_organization_is_listed_with_its_name(self):
        api = AdminApi([organization(ACME, name="Acme"), organization(GLOBEX, name="Globex")])

        listed = await KeycloakTenantMemberships(_model(), api.directory())(_tenant_admin_claims(ACME))

        assert {m.tenant_id: m for m in listed} == {
            ACME: TenantMembership(tenant_id=ACME, tenant_name="Acme", counted=False),
            GLOBEX: TenantMembership(tenant_id=GLOBEX, tenant_name="Globex", counted=True),
        }

    async def test_the_tokens_organization_is_counted_where_it_marks_the_caller(self):
        api = AdminApi(
            [
                organization(ACME, name="Acme", attributes={TENANT_ADMIN_MARKER: [MEMBER_ID]}),
                organization(GLOBEX, name="Globex"),
            ]
        )

        listed = await KeycloakTenantMemberships(_model(), api.directory())(_tenant_admin_claims(ACME))

        assert {m.tenant_id: m.counted for m in listed} == {ACME: True, GLOBEX: True}

    async def test_a_member_the_realm_does_not_know_lists_nothing(self):
        api = AdminApi(lookup_status=404)

        listed = await KeycloakTenantMemberships(_model(), api.directory())(_tenant_admin_claims(ACME))

        assert listed == []

    async def test_a_sub_that_is_not_an_id_lists_nothing_and_nothing_is_asked(self):
        api = AdminApi([organization(ACME)])

        listed = await KeycloakTenantMemberships(_model(), api.directory())(_tenant_admin_claims(ACME, sub="../x"))

        assert listed == []
        assert api.requests == []
