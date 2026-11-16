"""The Keycloak token model: what one realm token says, and every branch that refuses.

Token claims start from ``keycloak/fixtures/access-token.json``, the decoded
access token the local realm issued (user ``bob``, client ``member-app``, scope
``organization:acme``). Each test changes the one claim it is about.
"""

import json
from pathlib import Path

import pytest

from app.middleware.keycloak import (
    KeycloakClientType,
    KeycloakModelError,
    KeycloakTokenModel,
    TokenGrant,
    binding_failure,
    client_roles,
    local_issuer,
    organization,
    parse_app_clients,
    parse_portable_permissions,
    read_grant,
    single_audience,
    token_model,
)

FIXTURE = Path(__file__).resolve().parents[3] / "keycloak" / "fixtures" / "access-token.json"

MEMBER = "member"
TENANT_ADMIN = "tenant-admin"
MEMBER_APP = "member-app"
TENANT_ADMIN_APP = "tenant-admin-app"
ACME = "acme"
GLOBEX = "globex"
DOCUMENTS_READ = "documents:read"
DOCUMENTS_WRITE = "documents:write"
SETTINGS_MANAGE = "settings:manage"

AUDIENCES = frozenset({MEMBER, TENANT_ADMIN})
APP_CLIENTS = {MEMBER: MEMBER_APP, TENANT_ADMIN: TENANT_ADMIN_APP}
PORTABLE_PERMISSIONS = {MEMBER: frozenset({DOCUMENTS_READ})}


def _model(**changes) -> KeycloakTokenModel:
    settings = {
        "audiences": AUDIENCES,
        "app_clients": APP_CLIENTS,
        "portable_types": frozenset({MEMBER}),
        "organization_bound_types": frozenset({TENANT_ADMIN}),
        "portable_permissions": PORTABLE_PERMISSIONS,
    }
    settings.update(changes)
    return token_model(**settings)


MODEL = _model()


def _fixture_claims() -> dict:
    return json.loads(FIXTURE.read_text())["claims"]


def _claims(**changes) -> dict:
    """The fixture token with ``changes`` applied; a value of ``None`` removes the claim."""
    claims = {**_fixture_claims(), **changes}
    return {name: value for name, value in claims.items() if value is not None}


def _tenant_admin_claims(**changes) -> dict:
    return _claims(
        aud=TENANT_ADMIN,
        azp=TENANT_ADMIN_APP,
        resource_access={TENANT_ADMIN: {"roles": [SETTINGS_MANAGE, "members:invite"]}},
        **changes,
    )


class TestTheFixtureToken:
    def test_it_is_the_shape_the_model_is_built_for(self):
        claims = _fixture_claims()

        assert claims["aud"] == MEMBER
        assert claims["azp"] == MEMBER_APP
        assert claims["organization"] == [ACME]
        assert claims["resource_access"] == {MEMBER: {"roles": [DOCUMENTS_READ]}}

    def test_it_reads_as_a_portable_grant_in_its_organization(self):
        assert read_grant(MODEL, _fixture_claims()) == TokenGrant(
            organization=ACME, permissions=(DOCUMENTS_READ,), organization_bound=False
        )


class TestParsing:
    def test_app_clients_are_type_to_app_pairs(self):
        assert parse_app_clients(" member = member-app , tenant-admin=tenant-admin-app ,,") == APP_CLIENTS

    def test_client_ids_keep_their_case(self):
        assert parse_app_clients("Member=Member-App") == {"Member": "Member-App"}

    def test_nothing_configured_is_no_pair(self):
        assert parse_app_clients("") == {}
        assert parse_portable_permissions("") == {}

    @pytest.mark.parametrize("raw", ["member", "member=", "=member-app", "member=a=b"])
    def test_an_entry_that_is_not_one_pair_is_refused(self, raw):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_APP_CLIENTS"):
            parse_app_clients(raw)

    def test_two_app_clients_for_one_type_are_refused(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_APP_CLIENTS"):
            parse_app_clients("member=member-app,member=other-app")

    def test_portable_permissions_are_gathered_per_type(self):
        parsed = parse_portable_permissions("member=documents:read, member=documents:list ,viewer=reports:read")

        assert parsed == {
            MEMBER: frozenset({DOCUMENTS_READ, "documents:list"}),
            "viewer": frozenset({"reports:read"}),
        }

    @pytest.mark.parametrize("raw", ["documents:read", "member=", "=documents:read"])
    def test_a_permission_entry_that_is_not_one_pair_is_refused(self, raw):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_PORTABLE_PERMISSIONS"):
            parse_portable_permissions(raw)


class TestTheModelRefusesAHalfConfiguredRealm:
    def test_the_fixture_realm_is_accepted(self):
        assert MODEL.types == (
            KeycloakClientType(
                type_client=MEMBER,
                app_client=MEMBER_APP,
                organization_bound=False,
                portable_permissions=frozenset({DOCUMENTS_READ}),
            ),
            KeycloakClientType(type_client=TENANT_ADMIN, app_client=TENANT_ADMIN_APP, organization_bound=True),
        )

    def test_two_builds_of_the_same_settings_are_equal(self):
        assert _model() == MODEL
        assert hash(_model()) == hash(MODEL)

    def test_no_azp_mapping_at_all(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_APP_CLIENTS"):
            _model(app_clients={})

    def test_a_type_with_no_app_client(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_APP_CLIENTS.*tenant-admin"):
            _model(app_clients={MEMBER: MEMBER_APP})

    def test_an_app_client_for_a_type_that_is_not_an_audience(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_APP_CLIENTS.*viewer"):
            _model(app_clients={**APP_CLIENTS, "viewer": "viewer-app"})

    def test_an_app_client_that_is_also_an_audience(self):
        """Its ID tokens carry ``aud`` and ``azp`` both naming it, and would bind."""
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_APP_CLIENTS.*member"):
            _model(app_clients={MEMBER: TENANT_ADMIN, TENANT_ADMIN: TENANT_ADMIN_APP})

    def test_a_type_declared_neither_portable_nor_organization_bound(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_PORTABLE_TYPES.*tenant-admin"):
            _model(organization_bound_types=frozenset())

    def test_no_type_declared_at_all(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_PORTABLE_TYPES"):
            _model(portable_types=frozenset(), organization_bound_types=frozenset(), portable_permissions={})

    def test_a_type_declared_both(self):
        with pytest.raises(KeycloakModelError, match="both.*member"):
            _model(organization_bound_types=frozenset({MEMBER, TENANT_ADMIN}))

    def test_a_declared_type_that_is_not_an_audience(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_AUDIENCES.*viewer"):
            _model(organization_bound_types=frozenset({TENANT_ADMIN, "viewer"}))

    def test_a_portable_type_with_no_permission_list(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_PORTABLE_PERMISSIONS.*member"):
            _model(portable_permissions={})

    def test_a_portable_type_with_an_empty_permission_list(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_PORTABLE_PERMISSIONS.*member"):
            _model(portable_permissions={MEMBER: frozenset()})

    @pytest.mark.parametrize(
        "permission",
        [DOCUMENTS_WRITE, "documents:delete", "documents", "documents:", ":read", "documents:read:all", "read"],
    )
    def test_a_portable_permission_that_is_not_read_or_list(self, permission):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_PORTABLE_PERMISSIONS"):
            _model(portable_permissions={MEMBER: frozenset({DOCUMENTS_READ, permission})})

    def test_list_is_a_portable_action_too(self):
        model = _model(portable_permissions={MEMBER: frozenset({"documents:list"})})

        assert model.type_for(MEMBER).portable_permissions == frozenset({"documents:list"})

    def test_a_permission_list_for_a_type_that_is_not_portable(self):
        with pytest.raises(KeycloakModelError, match="KEYCLOAK_PORTABLE_PERMISSIONS.*tenant-admin"):
            _model(portable_permissions={**PORTABLE_PERMISSIONS, TENANT_ADMIN: frozenset({"settings:read"})})


class TestLocalIssuer:
    @pytest.mark.parametrize(
        "issuer",
        [
            "http://keycloak.localhost:8180/realms/identity-stack",
            "http://keycloak-test.localhost:18180/realms/identity-stack",
            "http://KEYCLOAK.LOCALHOST/realms/identity-stack",
            "http://localhost:8180/realms/identity-stack",
            "http://127.0.0.1:8180/realms/identity-stack",
            "http://127.9.9.9/realms/identity-stack",
            "http://[::1]:8180/realms/identity-stack",
        ],
    )
    def test_loopback_and_dot_localhost_are_local(self, issuer):
        assert local_issuer(issuer) is True

    @pytest.mark.parametrize(
        "issuer",
        [
            "http://keycloak.example.test/realms/identity-stack",
            "http://localhost.example.test/realms/identity-stack",
            "http://notlocalhost/realms/identity-stack",
            "http://keycloak.localhost.example.test/realms/identity-stack",
            "http://10.0.0.5:8180/realms/identity-stack",
            "http://[2001:db8::1]/realms/identity-stack",
            "http://localhost@keycloak.example.test/realms/identity-stack",
            "keycloak.localhost",
            "",
        ],
    )
    def test_anything_else_is_not(self, issuer):
        assert local_issuer(issuer) is False


class TestSingleAudience:
    @pytest.mark.parametrize("aud", [MEMBER, [MEMBER]])
    def test_a_string_or_a_one_element_list(self, aud):
        assert single_audience(_claims(aud=aud)) == MEMBER

    @pytest.mark.parametrize(
        "aud",
        [None, "", [], [""], [MEMBER, TENANT_ADMIN], [MEMBER, MEMBER], [MEMBER, "account"], [7], 7, {MEMBER: True}],
        ids=repr,
    )
    def test_anything_else_is_no_audience(self, aud):
        assert single_audience(_claims(aud=aud)) is None


class TestTokenBinding:
    def test_the_fixture_token_is_bound(self):
        assert binding_failure(MODEL, _fixture_claims()) is None

    def test_a_one_element_audience_list_is_bound(self):
        assert binding_failure(MODEL, _claims(aud=[MEMBER])) is None

    def test_several_audiences_are_refused_even_when_each_is_a_type_client(self):
        assert binding_failure(MODEL, _claims(aud=[MEMBER, TENANT_ADMIN])) is not None

    @pytest.mark.parametrize("aud", [None, [], "account", ["account"], 7], ids=repr)
    def test_a_missing_or_unknown_audience_is_refused(self, aud):
        assert binding_failure(MODEL, _claims(aud=aud)) is not None

    @pytest.mark.parametrize(
        "azp",
        [None, "", 7, [MEMBER_APP], TENANT_ADMIN_APP, "account", MEMBER, "Member-App"],
        ids=repr,
    )
    def test_an_azp_that_is_not_the_app_client_of_the_audience_is_refused(self, azp):
        assert binding_failure(MODEL, _claims(azp=azp)) is not None

    def test_the_reason_names_no_claim_value(self):
        reason = binding_failure(MODEL, _claims(azp="some-other-client"))

        assert reason is not None
        assert "some-other-client" not in reason


class TestOrganization:
    def test_the_measured_shape_a_list_of_one_alias(self):
        assert organization(_claims(organization=[ACME])) == ACME

    @pytest.mark.parametrize(
        "claim",
        [[], [""], [ACME, GLOBEX], [ACME, ACME], [7], [None], [[ACME]], [{ACME: {}}]],
        ids=repr,
    )
    def test_any_other_list_names_no_organization(self, claim):
        assert organization(_claims(organization=claim)) is None

    @pytest.mark.parametrize("value", [{}, {"id": "42"}, None, "ignored", [GLOBEX]], ids=repr)
    def test_a_map_of_one_key_names_it_and_its_value_is_not_read(self, value):
        assert organization(_claims(organization={ACME: value})) == ACME

    @pytest.mark.parametrize("claim", [{}, {"": {}}, {ACME: {}, GLOBEX: {}}], ids=repr)
    def test_any_other_map_names_no_organization(self, claim):
        assert organization(_claims(organization=claim)) is None

    @pytest.mark.parametrize("claim", [None, ACME, "", 7, True], ids=repr)
    def test_a_missing_claim_or_any_other_type_names_no_organization(self, claim):
        """Keycloak gives a non-member a token with no claim: this is the membership control."""
        assert organization(_claims(organization=claim)) is None


class TestClientRoles:
    def test_the_roles_of_the_audience(self):
        assert client_roles(_fixture_claims(), MEMBER) == [DOCUMENTS_READ]

    @pytest.mark.parametrize(
        "resource_access",
        [
            None,
            [],
            "member",
            {},
            {TENANT_ADMIN: {"roles": [SETTINGS_MANAGE]}},
            {MEMBER: None},
            {MEMBER: [DOCUMENTS_READ]},
            {MEMBER: {}},
            {MEMBER: {"roles": None}},
            {MEMBER: {"roles": DOCUMENTS_READ}},
            {MEMBER: {"roles": {DOCUMENTS_READ: True}}},
            {MEMBER: {"roles": [DOCUMENTS_READ, 7]}},
        ],
        ids=repr,
    )
    def test_missing_or_of_the_wrong_type_is_no_roles_at_all(self, resource_access):
        assert client_roles(_claims(resource_access=resource_access), MEMBER) is None

    def test_another_clients_roles_are_not_read(self):
        claims = _claims(resource_access={MEMBER: {"roles": []}, TENANT_ADMIN: {"roles": [SETTINGS_MANAGE]}})

        assert client_roles(claims, MEMBER) == []


class TestReadGrant:
    def test_a_portable_type_grants_only_what_is_on_its_list(self):
        claims = _claims(resource_access={MEMBER: {"roles": [DOCUMENTS_WRITE, DOCUMENTS_READ, "documents:delete"]}})

        assert read_grant(MODEL, claims) == TokenGrant(
            organization=ACME, permissions=(DOCUMENTS_READ,), organization_bound=False
        )

    def test_a_member_token_with_only_documents_write_grants_nothing(self):
        claims = _claims(resource_access={MEMBER: {"roles": [DOCUMENTS_WRITE]}})

        assert read_grant(MODEL, claims) == TokenGrant(organization=ACME, permissions=(), organization_bound=False)

    def test_an_organization_bound_type_carries_its_roles_and_says_so(self):
        assert read_grant(MODEL, _tenant_admin_claims()) == TokenGrant(
            organization=ACME, permissions=(SETTINGS_MANAGE, "members:invite"), organization_bound=True
        )

    def test_each_token_names_its_own_organization(self):
        assert read_grant(MODEL, _claims(organization=[GLOBEX])).organization == GLOBEX

    @pytest.mark.parametrize("claim", [None, [], [ACME, GLOBEX], {}, ACME], ids=repr)
    def test_no_single_organization_is_no_grant(self, claim):
        assert read_grant(MODEL, _claims(organization=claim)) is None

    @pytest.mark.parametrize(
        "changes",
        [{"aud": [MEMBER, TENANT_ADMIN]}, {"aud": None}, {"azp": TENANT_ADMIN_APP}, {"azp": None}],
        ids=repr,
    )
    def test_an_unbound_token_is_no_grant(self, changes):
        """The middleware refuses these with 401 first: the reader does not rely on it."""
        assert read_grant(MODEL, _claims(**changes)) is None

    @pytest.mark.parametrize("resource_access", [None, {}, {MEMBER: {"roles": "documents:read"}}], ids=repr)
    def test_unreadable_roles_name_the_organization_and_grant_nothing(self, resource_access):
        claims = _claims(resource_access=resource_access)

        assert read_grant(MODEL, claims) == TokenGrant(organization=ACME, permissions=(), organization_bound=False)

    def test_realm_roles_and_another_providers_claims_are_not_read(self):
        claims = _claims(
            realm_access={"roles": [DOCUMENTS_READ, SETTINGS_MANAGE, "owner"]},
            dct="other-tenant",
            tenants={"other-tenant": {"roles": ["owner"], "permissions": [DOCUMENTS_WRITE]}},
            resource_access={MEMBER: {"roles": []}},
        )

        assert read_grant(MODEL, claims) == TokenGrant(organization=ACME, permissions=(), organization_bound=False)
