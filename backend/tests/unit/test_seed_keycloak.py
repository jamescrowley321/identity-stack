"""The Keycloak provider row the seed writes, which first-login provisioning finds by name."""

from app.middleware.providers import keycloak_config
from app.models.identity.provider import ProviderType
from scripts.seed_keycloak import PROVIDER_NAME, keycloak_provider
from tests.helpers.keycloak_model import MEMBER_TYPE_CLIENT, model_settings

ISSUER = "http://keycloak.localhost:8180/realms/identity-stack"


def test_the_row_is_named_as_the_middleware_names_keycloak_principals():
    provider = keycloak_config(
        ISSUER, frozenset({MEMBER_TYPE_CLIENT}), require_https=False, **model_settings(MEMBER_TYPE_CLIENT)
    )

    assert keycloak_provider(issuer_url=ISSUER).name == PROVIDER_NAME == provider.name.lower()


def test_the_row_is_an_oidc_provider_on_the_realms_issuer():
    row = keycloak_provider(issuer_url=f"{ISSUER}/")

    assert (row.type, row.issuer_url, row.base_url, row.capabilities) == (
        ProviderType.oidc,
        ISSUER,
        ISSUER,
        ["sso"],
    )
