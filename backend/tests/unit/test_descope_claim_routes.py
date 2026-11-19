"""The routes that read Descope's ``tenants`` or top-level ``roles`` claim serve Descope principals only.

``GET /api/tenants`` and ``GET`` and ``POST /api/tenants/{tenant_id}/resources``
read the ``tenants`` claim (``get_tenant_claims``), and
``POST /api/tenants`` the top-level ``roles`` claim (``require_admin_role``). A
Keycloak, Ory or Zitadel principal is authorized from the canonical model, so
those claims on its token are refused with 403, whatever they say: a provider
mapper that put them there would otherwise grant membership of any tenant, or
an admin role (identity-stack#596). ``GET /api/roles/me`` reads no claim: it
answers the tenant resolver's grants, so a canonical principal gets its canonical
role there, never the forged one. A Descope principal is served as before.

Requests go through the shipped gateway middleware and the shipped routers.
The canonical store is a stub that makes every canonical principal a member
of the tenant the forged claims name, so the refusal is the guard's and not
the resolver's.
"""

import base64
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Ok
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import rbac
from app.dependencies.identity import get_tenant_service
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant import DESCOPE_CLAIMS_ONLY
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    ProviderTokenConfig,
    descope_config,
    keycloak_config,
    ory_config,
    zitadel_config,
)
from app.models.database import get_async_session

DESCOPE_PROJECT = "P2descopeclaimroutes"
TENANT = "11111111-1111-4111-8111-111111111111"
SUB = "subject-1"

KEYCLOAK = keycloak_config("https://keycloak.example.test/realms/r", "identity-stack-api", "identity-stack-spa")
ORY = ory_config("https://ory.example.test", "https://identity-stack-api")
ZITADEL = zitadel_config("https://zitadel.example.test", "project-1", "spa-1")
DESCOPE = descope_config(DESCOPE_PROJECT)
CANONICAL = {"keycloak": KEYCLOAK, "ory": ORY, "zitadel": ZITADEL}

# What each canonical provider's token needs to authenticate, beyond the forged claims.
_BOUND = {
    "keycloak": {"aud": "identity-stack-api", "azp": "identity-stack-spa", "typ": "Bearer"},
    "ory": {"aud": "https://identity-stack-api"},
    "zitadel": {"aud": ["project-1"], "client_id": "spa-1"},
}
FORGED = {
    "tenants": {TENANT: {"roles": ["owner", "admin"], "permissions": ["settings:manage"]}},
    "roles": ["owner", "admin"],
    "dct": TENANT,
}
REFUSED = {"detail": DESCOPE_CLAIMS_ONLY}
ROUTES = [
    ("GET", "/api/tenants", None, REFUSED),
    ("GET", f"/api/tenants/{TENANT}/resources", None, REFUSED),
    ("POST", f"/api/tenants/{TENANT}/resources", {"name": "forged"}, REFUSED),
    ("POST", "/api/tenants", {"name": "Forged"}, REFUSED),
]


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    """An unsigned JWT: gateway mode reads the payload the gateway already verified."""
    return f"{_segment({'alg': 'RS256'})}.{_segment({'exp': int(time.time()) + 300, **claims})}.sig"


def _canonical_token(name: str) -> str:
    provider = CANONICAL[name]
    (issuer,) = provider.accepted_issuers
    return _jwt({"iss": issuer, "sub": SUB, **_BOUND[name], **FORGED})


def _descope_token(**claims) -> str:
    return _jwt({"iss": f"https://api.descope.com/{DESCOPE_PROJECT}", "sub": SUB, "dct": TENANT, **claims})


class _CanonicalStore:
    """Every canonical principal is an active ``owner`` of ``TENANT``: the resolver never refuses them."""

    async def resolve(self, *, provider: str, sub: str):
        return Ok(
            {
                "user": {"id": "canonical-user-1", "status": "active"},
                "roles": [{"tenant_id": TENANT, "role_name": "owner", "permissions": ["settings:manage"]}],
            }
        )


@pytest.fixture
def services(monkeypatch):
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset(CANONICAL))
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: _CanonicalStore())
    tenant_service = MagicMock()
    tenant_service.create_tenant = AsyncMock(return_value=Ok({"id": str(uuid.uuid4()), "name": "Forged"}))
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=list)))
    session.commit = AsyncMock()
    session.add = MagicMock()
    return tenant_service, session


@pytest.fixture
def client(services) -> TestClient:
    tenant_service, session = services
    app = FastAPI()
    app.state.descope_client = None
    app.include_router(shipped_app.router)

    async def _rbac_session():
        yield object()

    async def _session():
        yield session

    app.dependency_overrides[get_rbac_session] = _rbac_session
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_tenant_service] = lambda: tenant_service
    providers: list[ProviderTokenConfig] = [DESCOPE, *CANONICAL.values()]
    app.add_middleware(GatewayClaimsMiddleware, providers=providers)
    return TestClient(app)


def _call(client: TestClient, method: str, path: str, body: dict | None, token: str):
    headers = {"Authorization": f"Bearer {token}", TENANT_SELECTOR_HEADER: TENANT}
    return client.request(method, path, headers=headers, json=body)


@pytest.mark.parametrize("provider", sorted(CANONICAL))
@pytest.mark.parametrize(("method", "path", "body", "refusal"), ROUTES, ids=[f"{m} {p}" for m, p, _, _ in ROUTES])
def test_a_canonical_principal_with_forged_descope_claims_is_refused(
    client, services, provider, method, path, body, refusal
):
    tenant_service, session = services

    response = _call(client, method, path, body, _canonical_token(provider))

    assert (response.status_code, response.json()) == (403, refusal)
    tenant_service.create_tenant.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.parametrize("provider", sorted(CANONICAL))
def test_a_canonical_principal_reads_its_canonical_roles_not_the_forged_ones(client, provider):
    response = _call(client, "GET", "/api/roles/me", None, _canonical_token(provider))

    assert (response.status_code, response.json()) == (
        200,
        {"tenant_id": TENANT, "roles": ["owner"], "permissions": ["settings:manage"]},
    )


class TestADescopePrincipalIsServedAsBefore:
    def test_its_tenants_are_listed_from_the_claim(self, client):
        token = _descope_token(tenants={TENANT: {"roles": ["admin"], "permissions": ["p"]}})

        response = _call(client, "GET", "/api/tenants", None, token)

        assert response.status_code == 200
        assert response.json() == {
            "tenants": [{"id": TENANT, "name": TENANT, "roles": ["admin"], "permissions": ["p"]}]
        }

    def test_a_member_reads_the_tenants_resources(self, client):
        response = _call(client, "GET", f"/api/tenants/{TENANT}/resources", None, _descope_token(tenants={TENANT: {}}))

        assert (response.status_code, response.json()) == (200, {"resources": []})

    def test_a_non_member_is_refused_by_the_route(self, client):
        response = _call(client, "GET", f"/api/tenants/{TENANT}/resources", None, _descope_token(tenants={}))

        assert (response.status_code, response.json()) == (403, {"detail": "Not a member of this tenant"})

    def test_its_roles_in_the_current_tenant(self, client):
        token = _descope_token(tenants={TENANT: {"roles": ["admin"], "permissions": ["p"]}})

        response = _call(client, "GET", "/api/roles/me", None, token)

        assert response.json() == {"tenant_id": TENANT, "roles": ["admin"], "permissions": ["p"]}

    def test_a_project_admin_creates_a_tenant(self, client, services):
        tenant_service, _ = services

        response = _call(client, "POST", "/api/tenants", {"name": "Acme"}, _descope_token(roles=["admin"]))

        assert response.status_code == 200
        tenant_service.create_tenant.assert_awaited_once_with(name="Acme", domains=None)

    def test_a_caller_without_the_project_role_is_refused(self, client, services):
        tenant_service, _ = services

        response = _call(client, "POST", "/api/tenants", {"name": "Acme"}, _descope_token(roles=["member"]))

        assert (response.status_code, response.json()) == (403, {"detail": "Admin or owner role required"})
        tenant_service.create_tenant.assert_not_awaited()
