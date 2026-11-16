"""The routes that hand a tenant id or a subject to Descope serve Descope principals only.

``get_tenant_id`` gives a principal from a canonical provider a canonical tenant
id (conventions §18). The routes below pass their tenant id and the caller's
``sub`` to Descope's Management API or its FGA store, where a canonical id names
nothing, so ``require_descope_principal`` refuses every other provider's
principal on them before anything is read.

Every tenant-scoped route is in exactly one of the two registers here. A new
route that depends on ``get_tenant_id`` fails this file until it is put in one.
"""

import base64
import json
import time
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Ok
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies import rbac
from app.dependencies.descope import require_descope_principal
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant import get_tenant_id
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import ProviderTokenConfig, TenantClaims, descope_config, ory_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session

DESCOPE_PROJECT = "P123"
DESCOPE_ISSUER = f"https://api.descope.com/{DESCOPE_PROJECT}"
ORY_ISSUER = "https://ory.example.test"
ORY_AUDIENCE = "https://identity-stack-api"
FAKE_ISSUER = "https://fake.example.test"

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
RESOURCE_ID = "33333333-3333-3333-3333-333333333333"
ORY_SUB = "ory-subject-1"

REFUSED = {"detail": "No tenant context — select a tenant first"}

# (method, path) of every route that hands the tenant or the subject to Descope.
DESCOPE_BACKED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/documents"),
        ("GET", "/api/documents"),
        ("GET", "/api/documents/{document_id}"),
        ("PUT", "/api/documents/{document_id}"),
        ("DELETE", "/api/documents/{document_id}"),
        ("POST", "/api/documents/{document_id}/share"),
        ("DELETE", "/api/documents/{document_id}/share/{target_user_id}"),
        ("GET", "/api/members"),
        ("POST", "/api/members/invite"),
        ("POST", "/api/members/{user_id}/deactivate"),
        ("POST", "/api/members/{user_id}/activate"),
        ("DELETE", "/api/members/{user_id}"),
        ("POST", "/api/keys"),
        ("GET", "/api/keys"),
        ("GET", "/api/keys/{key_id}"),
        ("POST", "/api/keys/{key_id}/deactivate"),
        ("POST", "/api/keys/{key_id}/activate"),
        ("DELETE", "/api/keys/{key_id}"),
        ("POST", "/api/fga/relations"),
        ("DELETE", "/api/fga/relations"),
        ("GET", "/api/fga/relations"),
        ("POST", "/api/fga/check"),
        ("GET", "/api/tenants/current"),
        ("GET", "/api/tenants/current/settings"),
        ("PATCH", "/api/tenants/current/settings"),
        ("GET", "/api/roles/me"),
        # ``RoleService`` mirrors every assignment to Descope (``DescopeSyncAdapter``).
        ("POST", "/api/roles/assign"),
        ("POST", "/api/roles/remove"),
    }
)

# (method, path) -> why a principal from any provider may use the route's tenant.
# A route belongs here only when nothing behind it, its service's sync adapter
# included, hands the tenant or a user id to Descope. None does yet.
PROVIDER_NEUTRAL_ROUTES: dict[tuple[str, str], str] = {}


def _dependency_calls(dependant: Dependant) -> Iterator[Callable]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from _dependency_calls(sub)


def _routes_depending_on(dependency: Callable) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in shipped_app.routes
        if isinstance(route, APIRoute) and dependency in _dependency_calls(route.dependant)
        for method in route.methods
    }


def _fake_tenant_claims(claims: dict) -> TenantClaims:
    return TenantClaims(tenant_id=claims.get("org"), roles=list(claims.get("org_roles", [])), permissions=[])


FAKE_PROVIDER = ProviderTokenConfig(
    name="Fake",
    accepted_issuers=frozenset({FAKE_ISSUER}),
    disco_address=f"{FAKE_ISSUER}/.well-known/openid-configuration",
    tenant_claims=_fake_tenant_claims,
)


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _bearer(claims: dict) -> dict[str, str]:
    """An unsigned JWT: gateway mode reads the payload the gateway already verified."""
    token = f"{_segment({'alg': 'RS256'})}.{_segment({'exp': int(time.time()) + 300, **claims})}.sig"
    return {"Authorization": f"Bearer {token}"}


ORY_OWNER = {"iss": ORY_ISSUER, "aud": ORY_AUDIENCE, "sub": ORY_SUB}
DESCOPE_MEMBER = {
    "iss": DESCOPE_ISSUER,
    "sub": "descope-user",
    "dct": "t1",
    "tenants": {"t1": {"roles": ["member"], "permissions": ["documents.read"]}},
}
DESCOPE_ADMIN = {
    "iss": DESCOPE_ISSUER,
    "sub": "descope-admin",
    "dct": "t1",
    "tenants": {"t1": {"roles": ["admin"], "permissions": []}},
}
FAKE_ADMIN = {"iss": FAKE_ISSUER, "sub": "fake-user", "org": "org-1", "org_roles": ["owner"]}


class _CanonicalStore:
    """Stands in for IdentityResolutionService: the Ory caller owns one tenant; lookups are recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, *, provider: str, sub: str):
        self.calls.append((provider, sub))
        return Ok(
            {
                "user": {"id": "canonical-user-1", "status": "active"},
                "roles": [{"tenant_id": TENANT_A, "role_name": "owner", "permissions": ["documents.read"]}],
            }
        )


@pytest.fixture
def store(monkeypatch) -> _CanonicalStore:
    canonical = _CanonicalStore()
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: canonical)
    return canonical


@pytest.fixture
def descope_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def client(store, descope_client) -> TestClient:
    """Every shipped route behind the shipped gateway middleware; only the stores are stubs."""
    limiter.reset()
    app = FastAPI()
    app.state.limiter = limiter
    app.state.descope_client = descope_client
    app.state.cache_publisher = MagicMock()
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(shipped_app.router)

    async def _session():
        yield MagicMock()

    app.dependency_overrides[get_rbac_session] = _session
    app.dependency_overrides[get_async_session] = _session
    app.add_middleware(
        GatewayClaimsMiddleware,
        providers=[descope_config(DESCOPE_PROJECT), ory_config(ORY_ISSUER, ORY_AUDIENCE), FAKE_PROVIDER],
    )
    return TestClient(app)


def _url(path: str) -> str:
    return (
        path.replace("{document_id}", RESOURCE_ID)
        .replace("{target_user_id}", "someone")
        .replace("{user_id}", "someone")
        .replace("{key_id}", "key-1")
    )


class TestTheRegisters:
    def test_the_guard_is_on_exactly_the_descope_backed_routes(self):
        assert _routes_depending_on(require_descope_principal) == DESCOPE_BACKED_ROUTES

    def test_every_route_that_takes_a_tenant_is_in_one_register(self):
        assert _routes_depending_on(get_tenant_id) == DESCOPE_BACKED_ROUTES | set(PROVIDER_NEUTRAL_ROUTES)

    def test_no_route_is_in_both(self):
        assert DESCOPE_BACKED_ROUTES & set(PROVIDER_NEUTRAL_ROUTES) == set()


class TestACanonicalProviderPrincipalIsRefused:
    """The Ory caller is an active owner of one tenant, so only the guard stands in the way."""

    @pytest.mark.parametrize(("method", "path"), sorted(DESCOPE_BACKED_ROUTES))
    def test_on_every_descope_backed_route(self, client, store, descope_client, method, path):
        response = client.request(method, _url(path), headers=_bearer(ORY_OWNER))

        assert response.status_code == 403
        assert response.json() == REFUSED
        # Refused before Descope is called and before the canonical model is read.
        assert descope_client.mock_calls == []
        assert store.calls == []

    def test_naming_its_tenant_changes_nothing(self, client, descope_client):
        response = client.post(
            "/api/documents",
            json={"title": "a title", "content": "some content"},
            headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_A},
        )

        assert response.status_code == 403
        assert response.json() == REFUSED
        assert descope_client.mock_calls == []


class TestAnotherClaimsProviderIsRefusedToo:
    def test_its_own_tenant_is_not_a_descope_tenant(self, client, descope_client):
        """The fake provider names a tenant and an owner role with its own extractor."""
        members = client.get("/api/members", headers=_bearer(FAKE_ADMIN))
        documents = client.get("/api/documents", headers=_bearer(FAKE_ADMIN))

        assert members.status_code == 403
        assert members.json() == REFUSED
        assert documents.status_code == 403
        assert documents.json() == REFUSED
        assert descope_client.mock_calls == []


class TestADescopePrincipalIsServed:
    def test_its_roles_come_back_from_its_own_claims(self, client):
        response = client.get("/api/roles/me", headers=_bearer(DESCOPE_MEMBER))

        assert response.status_code == 200
        assert response.json() == {"tenant_id": "t1", "roles": ["member"], "permissions": ["documents.read"]}

    def test_its_tenant_is_loaded_from_descope(self, client, descope_client):
        descope_client.load_tenant.return_value = {"id": "t1", "name": "Tenant One"}

        response = client.get("/api/tenants/current", headers=_bearer(DESCOPE_MEMBER))

        assert response.status_code == 200
        assert response.json()["tenant_id"] == "t1"
        descope_client.load_tenant.assert_awaited_once_with("t1")


class TestRoleAssignmentIsDescopeBacked:
    """``RoleService`` sends each assignment to Descope, so the two routes take Descope principals only."""

    @pytest.mark.parametrize("path", ["/api/roles/assign", "/api/roles/remove"])
    def test_an_ory_owner_of_the_tenant_is_refused_before_anything_is_read(self, client, store, descope_client, path):
        """The body names the caller's own tenant and a well-formed user: only the guard refuses it."""
        body = {"user_id": RESOURCE_ID, "tenant_id": TENANT_A, "role_names": ["member"]}

        response = client.post(path, json=body, headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_A})

        assert response.status_code == 403
        assert response.json() == REFUSED
        assert descope_client.mock_calls == []
        assert store.calls == []

    @pytest.mark.parametrize("path", ["/api/roles/assign", "/api/roles/remove"])
    def test_a_descope_admin_still_reaches_the_handler(self, client, path):
        """The handler's own check answers: it compared the body's tenant with the token's."""
        body = {"user_id": RESOURCE_ID, "tenant_id": TENANT_B, "role_names": ["member"]}

        response = client.post(path, json=body, headers=_bearer(DESCOPE_ADMIN))

        assert response.status_code == 403
        assert response.json() == {"detail": "Cannot manage roles for a different tenant"}
