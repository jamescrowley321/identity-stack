"""Every tenant-scoped route is provider-neutral or refuses a Keycloak principal.

A Keycloak tenant is an organization alias: an opaque string, not a Descope
tenant id and not a canonical tenant UUID. A route that sends its tenant to
Descope's Management API or FGA store, or parses it as a UUID, would act on the
wrong thing, so a Keycloak principal must get 403 there. This file walks the
shipped application: a route that takes a tenant (``get_tenant_id`` or
``get_tenant_claims``) or is gated by a role is called as two Keycloak
principals who hold everything a Keycloak token can hold, and has to answer 403
unless it is on the provider-neutral register.
"""

import base64
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies.identity import (
    get_permission_service,
    get_provider_service,
    get_role_service,
    get_sync_status_service,
    get_tenant_service,
    get_user_service,
)
from app.dependencies.rbac import (
    canonical_memberships,
    confirmed_tenant,
    get_rbac_session,
    require_global_role,
    require_permission,
    require_role,
)
from app.dependencies.tenant import TENANTS_CLAIM_NOT_READ, get_tenant_claims, get_tenant_id
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import keycloak_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from tests.helpers.keycloak_model import (
    MEMBER_TYPE_CLIENT,
    TENANT_ADMIN_APP_CLIENT,
    TENANT_ADMIN_TYPE_CLIENT,
    model_settings,
)
from tests.unit.test_descope_backed_routes import (
    DESCOPE_BACKED_ROUTES,
    PROVIDER_NEUTRAL_ROUTES,
    _dependency_calls,
    _routes_depending_on,
    _routes_gated_by,
)

FIXTURE = Path(__file__).resolve().parents[3] / "keycloak" / "fixtures" / "access-token.json"
FIXTURE_CLAIMS = json.loads(FIXTURE.read_text())["claims"]
ISSUER = FIXTURE_CLAIMS["iss"]
ACME = "acme"
RESOURCE_ID = "33333333-3333-3333-3333-333333333333"
REFUSED_BY_THE_GUARD = {"detail": "This route serves Descope principals only"}
MEMBER = "member"
TENANT_ADMIN = "tenant-admin"

TENANT_SCOPED = _routes_depending_on(get_tenant_id) | _routes_depending_on(get_tenant_claims)
ROLE_GATED = _routes_gated_by(require_role, require_global_role)
MUST_REFUSE = (TENANT_SCOPED | ROLE_GATED | DESCOPE_BACKED_ROUTES) - set(PROVIDER_NEUTRAL_ROUTES)


async def _only_acme(_claims: dict) -> list[str]:
    """A test double of the membership lookup: the caller's one organization is ``acme``."""
    return [ACME]


def _claims(principal: str) -> dict:
    """The fixture token, not expired. The tenant admin is confirmed and holds every role of its type."""
    claims = {**FIXTURE_CLAIMS, "exp": int(time.time()) + 300}
    if principal == TENANT_ADMIN:
        claims |= {
            "aud": TENANT_ADMIN_TYPE_CLIENT,
            "azp": TENANT_ADMIN_APP_CLIENT,
            "resource_access": {
                TENANT_ADMIN_TYPE_CLIENT: {"roles": ["members:invite", "members:remove", "settings:manage"]}
            },
        }
    return claims


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _bearer(principal: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_segment({'alg': 'RS256'})}.{_segment(_claims(principal))}.sig"}


def _url(path: str) -> str:
    return (
        path.replace("{tenant_id}", ACME)
        .replace("{document_id}", RESOURCE_ID)
        .replace("{target_user_id}", "someone")
        .replace("{user_id}", RESOURCE_ID)
        .replace("{link_id}", RESOURCE_ID)
        .replace("{key_id}", "key-1")
        .replace("{provider_id}", RESOURCE_ID)
        .replace("{name}", "editor")
    )


@pytest.fixture
def stores() -> dict[str, AsyncMock]:
    """Everything a handler could reach. A refused request touches none of it."""
    return {
        "descope": AsyncMock(),
        "roles": AsyncMock(),
        "permissions": AsyncMock(),
        "users": AsyncMock(),
        "providers": AsyncMock(),
        "sync": AsyncMock(),
        "tenants": AsyncMock(),
        "session": AsyncMock(),
    }


@pytest.fixture
def client(stores) -> TestClient:
    """Every shipped route behind the shipped gateway middleware, with Keycloak as the provider."""
    limiter.reset()
    app = FastAPI()
    app.state.limiter = limiter
    app.state.descope_client = stores["descope"]
    app.state.cache_publisher = MagicMock()
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(shipped_app.router)

    async def _session():
        yield stores["session"]

    app.dependency_overrides[get_rbac_session] = _session
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_role_service] = lambda: stores["roles"]
    app.dependency_overrides[get_permission_service] = lambda: stores["permissions"]
    app.dependency_overrides[get_user_service] = lambda: stores["users"]
    app.dependency_overrides[get_provider_service] = lambda: stores["providers"]
    app.dependency_overrides[get_sync_status_service] = lambda: stores["sync"]
    app.dependency_overrides[get_tenant_service] = lambda: stores["tenants"]
    keycloak = keycloak_config(
        ISSUER,
        frozenset({MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT}),
        require_https=False,
        organization_lookup=_only_acme,
        **model_settings(MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT),
    )
    app.add_middleware(GatewayClaimsMiddleware, providers=[keycloak])
    return TestClient(app)


class TestTheWalkCoversTheApplication:
    def test_every_route_that_takes_a_tenant_is_walked_or_registered(self):
        assert TENANT_SCOPED <= MUST_REFUSE | set(PROVIDER_NEUTRAL_ROUTES)

    def test_a_route_that_sends_its_tenant_to_descope_is_not_provider_neutral(self):
        assert DESCOPE_BACKED_ROUTES & set(PROVIDER_NEUTRAL_ROUTES) == set()

    def test_the_membership_list_is_walked(self):
        assert ("GET", "/api/tenants") in MUST_REFUSE

    def test_the_registers_hold_no_stale_route(self):
        shipped = {
            (method, route.path)
            for route in shipped_app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }

        assert set(PROVIDER_NEUTRAL_ROUTES) <= shipped

    def test_the_provider_neutral_routes_are_the_permission_gated_ones(self):
        assert _routes_gated_by(require_permission) == set(PROVIDER_NEUTRAL_ROUTES)

    def test_a_provider_neutral_route_depends_on_the_resolver_and_nothing_else(self):
        """No service, no session of its own and no Descope adapter: nothing behind it can call a provider."""
        resolver_only = {get_tenant_id, canonical_memberships, confirmed_tenant, get_rbac_session}
        neutral = [
            route
            for route in shipped_app.routes
            if isinstance(route, APIRoute) and any((m, route.path) in PROVIDER_NEUTRAL_ROUTES for m in route.methods)
        ]

        assert {route.path for route in neutral} == {path for _method, path in PROVIDER_NEUTRAL_ROUTES}
        for route in neutral:
            others = [
                call
                for call in _dependency_calls(route.dependant)
                if call not in resolver_only
                and not getattr(call, "__qualname__", "").startswith("require_permission.<locals>.")
            ]
            assert others == [], route.path


@pytest.mark.parametrize("principal", [MEMBER, TENANT_ADMIN])
class TestAKeycloakPrincipalIsRefused:
    @pytest.mark.parametrize(("method", "path"), sorted(MUST_REFUSE))
    def test_on_every_route_that_is_not_registered(self, client, stores, principal, method, path):
        response = client.request(method, _url(path), headers=_bearer(principal), json={"name": "a name"})

        assert response.status_code == 403, response.text
        assert [name for name, store in stores.items() if store.mock_calls] == []

    @pytest.mark.parametrize(("method", "path"), sorted(DESCOPE_BACKED_ROUTES))
    def test_by_the_guard_on_every_descope_backed_route(self, client, principal, method, path):
        response = client.request(method, _url(path), headers=_bearer(principal), json={"name": "a name"})

        assert response.status_code == 403
        assert response.json() == REFUSED_BY_THE_GUARD

    def test_on_the_membership_list_without_asking_descope(self, client, stores, principal):
        """Its memberships are not in the Descope ``tenants`` claim, so an empty list would be a wrong answer."""
        response = client.get("/api/tenants", headers=_bearer(principal))

        assert response.status_code == 403
        assert response.json() == {"detail": TENANTS_CLAIM_NOT_READ}
        assert stores["descope"].mock_calls == []


class TestTheProviderNeutralRoutesAnswer:
    """Control for the walk: the same two principals are served where the tenant is only answered back."""

    def test_the_member_reads_documents(self, client):
        response = client.get("/api/access-checks/documents-read", headers=_bearer(MEMBER))

        assert response.status_code == 200
        assert response.json() == {"tenant_id": ACME, "permission": "documents:read"}

    def test_the_confirmed_tenant_admin_manages_settings(self, client):
        response = client.get("/api/access-checks/settings-manage", headers=_bearer(TENANT_ADMIN))

        assert response.status_code == 200
        assert response.json() == {"tenant_id": ACME, "permission": "settings:manage"}

    def test_neither_holds_the_others_permission(self, client):
        assert client.get("/api/access-checks/settings-manage", headers=_bearer(MEMBER)).status_code == 403
        assert client.get("/api/access-checks/documents-read", headers=_bearer(TENANT_ADMIN)).status_code == 403

    def test_no_store_is_touched(self, client, stores):
        client.get("/api/access-checks/documents-read", headers=_bearer(MEMBER))
        client.get("/api/access-checks/settings-manage", headers=_bearer(TENANT_ADMIN))

        assert [name for name, store in stores.items() if store.mock_calls] == []
