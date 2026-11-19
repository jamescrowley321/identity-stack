"""The Keycloak provider row the seed writes, the tenants it links to organization aliases and the users it links."""

import uuid

import pytest

from app.middleware.providers import keycloak_config, keycloak_organization_alias
from app.models.identity.provider import ProviderType
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus
from scripts.seed_keycloak import (
    PROVIDER_NAME,
    KeycloakAccount,
    OrganizationLinkConflict,
    OrganizationsInputError,
    UserLinkConflict,
    UsersInputError,
    keycloak_provider,
    link_tenant,
    linked_user,
    parse_organizations,
    parse_users,
)
from tests.helpers.keycloak_model import API_AUDIENCE, SPA_CLIENT_ID

ISSUER = "http://keycloak.localhost:8180/realms/identity-stack"
KEYCLOAK_ROW = uuid.UUID("0c0c0000-0000-4000-8000-00000000000c")
OTHER_ROW = uuid.UUID("0d0d0000-0000-4000-8000-00000000000d")


def test_the_row_is_named_as_the_middleware_names_keycloak_principals():
    provider = keycloak_config(ISSUER, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)

    assert keycloak_provider(issuer_url=ISSUER).name == PROVIDER_NAME == provider.name.lower()


def test_the_row_is_an_oidc_provider_on_the_realms_issuer():
    row = keycloak_provider(issuer_url=f"{ISSUER}/")

    assert (row.type, row.issuer_url, row.base_url, row.capabilities) == (
        ProviderType.oidc,
        ISSUER,
        ISSUER,
        ["sso"],
    )


class TestTheOrganizationsInput:
    def test_it_is_tenant_name_to_alias(self):
        assert parse_organizations({"acme": "acme", "globex": "globex"}) == {"acme": "acme", "globex": "globex"}

    @pytest.mark.parametrize("raw", [{}, [], "acme", None, {"acme": ""}, {"acme": 7}, {"acme": ["acme"]}])
    def test_anything_else_is_refused(self, raw):
        with pytest.raises(OrganizationsInputError):
            parse_organizations(raw)


class TestLinkingATenant:
    def test_a_missing_tenant_is_created_with_the_alias_as_its_organization(self):
        tenant = link_tenant(None, "acme", "acme", KEYCLOAK_ROW)

        assert (tenant.name, tenant.external_org_id, tenant.external_org_provider_id, tenant.status) == (
            "acme",
            "acme",
            KEYCLOAK_ROW,
            TenantStatus.active,
        )

    def test_the_link_is_what_first_login_reads_from_the_token(self):
        """The recorded token's ``organization`` claim names the alias the seed links."""
        tenant = link_tenant(None, "acme", "acme", KEYCLOAK_ROW)

        assert tenant.external_org_id == keycloak_organization_alias({"organization": ["acme"]})

    def test_an_unlinked_tenant_gets_the_link(self):
        existing = Tenant(name="acme", status=TenantStatus.active)

        tenant = link_tenant(existing, "acme", "acme", KEYCLOAK_ROW)

        assert tenant is existing
        assert (tenant.external_org_id, tenant.external_org_provider_id) == ("acme", KEYCLOAK_ROW)

    def test_a_tenant_already_linked_to_the_alias_is_left_as_it_is(self):
        existing = Tenant(
            name="acme", external_org_id="acme", external_org_provider_id=KEYCLOAK_ROW, status=TenantStatus.active
        )

        assert link_tenant(existing, "acme", "acme", KEYCLOAK_ROW) is existing

    @pytest.mark.parametrize(
        ("organization", "provider"), [("globex", KEYCLOAK_ROW), ("acme", OTHER_ROW), ("acme", None)]
    )
    def test_a_tenant_linked_elsewhere_is_refused(self, organization, provider):
        existing = Tenant(
            name="acme", external_org_id=organization, external_org_provider_id=provider, status=TenantStatus.active
        )

        with pytest.raises(OrganizationLinkConflict, match="acme"):
            link_tenant(existing, "acme", "acme", KEYCLOAK_ROW)

    def test_a_suspended_tenant_is_refused(self):
        existing = Tenant(name="acme", status=TenantStatus.suspended)

        with pytest.raises(OrganizationLinkConflict, match="suspended"):
            link_tenant(existing, "acme", "acme", KEYCLOAK_ROW)

    def test_an_unlinked_tenant_with_domains_is_refused(self):
        existing = Tenant(name="acme", status=TenantStatus.active, domains=["acme.test"])

        with pytest.raises(OrganizationLinkConflict, match="domains"):
            link_tenant(existing, "acme", "acme", KEYCLOAK_ROW)

    def test_a_created_tenant_has_no_domains(self):
        assert not link_tenant(None, "acme", "acme", KEYCLOAK_ROW).domains


BOB_SUB = "3cb3c52a-0000-4000-8000-000000000b0b"
BOB = {"external_sub": BOB_SUB, "email": "bob@globex.test", "given_name": "Bob", "family_name": "Two-Orgs"}
BOB_ACCOUNT = KeycloakAccount(username="bob", **BOB)


class TestTheUsersInput:
    def test_it_is_username_to_account(self):
        assert parse_users({"bob": BOB}) == [BOB_ACCOUNT]

    @pytest.mark.parametrize(
        "raw",
        [
            {},
            [],
            None,
            {"bob": "bob"},
            {"bob": {**BOB, "email": ""}},
            {"bob": {**BOB, "given_name": 7}},
            {"bob": {key: value for key, value in BOB.items() if key != "family_name"}},
            {"bob": {**BOB, "roles": {"acme": "admin"}}},
            {"bob": BOB, "bob2": BOB},
            {"bob": BOB, "bob2": {**BOB, "external_sub": "another-account"}},
        ],
        ids=[
            "empty",
            "list",
            "null",
            "not-an-object",
            "empty-field",
            "non-string-field",
            "missing-field",
            "extra-field",
            "same-account-twice",
            "same-email-twice",
        ],
    )
    def test_anything_else_is_refused(self, raw):
        with pytest.raises(UsersInputError):
            parse_users(raw)


class TestLinkingAUser:
    def test_an_account_with_no_link_and_no_user_gets_a_new_active_user(self):
        user = linked_user(BOB_ACCOUNT, None, None)

        assert (user.email, user.user_name, user.given_name, user.family_name, user.status) == (
            "bob@globex.test",
            "bob",
            "Bob",
            "Two-Orgs",
            UserStatus.active,
        )

    def test_a_linked_active_user_is_left_as_it_is(self):
        user = User(email="bob@renamed.test", user_name="bob", status=UserStatus.active)
        link = IdPLink(user_id=user.id, provider_id=KEYCLOAK_ROW, external_sub=BOB_SUB)

        assert linked_user(BOB_ACCOUNT, link, user) is user

    def test_a_linked_user_who_is_not_active_is_left_to_reconciliation(self):
        user = User(email="bob@globex.test", user_name="bob", status=UserStatus.inactive)
        link = IdPLink(user_id=user.id, provider_id=KEYCLOAK_ROW, external_sub=BOB_SUB)

        assert (linked_user(BOB_ACCOUNT, link, user), user.status) == (user, UserStatus.inactive)

    def test_a_link_naming_no_user_is_refused(self):
        link = IdPLink(user_id=uuid.uuid4(), provider_id=KEYCLOAK_ROW, external_sub=BOB_SUB)

        with pytest.raises(UserLinkConflict, match="names no user"):
            linked_user(BOB_ACCOUNT, link, None)

    def test_a_user_holding_the_email_without_the_link_is_refused(self):
        holder = User(email="bob@globex.test", user_name="someone", status=UserStatus.active)

        with pytest.raises(UserLinkConflict, match="not linked to Keycloak user"):
            linked_user(BOB_ACCOUNT, None, holder)
