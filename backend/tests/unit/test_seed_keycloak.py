"""The Keycloak provider row the seed writes, and the tenants it links to organization aliases."""

import uuid

import pytest

from app.middleware.providers import keycloak_config, keycloak_organization_alias
from app.models.identity.provider import ProviderType
from app.models.identity.tenant import Tenant, TenantStatus
from scripts.seed_keycloak import (
    PROVIDER_NAME,
    OrganizationLinkConflict,
    OrganizationsInputError,
    keycloak_provider,
    link_tenant,
    parse_organizations,
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
