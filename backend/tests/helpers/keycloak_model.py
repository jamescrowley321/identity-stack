"""The fixture realm's clients, as a backend that accepts its tokens is configured with them.

``keycloak/fixtures/README.md`` describes the realm: the SPA's public client,
the API client whose id is the SPA access token's audience, and the admin
client the sync adapter writes with. A test gets the settings as
``keycloak_config`` arguments, as ``build_provider_configs`` arguments or as
environment variables.
"""

import os

from tests.helpers.keycloak_target import keycloak_test_target

API_AUDIENCE = "identity-stack-api"
SPA_CLIENT_ID = "identity-stack-spa"
# The scope the SPA asks for (infra/keycloak's frontend_environment): the plain
# organization scope, so Keycloak asks a member of several organizations which one.
SPA_SCOPE = "openid profile email organization"
OTHER_CLIENT_ID = "identity-stack-other"
# The realm's admin client, which the Keycloak sync adapter writes with.
ADMIN_CLIENT_ID = "identity-stack-admin"
ADMIN_CLIENT_SECRET = keycloak_test_target(os.environ).admin_client_secret

# Every variable the Keycloak registration reads, for a test that clears the environment.
KEYCLOAK_VARIABLES = (
    "KEYCLOAK_ISSUER_URL",
    "KEYCLOAK_AUDIENCE",
    "KEYCLOAK_CLIENT_ID",
    "KEYCLOAK_REQUIRE_HTTPS",
    "KEYCLOAK_POST_LOGOUT_REDIRECT_URI",
)


def client_settings() -> dict:
    """``keycloak_config``'s audience and client id for the fixture realm, as positional-free keywords."""
    return {"audience": API_AUDIENCE, "client_id": SPA_CLIENT_ID}


def builder_settings() -> dict:
    """The same settings under ``build_provider_configs``'s argument names."""
    return {"keycloak_audience": API_AUDIENCE, "keycloak_client_id": SPA_CLIENT_ID}


def set_client_environment(monkeypatch) -> None:
    """The same settings as the environment variables a deployment sets."""
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", API_AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", SPA_CLIENT_ID)
