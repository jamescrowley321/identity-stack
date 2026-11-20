"""The IdP-link routes hold the target user to the tenant resolver's canonical tenant (#722, conventions §18).

A link decides which provider account signs in as a person. The routes check the
target user's canonical memberships in the caller's tenant, and only a principal
authorized from the canonical model has a canonical tenant: a Descope principal's
``dct`` is Descope's own tenant id, refused even when it is spelled as a canonical
tenant's UUID.

Requests go through the shipped gateway middleware and the shipped routers. Only
the canonical identity store, the assignment rows and the link service are stubs.
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
from app.dependencies.rbac import INSUFFICIENT_PERMISSIONS, get_rbac_session
from app.dependencies.tenant_access import NO_TENANT_CONTEXT, TENANT_SELECTOR_HEADER
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

# A user of Acme, and a user of Globex only.
ALICE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOB = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
MEMBERSHIPS = {(ALICE, uuid.UUID(ACME)), (BOB, uuid.UUID(GLOBEX))}

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

NOT_IN_TENANT = {"status": 404, "detail": "User not found in tenant"}


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    """An unsigned JWT: gateway mode reads the payload the gateway already verified."""
    return f"{_segment({'alg': 'RS256'})}.{_segment({'exp': int(time.time()) + 300, **claims})}.sig"


def _canonical_headers(provider: str, tenant: str) -> dict[str, str]:
    (issuer,) = CANONICAL[provider].accepted_issuers
    return {
        "Authorization": f"Bearer {_jwt({'iss': issuer, 'sub': SUB, **_BOUND[provider]})}",
        TENANT_SELECTOR_HEADER: tenant,
    }


def _descope_headers(role: str) -> dict[str, str]:
    """A Descope principal whose ``dct`` is spelled as Acme's canonical UUID, holding ``role`` there."""
    claims = {
        "iss": f"https://api.descope.com/{DESCOPE_PROJECT}",
        "sub": SUB,
        "dct": ACME,
        "tenants": {ACME: {"roles": [role], "permissions": []}},
    }
    return {"Authorization": f"Bearer {_jwt(claims)}"}


class _CanonicalStore:
    """The canonical caller is an admin of Acme and a member of Globex."""

    async def resolve(self, *, provider: str, sub: str):
        return Ok(
            {
                "user": {"id": "canonical-user-1", "status": "active"},
                "roles": [
                    {"tenant_id": ACME, "role_name": "admin", "permissions": []},
                    {"tenant_id": GLOBEX, "role_name": "member", "permissions": []},
                ],
                "tenant_memberships": [],
            }
        )


class _Assignments:
    """Stands in for UserTenantRoleRepository: a row for each of ``MEMBERSHIPS``; lookups are recorded."""

    def __init__(self) -> None:
        self.lookups: list[tuple[uuid.UUID, uuid.UUID]] = []

    def __call__(self, _session) -> "_Assignments":
        return self

    async def list_by_user_tenant(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> list[object]:
        self.lookups.append((user_id, tenant_id))
        return [object()] if (user_id, tenant_id) in MEMBERSHIPS else []


@pytest.fixture
def assignments(monkeypatch) -> _Assignments:
    rows = _Assignments()
    monkeypatch.setattr(idp_links, "UserTenantRoleRepository", rows)
    return rows


@pytest.fixture
def links() -> AsyncMock:
    service = AsyncMock(spec=IdPLinkService)
    service.get_user_idp_links.return_value = Ok([LINK])
    service.create_idp_link.return_value = Ok(LINK)
    service.delete_idp_link.return_value = Ok({"status": "deleted", "link_id": str(LINK_ID)})
    return service


@pytest.fixture
def client(monkeypatch, assignments, links) -> TestClient:
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset(CANONICAL))
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: _CanonicalStore())
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


def _problem(response) -> dict:
    body = response.json()
    return {"status": response.status_code, "detail": body["detail"]}


@pytest.mark.parametrize("provider", sorted(CANONICAL))
class TestACanonicalAdmin:
    def test_lists_the_links_of_a_user_of_its_tenant(self, client, assignments, links, provider):
        response = _list(client, ALICE, _canonical_headers(provider, ACME))

        assert (response.status_code, response.json()) == (200, {"idp_links": [LINK]})
        assert assignments.lookups == [(ALICE, uuid.UUID(ACME))]
        links.get_user_idp_links.assert_awaited_once_with(user_id=ALICE)

    def test_adds_a_link_for_a_user_of_its_tenant(self, client, assignments, links, provider):
        response = _create(client, ALICE, _canonical_headers(provider, ACME))

        assert (response.status_code, response.json()) == (201, LINK)
        assert assignments.lookups == [(ALICE, uuid.UUID(ACME))]
        links.create_idp_link.assert_awaited_once_with(
            user_id=ALICE, provider_id=PROVIDER_ID, external_sub="ext-1", external_email="", metadata=None
        )

    def test_deletes_a_link_of_a_user_of_its_tenant(self, client, assignments, links, provider):
        response = _delete(client, ALICE, _canonical_headers(provider, ACME))

        assert (response.status_code, response.content) == (204, b"")
        assert assignments.lookups == [(ALICE, uuid.UUID(ACME))]
        links.delete_idp_link.assert_awaited_once_with(link_id=LINK_ID, user_id=ALICE)

    @pytest.mark.parametrize("route", sorted(ROUTES))
    def test_a_user_of_another_tenant_is_not_found(self, client, assignments, links, provider, route):
        response = ROUTES[route](client, BOB, _canonical_headers(provider, ACME))

        assert _problem(response) == NOT_IN_TENANT
        assert assignments.lookups == [(BOB, uuid.UUID(ACME))]
        assert links.mock_calls == []

    @pytest.mark.parametrize("route", sorted(ROUTES))
    def test_where_it_is_only_a_member_it_is_refused(self, client, assignments, links, provider, route):
        response = ROUTES[route](client, BOB, _canonical_headers(provider, GLOBEX))

        assert (response.status_code, response.json()["detail"]) == (403, INSUFFICIENT_PERMISSIONS)
        assert assignments.lookups == []
        assert links.mock_calls == []

    @pytest.mark.parametrize("route", sorted(ROUTES))
    def test_naming_a_tenant_it_is_not_in_is_refused(self, client, assignments, links, provider, route):
        response = ROUTES[route](client, ALICE, _canonical_headers(provider, INITECH))

        # require_role resolves the tenant first and refuses it.
        assert (response.status_code, response.json()["detail"]) == (403, NO_TENANT_CONTEXT)
        assert assignments.lookups == []
        assert links.mock_calls == []


class TestADescopePrincipal:
    @pytest.mark.parametrize("route", sorted(ROUTES))
    def test_a_dct_spelled_as_a_canonical_tenant_id_reaches_no_user_of_that_tenant(
        self, client, assignments, links, route
    ):
        """Before #722 the guard parsed ``dct`` as a canonical UUID and found Alice's Acme membership."""
        response = ROUTES[route](client, ALICE, _descope_headers("admin"))

        assert _problem(response) == NOT_IN_TENANT
        assert assignments.lookups == []
        assert links.mock_calls == []

    @pytest.mark.parametrize("route", sorted(ROUTES))
    def test_without_owner_or_admin_it_is_refused(self, client, assignments, links, route):
        response = ROUTES[route](client, ALICE, _descope_headers("member"))

        assert (response.status_code, response.json()["detail"]) == (403, INSUFFICIENT_PERMISSIONS)
        assert assignments.lookups == []
        assert links.mock_calls == []
