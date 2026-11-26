"""A live machine-user token: valid at every step the library checks, refused by the client binding.

As ``tests/integration/test_ory_end_to_end.py`` does against Hydra, the token is
a real one from the compose Zitadel: the ``identity-stack-tests`` machine user's
client-credentials token, with the project-audience scope so that its ``aud`` is
the project id the backend requires. It passes discovery, the JWKS, the
signature and the issuer through py-identity-model, the way the middleware calls
it, and the audience check too. The shipped middleware still answers 401,
because its ``client_id`` is not the SPA's (``ZitadelTokenBinding``).
"""

import base64
import json
import os

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from py_identity_model import DiscoveryDocumentRequest, DiscoveryPolicy, JwksRequest, TokenValidationConfig
from py_identity_model.aio import get_discovery_document, get_jwks, validate_token

from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import audience_rejected, binding_refusal, zitadel_config

MACHINE_VARIABLES = (
    "ZITADEL_TEST_MACHINE_CLIENT_ID",
    "ZITADEL_TEST_MACHINE_CLIENT_SECRET",
    "ZITADEL_TEST_SPA_CLIENT_ID",
)
TOKEN_REFUSED = {"detail": "Invalid or expired token"}
PROTECTED = "/api/protected"


@pytest.fixture(scope="session")
def machine(zitadel_settings) -> dict[str, str]:
    values = {name: os.environ.get(name, "") for name in MACHINE_VARIABLES}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.fail(f"{', '.join(missing)} not set: run this suite with `make zitadel-terraform-integration`")
    return values


@pytest.fixture(scope="session")
def machine_token(zitadel_settings, machine) -> str:
    project_audience = f"urn:zitadel:iam:org:project:id:{zitadel_settings.project_id}:aud"
    response = httpx.post(
        f"{zitadel_settings.base_url}/oauth/v2/token",
        data={"grant_type": "client_credentials", "scope": f"openid {project_audience}"},
        auth=(machine["ZITADEL_TEST_MACHINE_CLIENT_ID"], machine["ZITADEL_TEST_MACHINE_CLIENT_SECRET"]),
        timeout=15.0,
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _header(token: str) -> dict:
    segment = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


@pytest.fixture
def provider(zitadel_settings, machine):
    return zitadel_config(
        zitadel_settings.base_url,
        zitadel_settings.project_id,
        machine["ZITADEL_TEST_SPA_CLIENT_ID"],
        require_https=False,
    )


async def test_discovery_names_the_instance_and_its_keys(zitadel_settings, machine_token, provider):
    disco = await get_discovery_document(
        DiscoveryDocumentRequest(issuer=provider.discovery_issuer, policy=DiscoveryPolicy(require_https=False))
    )
    assert disco.is_successful, disco.error
    assert disco.issuer == zitadel_settings.base_url

    jwks = await get_jwks(JwksRequest(address=disco.jwks_uri, policy=DiscoveryPolicy(require_https=False)))
    assert jwks.is_successful, jwks.error
    assert _header(machine_token)["kid"] in {key.kid for key in jwks.keys}


async def test_the_signature_issuer_and_audience_hold_and_the_client_binding_refuses(
    zitadel_settings, machine, machine_token, provider
):
    # The library's checks, as the middleware runs them: discovery, JWKS,
    # signature and issuer, with the provider's algorithms.
    claims = await validate_token(
        jwt=machine_token,
        token_validation_config=TokenValidationConfig(
            perform_disco=True,
            algorithms=sorted(provider.algorithms),
            options={"verify_aud": False},
            require_https=False,
        ),
        issuer=provider.discovery_issuer,
    )
    assert claims["iss"] == zitadel_settings.base_url
    assert claims["client_id"] == machine["ZITADEL_TEST_MACHINE_CLIENT_ID"]
    assert claims["aud"] == [zitadel_settings.project_id]
    assert audience_rejected(claims, provider) is False

    # Only the client binding stands in the way.
    assert binding_refusal(claims, provider) is not None

    app = FastAPI()

    @app.get(PROTECTED)
    async def _protected():
        return {"reached": True}

    app.add_middleware(TokenValidationMiddleware, providers=[provider])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PROTECTED, headers={"Authorization": f"Bearer {machine_token}"})

    assert (response.status_code, response.json()) == (401, TOKEN_REFUSED)
