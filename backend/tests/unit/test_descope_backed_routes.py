"""Every route that takes a tenant, or reaches a provider, is held to one register here (conventions §18).

A route the SPA calls serves every provider (ADR-0012). ``get_tenant_id`` gives a
principal of a canonical provider a canonical tenant id, which names nothing in
Descope, so each route says how it keeps one provider's ids from another:

- ``DESCOPE_TENANT_ROUTES`` and ``DESCOPE_PROJECT_ROUTES``: a route that hands the
  tenant or the caller's ``sub`` to Descope carries ``require_descope_principal``.
  None is left.
- ``SYNC_PROVIDER_TENANT_ROUTES`` and ``SYNC_PROVIDER_PROJECT_ROUTES``: the role
  assignment, member and role and permission definition routes, whose services
  push to the configured sync adapter, carry ``require_sync_provider_principal``.
  The definitions belong to no tenant: changing them is gated by
  ``require_definitions_role``, and a tenant's owner or admin lists them.
- ``PER_PROVIDER_ROUTES``: ``/api/keys``, whose service the sync provider builds.
- ``RELATIONSHIP_STORE_ROUTES``: the FGA and document routes, which
  ``capabilities.served`` gives only to the principals whose ids the store keys on.
- ``ADAPTER_CAPABILITY_ROUTES``: the routes of a sync adapter capability, which
  ``served`` gives only to that provider's own principals.
- ``PROVIDER_NEUTRAL_ROUTES``: routes that hand no tenant or user id to a provider.
- ``CANONICAL_TENANT_ROUTES``: the IdP-link routes, which take the resolver's tenant
  only when it is a canonical tenant id (``get_canonical_tenant_id``) and refuse any
  other principal, a Descope one among them (#722).
- ``GLOBAL_ROLE_ROUTES``: state that belongs to no tenant and that Descope does not
  hold, gated by ``require_global_role``, which lets no caller pick the tenant.

A machine credential's token is refused on every route that changes people's
roles, memberships, credentials or relations (``MACHINE_REFUSED_ROUTES``), and every
other write says why a machine may make it (``MACHINE_PERMITTED_WRITES``). A new
route that takes a tenant, is handed the Descope management adapter, is role-gated
or writes fails this file until it is put in a register.
"""

import asyncio
import base64
import json
import time
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Error, Ok
from fastapi import Depends, FastAPI, HTTPException
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies import capabilities, identity, rbac
from app.dependencies.capabilities import (
    require_custom_attributes,
    require_machine_credentials,
    require_relationship_store,
)
from app.dependencies.descope import NOT_A_DESCOPE_PRINCIPAL, get_descope_management, require_descope_principal
from app.dependencies.identity import (
    get_identity_resolution_service,
    get_machine_credential_service,
    get_member_directory,
    get_permission_service,
    get_provider_service,
    get_role_service,
    get_sync_status_service,
    get_user_service,
)
from app.dependencies.rbac import (
    OPERATOR_ROLE,
    RoleGrantRefusal,
    get_rbac_session,
    refuse_machine_principal,
    require_global_role,
    require_permission,
    require_role,
)
from app.dependencies.sync_provider import NOT_A_SYNC_PROVIDER_PRINCIPAL, require_sync_provider_principal
from app.dependencies.tenant import get_canonical_tenant_id, get_tenant_access, get_tenant_id
from app.dependencies.tenant_access import SELECT_A_TENANT_FIRST
from app.domain import access
from app.errors.access import MemberActionRefusal
from app.errors.identity import IdentityErrorRaised, ProviderError
from app.errors.problem_detail import identity_error_raised_handler
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import ProviderTokenConfig, TenantClaims, descope_config, keycloak_config, ory_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from app.services.adapters.base import Capability
from app.services.descope_management import DescopeManagementAdapter
from app.services.members import MemberService
from app.services.permission import PermissionService
from app.services.provider import ProviderService
from app.services.relationship_store import RelationshipStoreKind
from app.services.role import RoleService
from app.services.sync_status import SyncStatusService
from app.services.user import UserService

DESCOPE_PROJECT = "P123"
DESCOPE_ISSUER = f"https://api.descope.com/{DESCOPE_PROJECT}"
ORY_ISSUER = "https://ory.example.test"
ORY_AUDIENCE = "https://identity-stack-api"
FAKE_ISSUER = "https://fake.example.test"
KEYCLOAK_ISSUER = "https://keycloak.example.test/realms/r"
KEYCLOAK_AUDIENCE = "identity-stack-api"
KEYCLOAK_SPA = "identity-stack-spa"

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
RESOURCE_ID = "33333333-3333-3333-3333-333333333333"
ORY_SUB = "ory-subject-1"

REFUSED = {"detail": "This route serves Descope principals only"}
DESCOPE_EVERYTHING = capabilities.Served(frozenset(Capability), RelationshipStoreKind.DESCOPE)
KEY_BODY = {"name": "a key", "expire_time": 4_102_444_800, "role_names": ["owner"]}
NO_TENANT = {"detail": "No tenant context"}
NOT_GRANTED = {"detail": "Insufficient permissions"}
KEYCLOAK_ONLY = {"detail": NOT_A_SYNC_PROVIDER_PRINCIPAL.format(provider="Keycloak")}

# (method, path) of every route that hands the tenant or the subject to Descope. None is
# left: the tenant settings are the custom_attributes capability's (``ADAPTER_CAPABILITY_ROUTES``).
DESCOPE_TENANT_ROUTES: frozenset[tuple[str, str]] = frozenset()

# (method, path) of every route whose service is the sync provider's own
# implementation: the ``machine_credentials`` capability (``/api/keys``). A Descope
# deployment's builds Descope's access keys only after ``require_descope_principal``
# (``app.dependencies.identity._descope_access_keys``); a canonical provider's keeps
# canonical rows and never reaches Descope.
PER_PROVIDER_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/keys"),
        ("GET", "/api/keys"),
        ("GET", "/api/keys/{key_id}"),
        ("POST", "/api/keys/{key_id}/deactivate"),
        ("POST", "/api/keys/{key_id}/activate"),
        ("DELETE", "/api/keys/{key_id}"),
    }
)

# (method, path) of every guarded route over state that belongs to no tenant. None is
# left: the role and permission definitions are the sync provider's
# (``SYNC_PROVIDER_PROJECT_ROUTES``), the FGA schema the relationship store's.
DESCOPE_PROJECT_ROUTES: frozenset[tuple[str, str]] = frozenset()

# (method, path) of every route that keys the relationship store on the request's
# tenant and the caller's subject id. No guard: ``capabilities.served`` gives each
# store only to the principals whose ids it holds (Descope's store to Descope
# principals, OpenFGA to canonical ones), and refuses everyone else 404 first.
RELATIONSHIP_STORE_TENANT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/documents"),
        ("GET", "/api/documents"),
        ("GET", "/api/documents/{document_id}"),
        ("PUT", "/api/documents/{document_id}"),
        ("DELETE", "/api/documents/{document_id}"),
        ("POST", "/api/documents/{document_id}/share"),
        ("DELETE", "/api/documents/{document_id}/share/{target_user_id}"),
        ("POST", "/api/fga/relations"),
        ("DELETE", "/api/fga/relations"),
        ("GET", "/api/fga/relations"),
        ("POST", "/api/fga/check"),
    }
)

# (method, path) of the relationship store's schema routes: the store's own, no
# tenant's. Gated like the tenant routes above; on OpenFGA the schema is read-only.
RELATIONSHIP_STORE_SCHEMA_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("GET", "/api/fga/schema"), ("PUT", "/api/fga/schema")}
)
RELATIONSHIP_STORE_ROUTES = RELATIONSHIP_STORE_TENANT_ROUTES | RELATIONSHIP_STORE_SCHEMA_ROUTES

DESCOPE_BACKED_ROUTES = DESCOPE_TENANT_ROUTES | DESCOPE_PROJECT_ROUTES

# (method, path) of every route whose service pushes its writes to the configured sync
# adapter (``SYNC_PROVIDER``): ``require_sync_provider_principal`` passes only that
# provider's principals, whose tenant and user ids the adapter takes as its own. In a
# Descope deployment that is exactly the Descope guard. The assignments, the members
# and the definition lists (an owner, admin or operator of the tenant reads them, #707)
# take the resolver's tenant ...
DEFINITION_READ_ROUTES: frozenset[tuple[str, str]] = frozenset({("GET", "/api/roles"), ("GET", "/api/permissions")})
SYNC_PROVIDER_TENANT_ROUTES: frozenset[tuple[str, str]] = DEFINITION_READ_ROUTES | frozenset(
    {
        ("POST", "/api/roles/assign"),
        ("POST", "/api/roles/remove"),
        ("GET", "/api/members"),
        ("POST", "/api/members/invite"),
        ("POST", "/api/members/{user_id}/deactivate"),
        ("POST", "/api/members/{user_id}/activate"),
        ("DELETE", "/api/members/{user_id}"),
    }
)

# ... and changing the definitions, which belong to no tenant, is gated by ``require_global_role``.
SYNC_PROVIDER_PROJECT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/roles"),
        ("PUT", "/api/roles/{name}"),
        ("DELETE", "/api/roles/{name}"),
        ("POST", "/api/permissions"),
        ("PUT", "/api/permissions/{name}"),
        ("DELETE", "/api/permissions/{name}"),
    }
)

SYNC_PROVIDER_ROUTES = SYNC_PROVIDER_TENANT_ROUTES | SYNC_PROVIDER_PROJECT_ROUTES

# (method, path) of every write that changes who holds a relation: the FGA relations and
# schema, and a document's shares.
RELATION_WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("PUT", "/api/fga/schema"),
        ("POST", "/api/fga/relations"),
        ("DELETE", "/api/fga/relations"),
        ("POST", "/api/documents/{document_id}/share"),
        ("DELETE", "/api/documents/{document_id}/share/{target_user_id}"),
    }
)
# (method, path) of the writes that decide which provider account signs in as a person.
IDP_LINK_WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("POST", "/api/users/{user_id}/idp-links"), ("DELETE", "/api/users/{user_id}/idp-links/{link_id}")}
)

# (method, path) of every route a machine credential's token is refused on
# (``refuse_machine_principal``): no credential manages credentials, people's roles or
# sign-in links, the definitions, a tenant's members or anyone's relations (#680, #683).
MACHINE_REFUSED_ROUTES = PER_PROVIDER_ROUTES | SYNC_PROVIDER_ROUTES | RELATION_WRITE_ROUTES | IDP_LINK_WRITE_ROUTES

_NO_PRINCIPAL = "takes no principal: guarded by a shared secret, not a token"
# (method, path) -> why a write a machine credential's token may reach changes no
# person's roles, memberships, credentials or relations. Every other write is refused.
MACHINE_PERMITTED_WRITES: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/logout"): "ends the caller's own session",
    ("POST", "/api/validate-id-token"): "validates a token; writes nothing",
    ("POST", "/api/fga/check"): "reads one relation; writes nothing",
    ("POST", "/api/documents"): "creates a document of its tenant, owned by the caller",
    ("PUT", "/api/documents/{document_id}"): "changes a document the caller may edit (require_fga), not who holds it",
    ("DELETE", "/api/documents/{document_id}"): "deletes a document the caller may delete (require_fga)",
    ("POST", "/api/tenants/{tenant_id}/resources"): "adds a resource row to a tenant it belongs to",
    ("PATCH", "/api/tenants/current/settings"): "the tenant's custom attributes, not a person's grants",
    ("PATCH", "/api/profile"): "the caller's own canonical user, which a machine token never resolves to",
    ("PATCH", "/api/profile/attributes"): "the caller's own attributes at the sync provider",
    ("POST", "/api/tenants"): "require_admin_role reads Descope claims only: a canonical machine is refused (#596)",
    ("POST", "/api/providers"): "require_global_role('operator'), which no machine credential may hold",
    ("PATCH", "/api/providers/{provider_id}"): "require_global_role('operator'), which no machine credential may hold",
    ("POST", "/api/internal/reconciliation/run"): _NO_PRINCIPAL,
    ("POST", "/api/internal/users/sync"): _NO_PRINCIPAL,
    ("POST", "/api/internal/webhooks/descope"): _NO_PRINCIPAL,
}
_WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# path -> why a route of the application that is not an ``APIRoute`` (which every register
# here walks) needs no register: FastAPI's own documentation pages, read-only.
NON_API_ROUTES: dict[str, str] = {
    "/openapi.json": "FastAPI's OpenAPI document: GET only, no dependency, no principal",
    "/redoc": "FastAPI's ReDoc page: GET only, no dependency, no principal",
}

# (method, path) -> what keeps another provider's subject from Descope on a route
# that is handed the management adapter and carries no guard.
UNGUARDED_ADAPTER_ROUTES: dict[tuple[str, str], str] = {}

_OWN_PRINCIPALS_ONLY = (
    "the custom_attributes capability (test_capabilities.py) hands the sync adapter the caller's sub "
    "and tenant only when served() finds the caller a principal of the sync provider (Descope: descope_managed)"
)

# (method, path) -> why a route of a sync adapter capability hands no other provider's
# subject or tenant to the provider. Each depends on the capability's gate first.
ADAPTER_CAPABILITY_ROUTES: dict[tuple[str, str], str] = {
    ("PATCH", "/api/profile/attributes"): _OWN_PRINCIPALS_ONLY,
    ("GET", "/api/tenants/current/settings"): _OWN_PRINCIPALS_ONLY,
    ("PATCH", "/api/tenants/current/settings"): _OWN_PRINCIPALS_ONLY,
}
# The ones that take the request's tenant: the tenant settings.
ADAPTER_CAPABILITY_TENANT_ROUTES = frozenset(
    {("GET", "/api/tenants/current/settings"), ("PATCH", "/api/tenants/current/settings")}
)

# (method, path) -> why a principal from any provider may use the route's tenant.
# A route belongs here only when nothing behind it, its service's sync adapter
# included, hands the tenant or a user id to Descope.
PROVIDER_NEUTRAL_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/tenants/current"): (
        "reads a canonical principal's tenant from the canonical model; only a Descope principal's tenant, "
        "its dct, is loaded from Descope, and any other principal is refused (#673)"
    ),
    ("GET", "/api/access-checks/documents-read"): (
        "answers the resolver's tenant to a holder of documents:read: the handler calls no provider and reads no table"
    ),
    ("GET", "/api/access-checks/settings-manage"): (
        "answers the resolver's tenant to a holder of settings:manage: the handler calls no provider and reads no table"
    ),
    ("GET", "/api/roles/me"): (
        "answers the caller's own roles and permissions in the resolver's tenant: the handler calls no provider "
        "and reads no table"
    ),
}


# (method, path) of every route that holds its target to the caller's canonical tenant
# (``get_canonical_tenant_id``): canonical rows only, handed to no provider. A Descope
# principal's ``dct`` names no canonical tenant, so it is refused (404) (#722).
CANONICAL_TENANT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/users/{user_id}/idp-links"),
        ("POST", "/api/users/{user_id}/idp-links"),
        ("DELETE", "/api/users/{user_id}/idp-links/{link_id}"),
    }
)


# (method, path) of every route over state that belongs to no tenant and is not
# Descope's: one role in one tenant opens all of it, so the request picks nothing.
GLOBAL_ROLE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/users"),
        ("GET", "/api/providers"),
        ("POST", "/api/providers"),
        ("PATCH", "/api/providers/{provider_id}"),
        ("GET", "/api/providers/{provider_id}/capabilities"),
        ("GET", "/api/sync/status"),
        ("GET", "/api/events/recent"),
    }
)

# (method, path) -> why a route gated by ``require_role`` or ``require_permission``
# takes no tenant from ``get_tenant_id`` and carries no guard.
SELF_SCOPED_ROLE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/access-checks/admin-role"): (
        "answers back the roles require_role resolved in the request's tenant: the handler calls no provider "
        "and reads no table"
    ),
    ("GET", "/api/access-checks/viewer-role"): (
        "answers back the roles require_role resolved in the request's tenant: the handler calls no provider "
        "and reads no table"
    ),
}


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


def _routes_gated_by(*factories: Callable) -> set[tuple[str, str]]:
    """(method, path) of every route that depends on a closure one of ``factories`` returned."""
    prefixes = tuple(f"{factory.__name__}.<locals>." for factory in factories)
    return {
        (method, route.path)
        for route in shipped_app.routes
        if isinstance(route, APIRoute)
        and any(getattr(call, "__qualname__", "").startswith(prefixes) for call in _dependency_calls(route.dependant))
        for method in route.methods
    }


# The guarded routes a capability gate answers first (test_capabilities.py): Descope's
# capabilities and FGA store serve a Descope principal only, so any other principal
# gets the gate's 404 there, before the guard and before Descope.
CAPABILITY_GATED_ROUTES = frozenset().union(
    *(
        _routes_depending_on(gate)
        for gate in (require_machine_credentials, require_custom_attributes, require_relationship_store)
    )
)
CAPABILITY_MISSING_TYPE = "/errors/capability-missing"


def _refused(response, method: str, path: str) -> tuple[int, object]:
    """The status and the body (the problem type for a gate's 404) a refused caller got."""
    body = response.json()
    return response.status_code, body["type"] if (method, path) in CAPABILITY_GATED_ROUTES else body


def _refusal(method: str, path: str) -> tuple[int, object]:
    """What a principal Descope does not manage gets on a Descope-backed route."""
    return (404, CAPABILITY_MISSING_TYPE) if (method, path) in CAPABILITY_GATED_ROUTES else (403, REFUSED)


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
DESCOPE_OPERATOR = {
    "iss": DESCOPE_ISSUER,
    "sub": "descope-operator",
    "dct": "t1",
    "tenants": {"t1": {"roles": ["operator"], "permissions": []}},
}
DESCOPE_ADMIN = {
    "iss": DESCOPE_ISSUER,
    "sub": "descope-admin",
    "dct": "t1",
    "tenants": {"t1": {"roles": ["admin"], "permissions": []}},
}
KEYCLOAK_USER = {
    "iss": KEYCLOAK_ISSUER,
    "sub": "keycloak-user",
    "aud": KEYCLOAK_AUDIENCE,
    "azp": KEYCLOAK_SPA,
    "typ": "Bearer",
}
FAKE_ADMIN = {"iss": FAKE_ISSUER, "sub": "fake-user", "org": "org-1", "org_roles": ["owner"]}


SOLE_OWNER = [{"tenant_id": TENANT_A, "role_name": "owner", "permissions": ["documents.read"]}]
SOLE_ADMIN = [{"tenant_id": TENANT_A, "role_name": "admin", "permissions": []}]
SOLE_OPERATOR = [{"tenant_id": TENANT_A, "role_name": "operator", "permissions": []}]
MEMBER_OF_A_OPERATOR_OF_B = [
    {"tenant_id": TENANT_A, "role_name": "member", "permissions": []},
    {"tenant_id": TENANT_B, "role_name": "operator", "permissions": []},
]
MEMBER_OF_A_ADMIN_OF_B = [
    {"tenant_id": TENANT_A, "role_name": "member", "permissions": []},
    {"tenant_id": TENANT_B, "role_name": "admin", "permissions": []},
]


class _CanonicalStore:
    """Stands in for IdentityResolutionService: the Ory caller owns one tenant; lookups are recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.roles: list[dict] = SOLE_OWNER

    async def resolve(self, *, provider: str, sub: str):
        self.calls.append((provider, sub))
        return Ok({"user": {"id": "canonical-user-1", "status": "active"}, "roles": self.roles})


@pytest.fixture
def store(monkeypatch) -> _CanonicalStore:
    canonical = _CanonicalStore()
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: canonical)
    return canonical


@pytest.fixture
def descope_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def role_service() -> AsyncMock:
    return AsyncMock(spec=RoleService)


@pytest.fixture
def permission_service() -> AsyncMock:
    return AsyncMock(spec=PermissionService)


@pytest.fixture
def user_service() -> AsyncMock:
    service = AsyncMock(spec=UserService)
    service.list_canonical_users.return_value = []
    return service


@pytest.fixture
def provider_service() -> AsyncMock:
    return AsyncMock(spec=ProviderService)


@pytest.fixture
def sync_status_service() -> AsyncMock:
    return AsyncMock(spec=SyncStatusService)


@pytest.fixture
def client(
    store, descope_client, role_service, permission_service, user_service, provider_service, sync_status_service
) -> TestClient:
    """Every shipped route behind the shipped gateway middleware; only the stores are stubs."""
    limiter.reset()
    app = FastAPI()
    app.state.limiter = limiter
    app.state.descope_client = descope_client
    app.state.cache_publisher = MagicMock()
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_exception_handler(IdentityErrorRaised, identity_error_raised_handler)
    app.include_router(shipped_app.router)

    async def _session():
        yield MagicMock()

    app.dependency_overrides[get_rbac_session] = _session
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_role_service] = lambda: role_service
    app.dependency_overrides[get_permission_service] = lambda: permission_service
    app.dependency_overrides[get_user_service] = lambda: user_service
    app.dependency_overrides[get_provider_service] = lambda: provider_service
    app.dependency_overrides[get_sync_status_service] = lambda: sync_status_service
    app.add_middleware(
        GatewayClaimsMiddleware,
        providers=[
            descope_config(DESCOPE_PROJECT),
            ory_config(ORY_ISSUER, ORY_AUDIENCE),
            keycloak_config(KEYCLOAK_ISSUER, KEYCLOAK_AUDIENCE, KEYCLOAK_SPA),
            FAKE_PROVIDER,
        ],
    )
    return TestClient(app)


def _url(path: str) -> str:
    return (
        path.replace("{document_id}", RESOURCE_ID)
        .replace("{target_user_id}", "someone")
        .replace("{user_id}", "someone")
        .replace("{key_id}", "key-1")
        .replace("{provider_id}", RESOURCE_ID)
        .replace("{name}", "editor")
    )


class TestTheRegisters:
    def test_the_guard_is_on_exactly_the_descope_backed_routes(self):
        assert _routes_depending_on(require_descope_principal) == DESCOPE_BACKED_ROUTES

    def test_every_route_that_takes_a_tenant_is_in_one_register(self):
        takes_a_tenant = _routes_depending_on(get_tenant_id) | _routes_depending_on(get_tenant_access)

        assert takes_a_tenant == (
            DESCOPE_TENANT_ROUTES
            | set(PROVIDER_NEUTRAL_ROUTES)
            | SYNC_PROVIDER_TENANT_ROUTES
            | PER_PROVIDER_ROUTES
            | RELATIONSHIP_STORE_TENANT_ROUTES
            | ADAPTER_CAPABILITY_TENANT_ROUTES
            | CANONICAL_TENANT_ROUTES
        )

    def test_the_canonical_tenant_is_taken_on_exactly_the_canonical_tenant_routes(self):
        assert _routes_depending_on(get_canonical_tenant_id) == CANONICAL_TENANT_ROUTES

    def test_the_sync_provider_guard_is_on_exactly_the_sync_provider_routes(self):
        assert _routes_depending_on(require_sync_provider_principal) == SYNC_PROVIDER_ROUTES

    def test_machines_are_refused_on_exactly_the_credential_role_member_and_relation_routes(self):
        assert _routes_depending_on(refuse_machine_principal) == MACHINE_REFUSED_ROUTES

    def test_every_route_the_registers_cannot_see_says_why(self):
        """A Mount, a WebSocket or another non-APIRoute would slip past every register: none may, unlisted."""
        unseen = {
            getattr(route, "path", repr(route)): (
                type(route).__name__,
                frozenset(getattr(route, "methods", None) or ()),
            )
            for route in shipped_app.routes
            if not isinstance(route, APIRoute)
        }

        assert unseen == {path: ("Route", frozenset({"GET", "HEAD"})) for path in NON_API_ROUTES}

    def test_every_write_a_machine_may_reach_says_why(self):
        """A new write fails here until it refuses machines or says why a machine may make it."""
        writes = {
            (method, route.path)
            for route in shipped_app.routes
            if isinstance(route, APIRoute)
            for method in route.methods & _WRITES
        }

        assert writes - MACHINE_REFUSED_ROUTES == set(MACHINE_PERMITTED_WRITES)

    def test_no_route_carries_both_guards(self):
        assert SYNC_PROVIDER_ROUTES & DESCOPE_BACKED_ROUTES == set()

    def test_every_per_provider_route_builds_its_service_by_provider(self):
        assert PER_PROVIDER_ROUTES == _routes_depending_on(get_machine_credential_service)

    def test_the_relationship_store_gate_is_on_exactly_its_routes(self):
        assert _routes_depending_on(require_relationship_store) == RELATIONSHIP_STORE_ROUTES

    def test_no_project_route_takes_a_tenant(self):
        assert DESCOPE_TENANT_ROUTES & DESCOPE_PROJECT_ROUTES == set()

    def test_every_route_handed_the_management_adapter_is_guarded_or_says_why(self):
        """A new route that depends on ``get_descope_management`` alone fails here until it chooses."""
        handed_the_adapter = _routes_depending_on(get_descope_management)

        assert handed_the_adapter - _routes_depending_on(require_descope_principal) == set(UNGUARDED_ADAPTER_ROUTES)

    def test_every_capability_gated_route_is_per_provider_the_relationship_stores_or_an_adapter_capabilitys(self):
        assert CAPABILITY_GATED_ROUTES
        assert CAPABILITY_GATED_ROUTES - DESCOPE_BACKED_ROUTES - PER_PROVIDER_ROUTES - RELATIONSHIP_STORE_ROUTES == set(
            ADAPTER_CAPABILITY_ROUTES
        )

    def test_no_adapter_capability_route_is_handed_the_management_adapter(self):
        assert set(ADAPTER_CAPABILITY_ROUTES) & _routes_depending_on(get_descope_management) == set()

    def test_the_refusal_is_not_the_tenant_resolvers(self):
        """Selecting a tenant would not help the refused caller, so the body does not say to."""
        assert REFUSED == {"detail": NOT_A_DESCOPE_PRINCIPAL}
        assert NOT_A_DESCOPE_PRINCIPAL != SELECT_A_TENANT_FIRST

    def test_no_route_is_in_both(self):
        assert (DESCOPE_BACKED_ROUTES | SYNC_PROVIDER_ROUTES) & set(PROVIDER_NEUTRAL_ROUTES) == set()
        other_registers = DESCOPE_BACKED_ROUTES | SYNC_PROVIDER_ROUTES | set(PROVIDER_NEUTRAL_ROUTES)
        assert PER_PROVIDER_ROUTES & other_registers == set()
        assert (other_registers | PER_PROVIDER_ROUTES) & set(ADAPTER_CAPABILITY_ROUTES) == set()
        assert (
            other_registers | PER_PROVIDER_ROUTES | set(ADAPTER_CAPABILITY_ROUTES) | RELATIONSHIP_STORE_ROUTES
        ) & CANONICAL_TENANT_ROUTES == set()


class TestDescopesAccessKeysServeDescopePrincipalsOnly:
    """Behind the capability gate, the Descope builder refuses another provider's principal itself."""

    @pytest.mark.parametrize(("method", "path"), sorted(PER_PROVIDER_ROUTES))
    def test_even_when_the_gate_lets_it_through(self, client, monkeypatch, store, descope_client, method, path):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "descope")
        monkeypatch.setattr(capabilities, "served", lambda _request: DESCOPE_EVERYTHING)

        response = client.request(
            method, _url(path), json=KEY_BODY, headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_A}
        )

        assert (response.status_code, response.json()) == (403, REFUSED)
        assert descope_client.mock_calls == []

    def test_the_global_gate_is_on_exactly_the_global_routes(self):
        assert _routes_gated_by(require_global_role) == GLOBAL_ROLE_ROUTES | SYNC_PROVIDER_PROJECT_ROUTES

    def test_a_global_route_has_no_tenant_gate_beside_it(self):
        tenant_scoped = _routes_depending_on(get_tenant_id) | _routes_gated_by(require_role, require_permission)

        assert (GLOBAL_ROLE_ROUTES | SYNC_PROVIDER_PROJECT_ROUTES) & tenant_scoped == set()

    def test_every_tenant_gated_route_resolves_its_tenant_or_is_guarded_or_says_why(self):
        """A new route behind ``require_role`` alone fails here until it chooses."""
        gated = _routes_gated_by(require_role, require_permission)
        accounted_for = _routes_depending_on(get_tenant_id) | _routes_depending_on(require_descope_principal)

        assert gated - accounted_for == set(SELF_SCOPED_ROLE_ROUTES) | RELATIONSHIP_STORE_SCHEMA_ROUTES


class TestACanonicalProviderPrincipalIsRefused:
    """The Ory caller is an active owner of one tenant, so only the guard stands in the way."""

    @pytest.mark.parametrize(("method", "path"), sorted(DESCOPE_BACKED_ROUTES))
    def test_on_every_descope_backed_route(self, client, store, descope_client, method, path):
        response = client.request(method, _url(path), headers=_bearer(ORY_OWNER))

        assert _refused(response, method, path) == _refusal(method, path)
        # Refused before Descope is called and before the canonical model is read.
        assert descope_client.mock_calls == []
        assert store.calls == []

    def test_naming_its_tenant_changes_nothing(self, client, descope_client):
        response = client.get("/api/members", headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_A})

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
        assert _refused(documents, "GET", "/api/documents") == _refusal("GET", "/api/documents")
        assert descope_client.mock_calls == []


class TestTheProfileTakesNoOtherProvidersSubjectToDescope:
    """``/api/profile`` hands Descope the caller's ``sub``, which is Descope's only for its own principal.

    Name and email are the canonical user's for every provider; Descope is asked for
    the custom attributes of a Descope principal only.
    """

    CANONICAL_USER = {
        "id": "canonical-user-1",
        "email": "member@example.test",
        "given_name": "A",
        "family_name": "Member",
    }
    CANONICAL_PROFILE = {
        "user_id": ORY_SUB,
        "name": "A Member",
        "given_name": "A",
        "family_name": "Member",
        "email": "member@example.test",
        "custom_attributes": {},
    }

    @pytest.fixture
    def resolver(self, client) -> AsyncMock:
        resolver = AsyncMock()
        resolver.resolve.return_value = Ok({"user": self.CANONICAL_USER})
        client.app.dependency_overrides[get_identity_resolution_service] = lambda: resolver
        return resolver

    def test_an_ory_principal_reads_its_canonical_profile_and_descope_is_not_asked(
        self, client, descope_client, resolver
    ):
        response = client.get("/api/profile", headers=_bearer(ORY_OWNER))

        assert (response.status_code, response.json()) == (200, self.CANONICAL_PROFILE)
        resolver.resolve.assert_awaited_once_with(provider="ory", sub=ORY_SUB)
        assert descope_client.mock_calls == []

    def test_another_claims_provider_reads_its_canonical_profile_too(self, client, descope_client, resolver):
        response = client.get("/api/profile", headers=_bearer(FAKE_ADMIN))

        assert (response.status_code, response.json()) == (
            200,
            {**self.CANONICAL_PROFILE, "user_id": FAKE_ADMIN["sub"]},
        )
        assert descope_client.mock_calls == []

    def test_a_descope_principal_reads_its_attributes_from_descope(self, client, descope_client, resolver):
        descope_client.load_user.return_value = {
            "name": "Descope's name",
            "email": "descope@example.test",
            "customAttributes": {"department": "Engineering"},
        }

        response = client.get("/api/profile", headers=_bearer(DESCOPE_MEMBER))

        assert (response.status_code, response.json()) == (
            200,
            {
                **self.CANONICAL_PROFILE,
                "user_id": DESCOPE_MEMBER["sub"],
                "custom_attributes": {"department": "Engineering"},
            },
        )
        descope_client.load_user.assert_awaited_once_with(DESCOPE_MEMBER["sub"])
        resolver.resolve.assert_awaited_once_with(provider="descope", sub=DESCOPE_MEMBER["sub"])

    def test_an_ory_principal_cannot_write_an_attribute(self, client, descope_client):
        """A well-formed body with an allowed key: Descope's custom attributes do not serve it."""
        response = client.patch(
            "/api/profile/attributes", json={"key": "department", "value": "Engineering"}, headers=_bearer(ORY_OWNER)
        )

        assert _refused(response, "PATCH", "/api/profile/attributes") == (404, CAPABILITY_MISSING_TYPE)
        assert descope_client.mock_calls == []

    def test_a_descope_principal_writes_its_attribute(self, client, descope_client):
        descope_client.resolve_login_id.return_value = "member@example.test"

        response = client.patch(
            "/api/profile/attributes",
            json={"key": "department", "value": "Engineering"},
            headers=_bearer(DESCOPE_MEMBER),
        )

        assert response.status_code == 200
        assert response.json() == {"status": "updated", "key": "department", "value": "Engineering"}
        descope_client.update_user_custom_attribute.assert_awaited_once_with(
            "member@example.test", "department", "Engineering"
        )

    def test_a_request_with_no_token_gets_401(self, client):
        assert client.get("/api/profile").status_code == 401
        assert client.patch("/api/profile", json={"given_name": "x"}).status_code == 401
        assert client.patch("/api/profile/attributes", json={"key": "department", "value": "x"}).status_code == 401


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


class TestRoleAssignmentInADescopeDeployment:
    """``RoleService`` sends each assignment to the sync adapter, here Descope's: its principals only."""

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


class TestProjectWideStateInADescopeDeployment:
    """Role and permission definitions and the FGA schema belong to no tenant; a Descope principal reaches them."""

    @pytest.mark.parametrize(("method", "path"), sorted(DESCOPE_PROJECT_ROUTES))
    def test_an_admin_of_one_of_several_tenants_is_refused(
        self, client, store, descope_client, role_service, permission_service, method, path
    ):
        """Naming the tenant the caller administers would pass ``require_role``: the guard (or gate) refuses it."""
        store.roles = MEMBER_OF_A_ADMIN_OF_B

        response = client.request(method, _url(path), headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_B})

        assert _refused(response, method, path) == _refusal(method, path)
        assert descope_client.mock_calls == []
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []
        assert store.calls == []

    def test_a_descope_admin_lists_the_role_definitions(self, client, role_service):
        role_service.list_roles.return_value = Ok([{"id": RESOURCE_ID, "name": "editor"}])

        response = client.get("/api/roles", headers=_bearer(DESCOPE_ADMIN))

        assert response.status_code == 200
        assert response.json() == {"roles": [{"id": RESOURCE_ID, "name": "editor"}]}

    def test_a_descope_admin_lists_the_permission_definitions(self, client, permission_service):
        permission_service.list_permissions.return_value = Ok([{"id": RESOURCE_ID, "name": "documents.read"}])

        response = client.get("/api/permissions", headers=_bearer(DESCOPE_ADMIN))

        assert response.status_code == 200
        assert response.json() == {"permissions": [{"id": RESOURCE_ID, "name": "documents.read"}]}

    def test_a_descope_admin_reads_the_fga_schema(self, client, descope_client):
        descope_client.get_fga_schema.return_value = "model AuthZ 1.0"

        response = client.get("/api/fga/schema", headers=_bearer(DESCOPE_ADMIN))

        assert response.status_code == 200
        assert response.json() == {"schema": "model AuthZ 1.0"}

    def test_a_descope_member_is_still_refused_by_the_role_gate(self, client, role_service):
        response = client.get("/api/roles", headers=_bearer(DESCOPE_MEMBER))

        assert response.status_code == 403
        assert response.json() != REFUSED
        assert role_service.mock_calls == []


ROLE_ID = "44444444-4444-4444-4444-444444444444"
ASSIGNMENT = {"user_id": RESOURCE_ID, "tenant_id": TENANT_A, "role_names": ["member"]}
ASSIGNMENT_ANSWERS = {
    "/api/roles/assign": {"status": "roles_assigned", "user_id": RESOURCE_ID, "role_names": ["member"]},
    "/api/roles/remove": {"status": "roles_removed", "user_id": RESOURCE_ID, "role_names": ["member"]},
}


class TestTheRoleRoutesServeTheSyncProvidersPrincipals:
    """``RoleService`` and ``PermissionService`` push to ``SYNC_PROVIDER``: its principals are served, no others."""

    @pytest.fixture
    def keycloak_sync(self, monkeypatch, store):
        """A Keycloak deployment: Keycloak principals are authorized from the canonical model, writes go to Keycloak."""
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
        store.roles = SOLE_ADMIN
        return store

    @pytest.mark.parametrize(("method", "path"), sorted(SYNC_PROVIDER_ROUTES))
    def test_an_ory_owner_is_refused_where_descope_is_synced(
        self, client, store, descope_client, role_service, permission_service, method, path
    ):
        response = client.request(method, _url(path), headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, REFUSED)
        assert descope_client.mock_calls == []
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []
        assert store.calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(SYNC_PROVIDER_ROUTES))
    def test_a_descope_admin_is_refused_where_keycloak_is_synced(
        self, keycloak_sync, client, descope_client, role_service, permission_service, method, path
    ):
        response = client.request(method, _url(path), headers=_bearer(DESCOPE_ADMIN))

        assert (response.status_code, response.json()) == (403, KEYCLOAK_ONLY)
        assert descope_client.mock_calls == []
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []

    def test_a_keycloak_operator_lists_the_definitions(self, keycloak_sync, client, role_service, permission_service):
        keycloak_sync.roles = SOLE_OPERATOR
        role_service.list_roles.return_value = Ok([{"id": ROLE_ID, "name": "member"}])
        permission_service.list_permissions.return_value = Ok([{"id": RESOURCE_ID, "name": "documents.read"}])

        roles = client.get("/api/roles", headers=_bearer(KEYCLOAK_USER))
        permissions = client.get("/api/permissions", headers=_bearer(KEYCLOAK_USER))

        assert (roles.status_code, roles.json()) == (200, {"roles": [{"id": ROLE_ID, "name": "member"}]})
        assert (permissions.status_code, permissions.json()) == (
            200,
            {"permissions": [{"id": RESOURCE_ID, "name": "documents.read"}]},
        )
        # Each request decided from the canonical model, never from the token.
        assert keycloak_sync.calls == [("keycloak", KEYCLOAK_USER["sub"])] * 2

    @pytest.mark.parametrize(
        ("roles", "tenant"),
        [(SOLE_ADMIN, None), (SOLE_OWNER, TENANT_A), (MEMBER_OF_A_ADMIN_OF_B, TENANT_B)],
        ids=["admin of its sole tenant", "owner naming its tenant", "admin of one of several naming it"],
    )
    def test_a_keycloak_tenant_admin_lists_the_definitions(
        self, keycloak_sync, client, role_service, permission_service, roles, tenant
    ):
        """Listing changes nothing and shows only the global definitions, so the Roles page serves it (#707)."""
        keycloak_sync.roles = roles
        role_service.list_roles.return_value = Ok([{"id": ROLE_ID, "name": "member"}])
        permission_service.list_permissions.return_value = Ok([{"id": RESOURCE_ID, "name": "documents.read"}])
        headers = {**_bearer(KEYCLOAK_USER), **({"X-Tenant-ID": tenant} if tenant else {})}

        answers = {path: client.get(path, headers=headers) for path in ("/api/roles", "/api/permissions")}

        assert {path: (r.status_code, r.json()) for path, r in answers.items()} == {
            "/api/roles": (200, {"roles": [{"id": ROLE_ID, "name": "member"}]}),
            "/api/permissions": (200, {"permissions": [{"id": RESOURCE_ID, "name": "documents.read"}]}),
        }
        role_service.list_roles.assert_awaited_once_with()

    @pytest.mark.parametrize(("method", "path"), sorted(DEFINITION_READ_ROUTES))
    def test_a_keycloak_member_cannot_list_the_definitions(
        self, keycloak_sync, client, role_service, permission_service, method, path
    ):
        keycloak_sync.roles = [{"tenant_id": TENANT_A, "role_name": "member", "permissions": []}]

        response = client.request(method, path, headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, NOT_GRANTED)
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(DEFINITION_READ_ROUTES))
    def test_a_keycloak_admin_of_one_tenant_cannot_list_them_naming_another(
        self, keycloak_sync, client, role_service, permission_service, method, path
    ):
        keycloak_sync.roles = MEMBER_OF_A_ADMIN_OF_B

        response = client.request(method, path, headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, NOT_GRANTED)
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []

    def test_a_keycloak_operator_creates_a_role(self, keycloak_sync, client, role_service):
        keycloak_sync.roles = SOLE_OPERATOR
        role_service.create_role.return_value = Ok({"id": ROLE_ID, "name": "editor"})

        response = client.post("/api/roles", json={"name": "editor"}, headers=_bearer(KEYCLOAK_USER))

        assert (response.status_code, response.json()) == (201, {"id": ROLE_ID, "name": "editor"})
        role_service.create_role.assert_awaited_once_with(name="editor", description="", permission_names=None)

    @pytest.mark.parametrize(
        ("path", "service_call"),
        [("/api/roles/assign", "assign_role_to_user"), ("/api/roles/remove", "unassign_role_from_user")],
    )
    def test_a_keycloak_admin_assigns_in_the_tenant_it_administers(
        self, keycloak_sync, client, role_service, path, service_call
    ):
        role_service.list_roles.return_value = Ok([{"id": ROLE_ID, "name": "member"}])
        getattr(role_service, service_call).return_value = Ok({})

        response = client.post(path, json=ASSIGNMENT, headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (200, ASSIGNMENT_ANSWERS[path])
        getattr(role_service, service_call).assert_awaited_once_with(
            user_id=uuid.UUID(RESOURCE_ID), tenant_id=uuid.UUID(TENANT_A), role_id=uuid.UUID(ROLE_ID)
        )

    @pytest.mark.parametrize(("method", "path"), sorted(SYNC_PROVIDER_PROJECT_ROUTES))
    def test_a_keycloak_admin_of_its_sole_tenant_cannot_change_the_definitions(
        self, keycloak_sync, client, role_service, permission_service, method, path
    ):
        """A tenant admin's role is one tenant's; the definitions are every tenant's, so they need ``operator``."""
        response = client.request(method, _url(path), headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, NOT_GRANTED)
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(SYNC_PROVIDER_PROJECT_ROUTES))
    def test_a_keycloak_token_with_forged_descope_owner_claims_cannot_change_the_definitions(
        self, keycloak_sync, client, role_service, permission_service, method, path
    ):
        """The canonical model decides a Keycloak principal: an ``owner`` in a forged ``tenants`` claim is not read."""
        forged = {**KEYCLOAK_USER, "dct": TENANT_A, "tenants": {TENANT_A: {"roles": ["owner"], "permissions": []}}}

        response = client.request(method, _url(path), headers={**_bearer(forged), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, NOT_GRANTED)
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(SYNC_PROVIDER_PROJECT_ROUTES))
    def test_a_keycloak_principal_is_refused_where_keycloak_is_synced_but_not_canonical(
        self, keycloak_sync, monkeypatch, client, role_service, permission_service, method, path
    ):
        """Keycloak has no claims extractor: without canonical authorization nothing grants it a role."""
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory"}))
        forged = {**KEYCLOAK_USER, "dct": TENANT_A, "tenants": {TENANT_A: {"roles": ["owner"], "permissions": []}}}

        response = client.request(method, _url(path), headers={**_bearer(forged), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, NO_TENANT)
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(SYNC_PROVIDER_PROJECT_ROUTES))
    def test_a_keycloak_operator_of_one_of_several_tenants_cannot_change_the_definitions(
        self, keycloak_sync, client, role_service, permission_service, method, path
    ):
        """Naming the tenant the caller operates in does not open state that belongs to no tenant."""
        keycloak_sync.roles = MEMBER_OF_A_OPERATOR_OF_B

        response = client.request(method, _url(path), headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_B})

        assert (response.status_code, response.json()) == (403, NO_TENANT)
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(SYNC_PROVIDER_PROJECT_ROUTES))
    def test_a_keycloak_admin_of_one_of_several_tenants_cannot_change_the_definitions(
        self, keycloak_sync, client, role_service, permission_service, method, path
    ):
        """The definitions belong to no tenant: naming the tenant the caller administers does not open them."""
        keycloak_sync.roles = MEMBER_OF_A_ADMIN_OF_B

        response = client.request(method, _url(path), headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_B})

        assert (response.status_code, response.json()) == (403, NO_TENANT)
        assert role_service.mock_calls == []
        assert permission_service.mock_calls == []

    def test_that_admin_still_assigns_in_the_tenant_it_administers(self, keycloak_sync, client, role_service):
        keycloak_sync.roles = MEMBER_OF_A_ADMIN_OF_B
        role_service.list_roles.return_value = Ok([{"id": ROLE_ID, "name": "member"}])
        role_service.assign_role_to_user.return_value = Ok({})

        response = client.post(
            "/api/roles/assign",
            json={**ASSIGNMENT, "tenant_id": TENANT_B},
            headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_B},
        )

        assert (response.status_code, response.json()) == (200, ASSIGNMENT_ANSWERS["/api/roles/assign"])
        role_service.assign_role_to_user.assert_awaited_once_with(
            user_id=uuid.UUID(RESOURCE_ID), tenant_id=uuid.UUID(TENANT_B), role_id=uuid.UUID(ROLE_ID)
        )

    def test_a_keycloak_member_reads_its_canonical_roles(self, keycloak_sync, client):
        keycloak_sync.roles = [{"tenant_id": TENANT_A, "role_name": "member", "permissions": ["documents.read"]}]

        response = client.get("/api/roles/me", headers=_bearer(KEYCLOAK_USER))

        assert (response.status_code, response.json()) == (
            200,
            {"tenant_id": TENANT_A, "roles": ["member"], "permissions": ["documents.read"]},
        )


DEFINITION = {"id": ROLE_ID, "name": "editor"}
# (method, path) -> the request body, the status and the service method that answers it.
DEFINITION_CALLS: dict[tuple[str, str], tuple[dict | None, int, str]] = {
    ("GET", "/api/roles"): (None, 200, "list_roles"),
    ("POST", "/api/roles"): ({"name": "editor"}, 201, "create_role"),
    ("PUT", "/api/roles/{name}"): ({"description": "changed"}, 200, "update_role"),
    ("DELETE", "/api/roles/{name}"): (None, 200, "delete_role"),
    ("GET", "/api/permissions"): (None, 200, "list_permissions"),
    ("POST", "/api/permissions"): ({"name": "editor"}, 201, "create_permission"),
    ("PUT", "/api/permissions/{name}"): ({"new_name": "editor2"}, 200, "update_permission"),
    ("DELETE", "/api/permissions/{name}"): (None, 200, "delete_permission"),
}
DEFINITION_ANSWERS = {
    "list_roles": {"roles": [DEFINITION]},
    "list_permissions": {"permissions": [DEFINITION]},
}


class TestADescopeAdminChangesTheDefinitions:
    """Where Descope is synced, a Descope admin reaches every definition route from its claims, as before."""

    def test_every_definition_route_is_listed(self):
        assert DEFINITION_CALLS.keys() == SYNC_PROVIDER_PROJECT_ROUTES | DEFINITION_READ_ROUTES

    @pytest.mark.parametrize(("method", "path"), sorted(SYNC_PROVIDER_PROJECT_ROUTES | DEFINITION_READ_ROUTES))
    def test_on_every_definition_route(self, client, store, role_service, permission_service, method, path):
        body, status, call = DEFINITION_CALLS[(method, path)]
        role_service.list_roles.return_value = Ok([DEFINITION])
        permission_service.list_permissions.return_value = Ok([DEFINITION])
        for name in ("create_role", "update_role", "delete_role"):
            getattr(role_service, name).return_value = Ok(DEFINITION)
        for name in ("create_permission", "update_permission", "delete_permission"):
            getattr(permission_service, name).return_value = Ok(DEFINITION)
        service = permission_service if path.startswith("/api/permissions") else role_service

        response = client.request(method, _url(path), headers=_bearer(DESCOPE_ADMIN), json=body)

        assert (response.status_code, response.json()) == (status, DEFINITION_ANSWERS.get(call, DEFINITION))
        getattr(service, call).assert_awaited_once()
        assert store.calls == []


OPERATOR_GRANT_REFUSED = {"detail": RoleGrantRefusal.OPERATOR_GRANT}
OPERATOR_GRANT = {**ASSIGNMENT, "role_names": [OPERATOR_ROLE]}
OPERATOR_GRANT_ANSWERS = {
    path: {**answer, "role_names": [OPERATOR_ROLE]} for path, answer in ASSIGNMENT_ANSWERS.items()
}
ADMIN_AND_OPERATOR_OF_A = [
    {"tenant_id": TENANT_A, "role_name": "admin", "permissions": []},
    {"tenant_id": TENANT_A, "role_name": OPERATOR_ROLE, "permissions": []},
]
ADMIN_OF_A_OPERATOR_OF_B = [
    {"tenant_id": TENANT_A, "role_name": "admin", "permissions": []},
    {"tenant_id": TENANT_B, "role_name": OPERATOR_ROLE, "permissions": []},
]
ASSIGNMENT_PATHS = sorted(ASSIGNMENT_ANSWERS)


def _descope_token_in_tenant_a(roles: list[str]) -> dict:
    return {
        "iss": DESCOPE_ISSUER,
        "sub": "descope-tenant-a",
        "dct": TENANT_A,
        "tenants": {TENANT_A: {"roles": roles, "permissions": []}},
    }


class TestOnlyAnOperatorAssignsOrRemovesOperator:
    """``operator`` opens every tenant's state: a tenant admin must not hand it out, to itself or anyone."""

    @pytest.fixture
    def keycloak_sync(self, monkeypatch, store):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
        store.roles = SOLE_ADMIN
        return store

    @pytest.mark.parametrize("path", ASSIGNMENT_PATHS)
    @pytest.mark.parametrize("user_id", [RESOURCE_ID, "canonical-user-1"], ids=["another member", "itself"])
    def test_a_keycloak_admin_of_its_sole_tenant_is_refused(self, keycloak_sync, client, role_service, path, user_id):
        body = {**OPERATOR_GRANT, "user_id": user_id}

        response = client.post(path, json=body, headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, OPERATOR_GRANT_REFUSED)
        assert role_service.mock_calls == []

    @pytest.mark.parametrize("path", ASSIGNMENT_PATHS)
    def test_a_keycloak_admin_who_operates_another_tenant_is_refused(self, keycloak_sync, client, role_service, path):
        """Operator is decided as ``require_global_role`` decides it: one of several memberships is not enough."""
        keycloak_sync.roles = ADMIN_OF_A_OPERATOR_OF_B

        response = client.post(path, json=OPERATOR_GRANT, headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, OPERATOR_GRANT_REFUSED)
        assert role_service.mock_calls == []

    @pytest.mark.parametrize(
        ("path", "service_call"),
        [("/api/roles/assign", "assign_role_to_user"), ("/api/roles/remove", "unassign_role_from_user")],
    )
    def test_a_keycloak_operator_and_admin_of_its_sole_tenant_is_served(
        self, keycloak_sync, client, role_service, path, service_call
    ):
        keycloak_sync.roles = ADMIN_AND_OPERATOR_OF_A
        role_service.list_roles.return_value = Ok([{"id": ROLE_ID, "name": OPERATOR_ROLE}])
        getattr(role_service, service_call).return_value = Ok({})

        response = client.post(path, json=OPERATOR_GRANT, headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (200, OPERATOR_GRANT_ANSWERS[path])
        getattr(role_service, service_call).assert_awaited_once_with(
            user_id=uuid.UUID(RESOURCE_ID), tenant_id=uuid.UUID(TENANT_A), role_id=uuid.UUID(ROLE_ID)
        )

    @pytest.mark.parametrize("path", ASSIGNMENT_PATHS)
    def test_a_descope_admin_is_refused(self, client, role_service, path):
        response = client.post(path, json=OPERATOR_GRANT, headers=_bearer(_descope_token_in_tenant_a(["admin"])))

        assert (response.status_code, response.json()) == (403, OPERATOR_GRANT_REFUSED)
        assert role_service.mock_calls == []

    @pytest.mark.parametrize(
        ("path", "service_call"),
        [("/api/roles/assign", "assign_role_to_user"), ("/api/roles/remove", "unassign_role_from_user")],
    )
    def test_a_descope_admin_and_operator_is_served(self, client, store, role_service, path, service_call):
        role_service.list_roles.return_value = Ok([{"id": ROLE_ID, "name": OPERATOR_ROLE}])
        getattr(role_service, service_call).return_value = Ok({})
        token = _descope_token_in_tenant_a(["admin", OPERATOR_ROLE])

        response = client.post(path, json=OPERATOR_GRANT, headers=_bearer(token))

        assert (response.status_code, response.json()) == (200, OPERATOR_GRANT_ANSWERS[path])
        assert store.calls == []

    @pytest.mark.parametrize("path", ASSIGNMENT_PATHS)
    def test_another_role_needs_no_operator(self, keycloak_sync, client, role_service, path):
        role_service.list_roles.return_value = Ok([{"id": ROLE_ID, "name": "member"}])
        role_service.assign_role_to_user.return_value = Ok({})
        role_service.unassign_role_from_user.return_value = Ok({})

        response = client.post(path, json=ASSIGNMENT, headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (200, ASSIGNMENT_ANSWERS[path])


OPERATOR_DEFINITION_REFUSED = {"detail": RoleGrantRefusal.OPERATOR_DEFINITION}
OPERATOR_DEFINITION = {"id": ROLE_ID, "name": OPERATOR_ROLE}
# What touches the ``operator`` definition: (method, path, body, the RoleService call that answers it).
OPERATOR_DEFINITION_CHANGES = {
    "create it": ("POST", "/api/roles", {"name": OPERATOR_ROLE}, "create_role"),
    "change it": ("PUT", f"/api/roles/{OPERATOR_ROLE}", {"description": "changed"}, "update_role"),
    "rename it": ("PUT", f"/api/roles/{OPERATOR_ROLE}", {"new_name": "renamed"}, "update_role"),
    "rename another role to it": ("PUT", "/api/roles/editor", {"new_name": OPERATOR_ROLE}, "update_role"),
    "delete it": ("DELETE", f"/api/roles/{OPERATOR_ROLE}", None, "delete_role"),
}
OPERATOR_DEFINITION_STATUS = {"create_role": 201, "update_role": 200, "delete_role": 200}
DESCOPE_ADMIN_AND_OPERATOR = {
    **DESCOPE_ADMIN,
    "sub": "descope-operator-admin",
    "tenants": {"t1": {"roles": ["admin", OPERATOR_ROLE], "permissions": []}},
}


class TestOnlyAnOperatorChangesTheOperatorDefinition:
    """A Descope admin passes the definitions gate, but ``operator`` opens every tenant: only an operator touches it."""

    @staticmethod
    def _answer(role_service: AsyncMock) -> None:
        role_service.list_roles.return_value = Ok([OPERATOR_DEFINITION, {"id": RESOURCE_ID, "name": "editor"}])
        for call in OPERATOR_DEFINITION_STATUS:
            getattr(role_service, call).return_value = Ok(OPERATOR_DEFINITION)

    @pytest.mark.parametrize("change", sorted(OPERATOR_DEFINITION_CHANGES))
    def test_a_descope_admin_is_refused(self, client, role_service, change):
        method, path, body, _call = OPERATOR_DEFINITION_CHANGES[change]

        response = client.request(method, path, json=body, headers=_bearer(DESCOPE_ADMIN))

        assert (response.status_code, response.json()) == (403, OPERATOR_DEFINITION_REFUSED)
        assert role_service.mock_calls == []

    @pytest.mark.parametrize("change", sorted(OPERATOR_DEFINITION_CHANGES))
    def test_a_descope_operator_is_served(self, client, role_service, change):
        method, path, body, call = OPERATOR_DEFINITION_CHANGES[change]
        self._answer(role_service)

        response = client.request(method, path, json=body, headers=_bearer(DESCOPE_ADMIN_AND_OPERATOR))

        assert (response.status_code, response.json()) == (OPERATOR_DEFINITION_STATUS[call], OPERATOR_DEFINITION)
        getattr(role_service, call).assert_awaited_once()

    @pytest.mark.parametrize("change", sorted(OPERATOR_DEFINITION_CHANGES))
    def test_a_canonical_operator_is_served(self, monkeypatch, client, store, role_service, change):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
        store.roles = SOLE_OPERATOR
        method, path, body, call = OPERATOR_DEFINITION_CHANGES[change]
        self._answer(role_service)

        response = client.request(method, path, json=body, headers=_bearer(KEYCLOAK_USER))

        assert (response.status_code, response.json()) == (OPERATOR_DEFINITION_STATUS[call], OPERATOR_DEFINITION)
        getattr(role_service, call).assert_awaited_once()

    def test_another_role_needs_no_operator(self, client, role_service):
        role_service.list_roles.return_value = Ok([{"id": RESOURCE_ID, "name": "editor"}])
        role_service.delete_role.return_value = Ok({"status": "deleted", "name": "editor"})

        response = client.delete("/api/roles/editor", headers=_bearer(DESCOPE_ADMIN))

        assert (response.status_code, response.json()) == (200, {"status": "deleted", "name": "editor"})


MEMBER_ID = "55555555-5555-5555-5555-555555555555"
MEMBER_ROUTE_CALLS = {
    ("GET", "/api/members"): ("list_members", (TENANT_A,), {}),
    ("POST", "/api/members/invite"): (
        "invite_member",
        (),
        {"email": "new@example.com", "tenant_id": TENANT_A, "role_names": ["member"]},
    ),
    ("POST", "/api/members/{user_id}/deactivate"): ("set_member_status", (MEMBER_ID, TENANT_A, "disabled"), {}),
    ("POST", "/api/members/{user_id}/activate"): ("set_member_status", (MEMBER_ID, TENANT_A, "enabled"), {}),
    ("DELETE", "/api/members/{user_id}"): ("remove_member", (MEMBER_ID, TENANT_A), {}),
}
MEMBER_ROUTE_ANSWERS = {
    ("GET", "/api/members"): {"members": []},
    ("POST", "/api/members/invite"): {"status": "invited"},
    ("POST", "/api/members/{user_id}/deactivate"): {"status": "deactivated", "user_id": MEMBER_ID},
    ("POST", "/api/members/{user_id}/activate"): {"status": "activated", "user_id": MEMBER_ID},
    ("DELETE", "/api/members/{user_id}"): {"status": "removed", "user_id": MEMBER_ID},
}

# route name: (method, path, the directory call it makes) for the actions on one member.
MEMBER_ACTIONS = {
    "deactivate": ("POST", "/api/members/{user_id}/deactivate", "set_member_status"),
    "activate": ("POST", "/api/members/{user_id}/activate", "set_member_status"),
    "remove": ("DELETE", "/api/members/{user_id}", "remove_member"),
}
# The canonical user id ``_CanonicalStore`` resolves every caller to.
CANONICAL_CALLER = "canonical-user-1"


class TestTheMemberRoutesServeTheSyncProvidersPrincipals:
    """Where Keycloak is synced, its tenant admins manage the tenant's canonical members (#677)."""

    @pytest.fixture
    def keycloak_sync(self, monkeypatch, store):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
        store.roles = SOLE_ADMIN
        return store

    @pytest.fixture
    def members(self, client) -> AsyncMock:
        directory = AsyncMock(spec=MemberService)
        directory.list_members.return_value = Ok({"members": []})
        directory.invite_member.return_value = Ok({"status": "invited"})
        directory.set_member_status.return_value = Ok({})
        directory.remove_member.return_value = Ok({"status": "removed", "user_id": MEMBER_ID})
        directory.tenant_member.return_value = Ok(access.TenantMember(user_id=MEMBER_ID, roles=frozenset({"member"})))
        client.app.dependency_overrides[get_member_directory] = lambda: directory
        return directory

    @staticmethod
    def _call(client, method: str, path: str, headers: dict):
        body = {"email": "new@example.com"} if path.endswith("/invite") else None
        return client.request(method, path.replace("{user_id}", MEMBER_ID), json=body, headers=headers)

    @pytest.mark.parametrize(("method", "path"), sorted(MEMBER_ROUTE_CALLS))
    def test_a_keycloak_admin_manages_the_members_of_its_tenant(self, keycloak_sync, client, members, method, path):
        response = self._call(client, method, path, {**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (200, MEMBER_ROUTE_ANSWERS[(method, path)])
        name, args, kwargs = MEMBER_ROUTE_CALLS[(method, path)]
        getattr(members, name).assert_awaited_once_with(*args, **kwargs)

    @pytest.mark.parametrize(("method", "path"), sorted(MEMBER_ROUTE_CALLS))
    def test_a_keycloak_member_is_refused_as_today(self, keycloak_sync, client, members, method, path):
        keycloak_sync.roles = [{"tenant_id": TENANT_A, "role_name": "member", "permissions": []}]

        response = self._call(client, method, path, {**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, NOT_GRANTED)
        assert members.mock_calls == []

    def test_a_keycloak_admin_cannot_name_another_tenant(self, keycloak_sync, client, members):
        response = client.get("/api/members", headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_B})

        assert response.status_code == 403
        assert members.mock_calls == []

    def test_an_admin_cannot_invite_an_owner(self, keycloak_sync, client, members):
        response = client.post(
            "/api/members/invite",
            json={"email": "new@example.com", "role_names": ["owner"]},
            headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A},
        )

        assert (response.status_code, response.json()) == (403, {"detail": "Only owners can assign the owner role"})
        assert members.mock_calls == []

    @pytest.mark.parametrize(
        "roles", [SOLE_ADMIN, ADMIN_OF_A_OPERATOR_OF_B], ids=["admin of its sole tenant", "operator of another"]
    )
    def test_a_keycloak_admin_cannot_invite_an_operator(self, keycloak_sync, client, members, roles):
        """An invitation with ``operator`` hands out every tenant's state, as an assignment of it would."""
        keycloak_sync.roles = roles

        response = client.post(
            "/api/members/invite",
            json={"email": "new@example.com", "role_names": ["member", OPERATOR_ROLE]},
            headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A},
        )

        assert (response.status_code, response.json()) == (403, OPERATOR_GRANT_REFUSED)
        assert members.mock_calls == []

    def test_a_keycloak_operator_and_admin_of_its_sole_tenant_invites_an_operator(self, keycloak_sync, client, members):
        keycloak_sync.roles = ADMIN_AND_OPERATOR_OF_A

        response = client.post(
            "/api/members/invite",
            json={"email": "new@example.com", "role_names": [OPERATOR_ROLE]},
            headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A},
        )

        assert (response.status_code, response.json()) == (200, {"status": "invited"})
        members.invite_member.assert_awaited_once_with(
            email="new@example.com", tenant_id=TENANT_A, role_names=[OPERATOR_ROLE]
        )

    @pytest.mark.parametrize(
        ("roles", "answer"),
        [(["admin"], (403, OPERATOR_GRANT_REFUSED)), (["admin", OPERATOR_ROLE], (200, {"status": "invited"}))],
        ids=["admin", "admin and operator"],
    )
    def test_a_descope_admin_invites_an_operator_only_as_an_operator(self, client, members, roles, answer):
        response = client.post(
            "/api/members/invite",
            json={"email": "new@example.com", "role_names": [OPERATOR_ROLE]},
            headers=_bearer(_descope_token_in_tenant_a(roles)),
        )

        assert (response.status_code, response.json()) == answer

    def test_a_revocation_keycloak_did_not_take_is_told(self, keycloak_sync, client, members):
        members.set_member_status.return_value = Ok({"email": "m@acme.test", "provider_sync": "failed"})

        response = client.post(
            f"/api/members/{MEMBER_ID}/deactivate", headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A}
        )

        assert (response.status_code, response.json()) == (
            200,
            {"status": "deactivated", "user_id": MEMBER_ID, "provider_sync": "failed"},
        )

    @pytest.mark.parametrize("route", sorted(MEMBER_ACTIONS))
    def test_a_keycloak_admin_cannot_act_on_the_owner(self, keycloak_sync, client, members, route):
        members.tenant_member.return_value = Ok(access.TenantMember(user_id=MEMBER_ID, roles=frozenset({"owner"})))
        method, path, call = MEMBER_ACTIONS[route]

        response = self._call(client, method, path, {**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, {"detail": MemberActionRefusal.OWNER_REQUIRED})
        members.tenant_member.assert_awaited_once_with(MEMBER_ID, TENANT_A)
        getattr(members, call).assert_not_awaited()

    @pytest.mark.parametrize("route", sorted(MEMBER_ACTIONS))
    def test_a_keycloak_owner_acts_on_another_owner(self, keycloak_sync, client, members, route):
        keycloak_sync.roles = SOLE_OWNER
        members.tenant_member.return_value = Ok(access.TenantMember(user_id=MEMBER_ID, roles=frozenset({"owner"})))
        method, path, call = MEMBER_ACTIONS[route]

        response = self._call(client, method, path, {**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (200, MEMBER_ROUTE_ANSWERS[(method, path)])
        getattr(members, call).assert_awaited_once()

    @pytest.mark.parametrize("route", sorted(MEMBER_ACTIONS))
    @pytest.mark.parametrize(
        "roles", [SOLE_ADMIN, ADMIN_OF_A_OPERATOR_OF_B], ids=["admin of its sole tenant", "operator of another"]
    )
    def test_a_keycloak_admin_cannot_act_on_an_operator(self, keycloak_sync, client, members, route, roles):
        keycloak_sync.roles = roles
        members.tenant_member.return_value = Ok(
            access.TenantMember(user_id=MEMBER_ID, roles=frozenset({OPERATOR_ROLE}))
        )
        method, path, call = MEMBER_ACTIONS[route]

        response = self._call(client, method, path, {**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (403, OPERATOR_GRANT_REFUSED)
        getattr(members, call).assert_not_awaited()

    @pytest.mark.parametrize("route", sorted(MEMBER_ACTIONS))
    def test_a_keycloak_operator_and_admin_acts_on_an_operator(self, keycloak_sync, client, members, route):
        keycloak_sync.roles = ADMIN_AND_OPERATOR_OF_A
        members.tenant_member.return_value = Ok(
            access.TenantMember(user_id=MEMBER_ID, roles=frozenset({OPERATOR_ROLE}))
        )
        method, path, call = MEMBER_ACTIONS[route]

        response = self._call(client, method, path, {**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A})

        assert (response.status_code, response.json()) == (200, MEMBER_ROUTE_ANSWERS[(method, path)])
        getattr(members, call).assert_awaited_once()

    @pytest.mark.parametrize("route", ["deactivate", "remove"])
    def test_a_keycloak_admin_neither_deactivates_nor_removes_itself(self, keycloak_sync, client, members, route):
        """The caller is the store's canonical user: it could not sign back in to undo it."""
        members.tenant_member.return_value = Ok(
            access.TenantMember(user_id=CANONICAL_CALLER, roles=frozenset({"admin"}))
        )
        method, path, call = MEMBER_ACTIONS[route]

        response = client.request(
            method,
            path.replace("{user_id}", CANONICAL_CALLER),
            headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A},
        )

        assert (response.status_code, response.json()) == (403, {"detail": MemberActionRefusal.ACTING_ON_SELF})
        members.tenant_member.assert_awaited_once_with(CANONICAL_CALLER, TENANT_A)
        getattr(members, call).assert_not_awaited()

    def test_a_failed_member_lookup_changes_nothing(self, keycloak_sync, client, members):
        members.tenant_member.return_value = Error(ProviderError(message="down"))

        response = client.delete(
            f"/api/members/{MEMBER_ID}", headers={**_bearer(KEYCLOAK_USER), "X-Tenant-ID": TENANT_A}
        )

        assert (response.status_code, response.json()) == (502, {"detail": "Failed to remove member"})
        members.remove_member.assert_not_awaited()

    def test_the_directory_is_the_canonical_one_where_keycloak_is_synced(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(keycloak_admin=MagicMock(), cache_publisher=MagicMock()))
        )

        directory = asyncio.run(get_member_directory(request, MagicMock()))

        assert isinstance(directory, MemberService)

    def test_the_directory_is_descope_where_descope_is_synced(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(descope_client=MagicMock())))

        directory = asyncio.run(get_member_directory(request, MagicMock()))

        assert isinstance(directory, DescopeManagementAdapter)


class TestTheSyncProviderGuard:
    def test_every_sync_adapter_names_its_principals(self):
        assert identity.SYNC_PROVIDER_NAMES.keys() == identity.SYNC_ADAPTERS.keys()

    @pytest.mark.parametrize("state", [{}, {"claims": None}], ids=["no claims", "claims is None"])
    def test_a_request_with_no_claims_is_refused_by_the_guard_itself(self, state):
        with pytest.raises(HTTPException) as refused:
            require_sync_provider_principal(_hand_built_request(**state))

        assert (refused.value.status_code, refused.value.detail) == (401, "Not authenticated")

    def test_claims_that_no_provider_is_recorded_for_are_refused(self):
        with pytest.raises(HTTPException) as refused:
            require_sync_provider_principal(_hand_built_request(claims=DESCOPE_ADMIN))

        assert (refused.value.status_code, refused.value.detail) == (403, REFUSED["detail"])

    def test_an_unknown_sync_provider_passes_nobody(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "unknown")
        request = _hand_built_request(claims=DESCOPE_ADMIN, provider=descope_config(DESCOPE_PROJECT))

        with pytest.raises(HTTPException) as refused:
            require_sync_provider_principal(request)

        assert (refused.value.status_code, refused.value.detail) == (
            403,
            NOT_A_SYNC_PROVIDER_PRINCIPAL.format(provider="unknown"),
        )

    def test_a_descope_principal_passes_where_descope_is_synced(self):
        request = _hand_built_request(claims=DESCOPE_ADMIN, provider=descope_config(DESCOPE_PROJECT))

        assert require_sync_provider_principal(request) is None


class TestStateThatBelongsToNoTenant:
    """One role in one tenant opens every tenant's users, the provider records and the sync status."""

    @pytest.mark.parametrize(("method", "path"), sorted(GLOBAL_ROLE_ROUTES))
    def test_an_operator_of_one_of_several_tenants_cannot_name_it_with_the_header(
        self, client, store, user_service, provider_service, sync_status_service, method, path
    ):
        store.roles = MEMBER_OF_A_OPERATOR_OF_B

        response = client.request(method, _url(path), headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_B})

        assert response.status_code == 403
        assert response.json() == NO_TENANT
        assert user_service.mock_calls == []
        assert provider_service.mock_calls == []
        assert sync_status_service.mock_calls == []

    def test_the_header_does_name_that_tenant_on_a_tenant_scoped_gate(self, store):
        """The caller above is an operator where the header points: only the global gate refuses them."""
        store.roles = MEMBER_OF_A_OPERATOR_OF_B
        app = FastAPI()

        @app.get("/tenant-scoped")
        async def tenant_scoped(roles: list[str] = Depends(require_role("operator"))):
            return {"roles": roles}

        async def _session():
            yield MagicMock()

        app.dependency_overrides[get_rbac_session] = _session
        app.add_middleware(GatewayClaimsMiddleware, providers=[ory_config(ORY_ISSUER, ORY_AUDIENCE)])

        response = TestClient(app).get("/tenant-scoped", headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_B})

        assert response.status_code == 200
        assert response.json() == {"roles": ["operator"]}

    def test_several_memberships_and_no_header_are_refused(self, client, store, user_service):
        store.roles = MEMBER_OF_A_OPERATOR_OF_B

        response = client.get("/api/users", headers=_bearer(ORY_OWNER))

        assert response.status_code == 403
        assert response.json() == NO_TENANT
        assert user_service.mock_calls == []

    def test_an_operator_by_their_sole_membership_is_served(self, client, store):
        store.roles = SOLE_OPERATOR

        response = client.get("/api/users", headers=_bearer(ORY_OWNER))

        assert response.status_code == 200
        assert response.json() == {"users": []}

    def test_a_sole_membership_named_by_the_header_is_served(self, client, store):
        """The request picks nothing here, but a request that names the caller's one tenant is not wrong."""
        store.roles = SOLE_OPERATOR

        response = client.get("/api/users", headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_A})

        assert response.status_code == 200
        assert response.json() == {"users": []}

    def test_a_header_naming_another_tenant_is_refused_even_with_one_membership(self, client, store, user_service):
        store.roles = SOLE_OPERATOR

        response = client.get("/api/users", headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_B})

        assert response.status_code == 403
        assert response.json() == NO_TENANT
        assert user_service.mock_calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(GLOBAL_ROLE_ROUTES))
    def test_an_operator_of_one_of_several_tenants_cannot_name_it_with_a_dct(
        self, client, store, user_service, provider_service, sync_status_service, method, path
    ):
        """A ``dct`` on the token is a selection too: it chooses nothing here either."""
        store.roles = MEMBER_OF_A_OPERATOR_OF_B

        response = client.request(method, _url(path), headers=_bearer({**ORY_OWNER, "dct": TENANT_B}))

        assert response.status_code == 403
        assert response.json() == NO_TENANT
        assert user_service.mock_calls == []
        assert provider_service.mock_calls == []
        assert sync_status_service.mock_calls == []

    def test_the_dct_does_name_that_tenant_on_a_tenant_scoped_gate(self, store):
        """The caller above is an operator where the ``dct`` points: only the global gate refuses them."""
        store.roles = MEMBER_OF_A_OPERATOR_OF_B
        app = FastAPI()

        @app.get("/tenant-scoped")
        async def tenant_scoped(roles: list[str] = Depends(require_role("operator"))):
            return {"roles": roles}

        async def _session():
            yield MagicMock()

        app.dependency_overrides[get_rbac_session] = _session
        app.add_middleware(GatewayClaimsMiddleware, providers=[ory_config(ORY_ISSUER, ORY_AUDIENCE)])

        response = TestClient(app).get("/tenant-scoped", headers=_bearer({**ORY_OWNER, "dct": TENANT_B}))

        assert response.status_code == 200
        assert response.json() == {"roles": ["operator"]}

    def test_a_sole_membership_named_by_a_dct_is_served(self, client, store):
        store.roles = SOLE_OPERATOR

        response = client.get("/api/users", headers=_bearer({**ORY_OWNER, "dct": TENANT_A}))

        assert response.status_code == 200
        assert response.json() == {"users": []}

    def test_a_dct_naming_another_tenant_is_refused_even_with_one_membership(self, client, store, user_service):
        store.roles = SOLE_OPERATOR

        response = client.get("/api/users", headers=_bearer({**ORY_OWNER, "dct": TENANT_B}))

        assert response.status_code == 403
        assert response.json() == NO_TENANT
        assert user_service.mock_calls == []

    def test_a_dct_and_a_header_together_are_refused_with_several_memberships(self, client, store, user_service):
        store.roles = MEMBER_OF_A_OPERATOR_OF_B

        response = client.get(
            "/api/users", headers={**_bearer({**ORY_OWNER, "dct": TENANT_B}), "X-Tenant-ID": TENANT_B}
        )

        assert response.status_code == 403
        assert response.json() == NO_TENANT
        assert user_service.mock_calls == []

    def test_a_sole_membership_without_the_role_is_refused(self, client, store, user_service):
        response = client.get("/api/users", headers=_bearer(ORY_OWNER))

        assert response.status_code == 403
        assert response.json() == NOT_GRANTED
        assert user_service.mock_calls == []

    def test_a_descope_operator_is_served_from_its_claims(self, client, store):
        response = client.get("/api/users", headers={**_bearer(DESCOPE_OPERATOR), "X-Tenant-ID": TENANT_B})

        assert response.status_code == 200
        assert response.json() == {"users": []}
        assert store.calls == []

    def test_a_descope_member_is_refused(self, client, user_service):
        response = client.get("/api/users", headers=_bearer(DESCOPE_MEMBER))

        assert response.status_code == 403
        assert response.json() == NOT_GRANTED
        assert user_service.mock_calls == []

    def test_the_refusal_is_logged_with_the_caller_and_not_the_tenant(self, client, store, caplog):
        store.roles = MEMBER_OF_A_OPERATOR_OF_B

        with caplog.at_level("WARNING", logger="app.dependencies.tenant_access"):
            client.get("/api/users", headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_B})

        (message,) = [
            record.getMessage() for record in caplog.records if record.name == "app.dependencies.tenant_access"
        ]
        assert message == (
            f"tenant selection refused: provider=Ory sub={ORY_SUB!r} named one of several tenants "
            "on a route whose state belongs to no tenant"
        )


def _hand_built_request(**state: object) -> SimpleNamespace:
    """A request no middleware touched: only what the caller puts on ``request.state``."""
    return SimpleNamespace(state=SimpleNamespace(**state))


class TestClaimsWithNoProviderAreRefused:
    """The guard passes a principal it knows to be Descope's, never one it cannot place."""

    @pytest.mark.parametrize(
        "state",
        [
            {"claims": DESCOPE_ADMIN},
            {"claims": DESCOPE_ADMIN, "provider": None},
            {"claims": DESCOPE_ADMIN, "provider": "Descope"},
        ],
        ids=["no provider", "provider is None", "provider is only a name"],
    )
    def test_claims_that_no_provider_is_recorded_for(self, state):
        with pytest.raises(HTTPException) as refused:
            require_descope_principal(_hand_built_request(**state))

        assert refused.value.status_code == 403
        assert refused.value.detail == REFUSED["detail"]

    def test_the_refusal_is_logged_with_the_subject(self, caplog):
        with caplog.at_level("WARNING", logger="app.dependencies.descope"), pytest.raises(HTTPException):
            require_descope_principal(_hand_built_request(claims=DESCOPE_ADMIN))

        (message,) = [record.getMessage() for record in caplog.records if record.name == "app.dependencies.descope"]
        assert message == (
            "refused: provider=unrecorded sub='descope-admin' called a route that serves Descope principals only"
        )

    def test_a_descope_principal_passes(self):
        request = _hand_built_request(claims=DESCOPE_ADMIN, provider=descope_config(DESCOPE_PROJECT))

        assert require_descope_principal(request) is None

    @pytest.mark.parametrize("state", [{}, {"claims": None}], ids=["no claims", "claims is None"])
    def test_a_request_with_no_claims_is_refused_by_the_guard_itself(self, state):
        """Conventions §17: the guard does not lean on a second dependency to answer 401."""
        with pytest.raises(HTTPException) as refused:
            require_descope_principal(_hand_built_request(**state))

        assert refused.value.status_code == 401
        assert refused.value.detail == "Not authenticated"

    def test_a_request_with_no_token_gets_401_from_a_guarded_route(self, client):
        response = client.get("/api/documents")

        assert response.status_code == 401
