"""A Descope access-key session is a machine: refused where credentials, roles and members change (#699).

The session is a real one: a new owner+admin access key of the test tenant, exchanged
at Descope and deleted (``helpers/auth.get_access_key_session_token``). An owner or
admin key would otherwise mint its own successor before it expires and manage its
siblings, roles and members, so access outlived the person who made it.
"""

import pytest
from playwright.sync_api import APIRequestContext

from app.dependencies.rbac import MACHINE_PRINCIPAL_REFUSED
from tests.e2e.helpers.auth import _decode_jwt_payload, get_access_key_session_token
from tests.e2e.not_applicable import descope_credentials_missing

pytestmark = pytest.mark.skipif(
    descope_credentials_missing("DESCOPE_MANAGEMENT_KEY", "E2E_TEST_TENANT_ID"),
    reason="DESCOPE_MANAGEMENT_KEY/E2E_TEST_TENANT_ID not set",
)


@pytest.fixture(scope="module")
def access_key_token() -> str:
    return get_access_key_session_token()


@pytest.fixture
def access_key_api_context(playwright, backend_url: str, access_key_token: str):
    context = playwright.request.new_context(
        base_url=backend_url, extra_http_headers={"Authorization": f"Bearer {access_key_token}"}
    )
    yield context
    context.dispose()


def test_the_session_names_no_authentication_method(access_key_token: str):
    """What the backend tells it from a user's session by: Descope stamps ``amr`` on a user's only."""
    claims = _decode_jwt_payload(access_key_token)

    assert {"amr", "damr"}.isdisjoint(claims), sorted(claims)
    assert isinstance(claims.get("sub"), str) and claims["sub"]


@pytest.mark.parametrize("path", ["/api/keys", "/api/members", "/api/roles", "/api/permissions"])
def test_it_is_refused_as_a_machine(access_key_api_context: APIRequestContext, path: str):
    response = access_key_api_context.get(path)

    assert (response.status, response.json().get("detail")) == (403, MACHINE_PRINCIPAL_REFUSED)


def test_it_cannot_mint_its_successor(access_key_api_context: APIRequestContext):
    response = access_key_api_context.post(
        "/api/keys", data={"name": "e2e-admin-successor", "expire_time": 4_102_444_800, "role_names": ["admin"]}
    )

    assert (response.status, response.json().get("detail")) == (403, MACHINE_PRINCIPAL_REFUSED)


def test_its_grants_still_serve_its_own_roles(access_key_api_context: APIRequestContext):
    response = access_key_api_context.get("/api/roles/me")

    assert response.status == 200, response.text()
    assert set(response.json()["roles"]) >= {"owner", "admin"}
