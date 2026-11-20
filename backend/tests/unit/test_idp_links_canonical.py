"""Only an operator manages IdP links (#722, conventions §18).

A link signs its login in as the person, with every grant they hold in every
tenant. A tenant's owner or admin making one for a user of their tenant would take
over that user's other tenants (and an operator's, or an owner's, grants), so the
routes are gated by ``require_global_role("operator")``. A Descope principal's
``dct`` spelled as a canonical tenant's UUID grants it nothing here either.

Requests go through the shipped gateway middleware and the shipped routers. Only
the canonical identity store, the user rows and the link service are stubs.
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
from slowapi.errors import RateLimitExceeded

from app.dependencies import rbac
from app.dependencies.identity import get_idp_link_service
from app.dependencies.rbac import NOT_A_CANONICAL_PRINCIPAL, get_rbac_session
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.errors.identity import IdentityErrorRaised
from app.errors.problem_detail import identity_error_raised_handler
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import descope_config, keycloak_config, ory_config, zitadel_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from app.routers import idp_links
from app.services.idp_link import IdPLinkService

DESCOPE_PROJECT = "P2idplinks"
SUB = "subject-1"

ACME = "11111111-1111-4111-8111-111111111111"
GLOBEX = "22222222-2222-4222-8222-222222222222"
# A tenant the caller holds no membership in.
INITECH = "33333333-3333-4333-8333-333333333333"

# Targets: a member of Acme only, a member of Acme and Globex, and Acme's owner.
ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOB = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OWEN = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
USERS = {ALICE, BOB, OWEN}
TARGETS = {"single-tenant member": ALICE, "multi-tenant member": BOB, "owner": OWEN}
# A user the canonical model does not hold.
NOBODY = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")

PROVIDER_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
LINK_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
LINK = {"id": str(LINK_ID), "user_id": str(ALICE), "provider_id": str(PROVIDER_ID), "external_sub": "ext-1"}

KEYCLOAK = keycloak_config("https://keycloak.example.test/realms/r", "identity-stack-api", "identity-stack-spa")
ORY = ory_config("https://ory.example.test", "https://identity-stack-api")
ZITADEL = zitadel_config("https://zitadel.example.test", "project-1", "spa-1")
DESCOPE = descope_config(DESCOPE_PROJECT)
CANONICAL = {"keycloak": KEYCLOAK, "ory": ORY, "zitadel": ZITADEL}

# What each canonical provider's token needs to authenticate.
_BOUND = {
    "keycloak": {"aud": "identity-stack-api", "azp": "identity-stack-spa", "typ": "Bearer"},
    "ory": {"aud": "https://identity-stack-api"},
    "zitadel": {"aud": ["project-1"], "client_id": "spa-1"},
}

NOT_FOUND = {"status": 404, "detail": "User not found"}


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    """An unsigned JWT: gateway mode reads the payload the gateway already verified."""
    return f"{_segment({'alg': 'RS256'})}.{_segment({'exp': int(time.time()) + 300, **claims})}.sig"


def _canonical_headers(provider: str, tenant: str | None = None) -> dict[str, str]:
    (issuer,) = CANONICAL[provider].accepted_issuers
    headers = {"Authorization": f"Bearer {_jwt({'iss': issuer, 'sub': SUB, **_BOUND[provider]})}"}
    if tenant is not None:
        headers[TENANT_SELECTOR_HEADER] = tenant
    return headers


def _descope_headers(role: str) -> dict[str, str]:
    """A Descope principal whose ``dct`` is spelled as Acme's canonical UUID, holding ``role`` there."""
    claims = {
        "iss": f"https://api.descope.com/{DESCOPE_PROJECT}",
        "sub": SUB,
        "dct": ACME,
        "tenants": {ACME: {"roles": [role], "permissions": []}},
    }
    return {"Authorization": f"Bearer {_jwt(claims)}"}


def _grant(tenant_id: str, role: str) -> dict:
    return {"tenant_id": tenant_id, "role_name": role, "permissions": []}


OPERATOR = [_grant(ACME, "operator")]
# What a tenant's owner or admin holds: none of it is operator.
NOT_OPERATORS = {
    "admin": [_grant(ACME, "admin")],
    "owner": [_grant(ACME, "owner")],
    "admin of two tenants": [_grant(ACME, "admin"), _grant(GLOBEX, "admin")],
}


class _CanonicalStore:
    """Stands in for IdentityResolutionService: the caller holds ``roles``."""

    def __init__(self) -> None:
        self.roles: list[dict] = OPERATOR

    async def resolve(self, *, provider: str, sub: str):
        return Ok({"user": {"id": "canonical-user-1", "status": "active"}, "roles": self.roles})


class _Users:
    """Stands in for UserRepository: holds ``USERS``; lookups are recorded."""

    def __init__(self) -> None:
        self.lookups: list[uuid.UUID] = []

    def __call__(self, _session) -> "_Users":
        return self

    async def get(self, user_id: uuid.UUID) -> object | None:
        self.lookups.append(user_id)
        return object() if user_id in USERS else None


@pytest.fixture
def store(monkeypatch) -> _CanonicalStore:
    canonical = _CanonicalStore()
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset(CANONICAL))
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: canonical)
    return canonical


@pytest.fixture
def users(monkeypatch) -> _Users:
    rows = _Users()
    monkeypatch.setattr(idp_links, "UserRepository", rows)
    return rows


@pytest.fixture
def links() -> AsyncMock:
    service = AsyncMock(spec=IdPLinkService)
    service.get_user_idp_links.return_value = Ok([LINK])
    service.create_idp_link.return_value = Ok(LINK)
    service.delete_idp_link.return_value = Ok({"status": "deleted", "link_id": str(LINK_ID)})
    return service


@pytest.fixture
def client(store, users, links) -> TestClient:
    limiter.reset()
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(IdentityErrorRaised, identity_error_raised_handler)
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(shipped_app.router)

    async def _session():
        yield MagicMock()

    app.dependency_overrides[get_rbac_session] = _session
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_idp_link_service] = lambda: links
    app.add_middleware(GatewayClaimsMiddleware, providers=[DESCOPE, *CANONICAL.values()])
    return TestClient(app)


def _list(client: TestClient, user: uuid.UUID, headers: dict[str, str]):
    return client.get(f"/api/users/{user}/idp-links", headers=headers)


def _create(client: TestClient, user: uuid.UUID, headers: dict[str, str]):
    body = {"provider_id": str(PROVIDER_ID), "external_sub": "ext-1"}
    return client.post(f"/api/users/{user}/idp-links", headers=headers, json=body)


def _delete(client: TestClient, user: uuid.UUID, headers: dict[str, str]):
    return client.delete(f"/api/users/{user}/idp-links/{LINK_ID}", headers=headers)


ROUTES = {"list": _list, "create": _create, "delete": _delete}


@pytest.mark.parametrize("provider", sorted(CANONICAL))
class TestAnOperator:
    @pytest.mark.parametrize("target", sorted(TARGETS))
    def test_lists_a_users_links(self, client, users, links, provider, target):
        response = _list(client, TARGETS[target], _canonical_headers(provider))

        assert (response.status_code, response.json()) == (200, {"idp_links": [LINK]})
        assert users.lookups == [TARGETS[target]]
        links.get_user_idp_links.assert_awaited_once_with(user_id=TARGETS[target])

    @pytest.mark.parametrize("target", sorted(TARGETS))
    def test_adds_a_link(self, client, users, links, provider, target):
        response = _create(client, TARGETS[target], _canonical_headers(provider))

        assert (response.status_code, response.json()) == (201, LINK)
        links.create_idp_link.assert_awaited_once_with(
            user_id=TARGETS[target], provider_id=PROVIDER_ID, external_sub="ext-1", external_email="", metadata=None
        )

    @pytest.mark.parametrize("target", sorted(TARGETS))
    def test_deletes_a_link(self, client, users, links, provider, target):
        response = _delete(client, TARGETS[target], _canonical_headers(provider))

        assert (response.status_code, response.content) == (204, b"")
        links.delete_idp_link.assert_awaited_once_with(link_id=LINK_ID, user_id=TARGETS[target])

    @pytest.mark.parametrize("route", sorted(ROUTES))
    def test_a_user_that_does_not_exist_is_not_found(self, client, users, links, provider, route):
        response = ROUTES[route](client, NOBODY, _canonical_headers(provider))

        assert {"status": response.status_code, "detail": response.json()["detail"]} == NOT_FOUND
        assert users.lookups == [NOBODY]
        assert links.mock_calls == []


@pytest.mark.parametrize("provider", sorted(CANONICAL))
@pytest.mark.parametrize("caller", sorted(NOT_OPERATORS))
@pytest.mark.parametrize("target", sorted(TARGETS))
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_tenants_owner_or_admin_is_refused(client, store, users, links, provider, caller, target, route):
    """Even for a user of their own tenant: the link would sign in as that user everywhere."""
    store.roles = NOT_OPERATORS[caller]

    response = ROUTES[route](client, TARGETS[target], _canonical_headers(provider, ACME))

    assert response.status_code == 403
    assert users.lookups == []
    assert links.mock_calls == []


@pytest.mark.parametrize("role", ["admin", "owner", "operator"])
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_descope_principal_is_refused_whatever_it_claims(client, users, links, route, role):
    """Its ``dct`` is spelled as Acme's canonical UUID: before #722 the guard found Alice's Acme membership."""
    response = ROUTES[route](client, ALICE, _descope_headers(role))

    assert (response.status_code, response.json()["detail"]) == (403, NOT_A_CANONICAL_PRINCIPAL)
    assert users.lookups == []
    assert links.mock_calls == []
