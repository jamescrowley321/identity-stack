"""The routes that hand a tenant id or a subject to Descope serve Descope principals only.

``get_tenant_id`` gives a principal from a canonical provider a canonical tenant
id (conventions §18). The routes below pass their tenant id and the caller's
``sub`` to Descope's Management API or its FGA store, where a canonical id names
nothing, so ``require_descope_principal`` refuses every other provider's
principal on them before anything is read.

Every tenant-scoped route is in exactly one of the two registers here. A new
route that depends on ``get_tenant_id`` fails this file until it is put in one.

The routes over project-wide state that Descope holds or mirrors (the role and
permission definitions, the FGA schema) take no tenant from ``get_tenant_id``
but carry the same guard: a canonical principal who is an admin of any one of
their tenants would otherwise change every tenant's definitions in Descope.

The routes that take the caller's ``sub`` to Descope and no tenant (the profile)
are held the same way: every route that is handed the Descope management adapter
carries the guard, or is listed with what keeps another provider's subject from
reaching Descope.

The routes over state that belongs to no tenant and that Descope does not hold
(every tenant's users, the provider records, the sync status) are gated by
``require_global_role``, which lets no caller pick the tenant that answers with
the ``X-Tenant-ID`` header. Every role-gated route is held to one of these
shapes, so a new one has to choose.
"""

import base64
import json
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Ok
from fastapi import Depends, FastAPI, HTTPException
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies import rbac
from app.dependencies.descope import NOT_A_DESCOPE_PRINCIPAL, get_descope_management, require_descope_principal
from app.dependencies.identity import (
    get_permission_service,
    get_provider_service,
    get_role_service,
    get_sync_status_service,
    get_user_service,
)
from app.dependencies.rbac import get_rbac_session, require_global_role, require_permission, require_role
from app.dependencies.tenant import get_tenant_id
from app.dependencies.tenant_access import SELECT_A_TENANT_FIRST
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import ProviderTokenConfig, TenantClaims, descope_config, ory_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from app.services.permission import PermissionService
from app.services.provider import ProviderService
from app.services.role import RoleService
from app.services.sync_status import SyncStatusService
from app.services.user import UserService

DESCOPE_PROJECT = "P123"
DESCOPE_ISSUER = f"https://api.descope.com/{DESCOPE_PROJECT}"
ORY_ISSUER = "https://ory.example.test"
ORY_AUDIENCE = "https://identity-stack-api"
FAKE_ISSUER = "https://fake.example.test"

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
RESOURCE_ID = "33333333-3333-3333-3333-333333333333"
ORY_SUB = "ory-subject-1"

REFUSED = {"detail": "This route serves Descope principals only"}
NO_TENANT = {"detail": "No tenant context"}
NOT_GRANTED = {"detail": "Insufficient permissions"}

# (method, path) of every route that hands the tenant or the subject to Descope.
DESCOPE_TENANT_ROUTES: frozenset[tuple[str, str]] = frozenset(
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

# (method, path) of every guarded route over state that belongs to no tenant:
# ``RoleService`` and ``PermissionService`` mirror each definition to Descope, and
# the FGA schema is the Descope project's own.
DESCOPE_PROJECT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/roles"),
        ("POST", "/api/roles"),
        ("PUT", "/api/roles/{name}"),
        ("DELETE", "/api/roles/{name}"),
        ("GET", "/api/permissions"),
        ("POST", "/api/permissions"),
        ("PUT", "/api/permissions/{name}"),
        ("DELETE", "/api/permissions/{name}"),
        ("GET", "/api/fga/schema"),
        ("PUT", "/api/fga/schema"),
    }
)

# (method, path) of every guarded route that hands Descope the caller's ``sub`` and no tenant.
DESCOPE_SUBJECT_ROUTES: frozenset[tuple[str, str]] = frozenset({("PATCH", "/api/profile")})

DESCOPE_BACKED_ROUTES = DESCOPE_TENANT_ROUTES | DESCOPE_PROJECT_ROUTES | DESCOPE_SUBJECT_ROUTES

# (method, path) -> what keeps another provider's subject from Descope on a route
# that is handed the management adapter and carries no guard.
UNGUARDED_ADAPTER_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/profile"): (
        "every provider's SPA session loads it: the handler asks Descope for a Descope principal only "
        "and answers anyone else with the empty profile"
    ),
}

# (method, path) -> why a principal from any provider may use the route's tenant.
# A route belongs here only when nothing behind it, its service's sync adapter
# included, hands the tenant or a user id to Descope. None does yet.
PROVIDER_NEUTRAL_ROUTES: dict[tuple[str, str], str] = {}


# (method, path) of every route over state that belongs to no tenant and is not
# Descope's: one role in one tenant opens all of it, so the header picks nothing.
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
    ("GET", "/api/users/{user_id}/idp-links"): "_require_user_in_tenant holds the user to the token's dct (#590)",
    ("POST", "/api/users/{user_id}/idp-links"): "_require_user_in_tenant holds the user to the token's dct (#590)",
    ("DELETE", "/api/users/{user_id}/idp-links/{link_id}"): (
        "_require_user_in_tenant holds the user to the token's dct (#590)"
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
FAKE_ADMIN = {"iss": FAKE_ISSUER, "sub": "fake-user", "org": "org-1", "org_roles": ["owner"]}


SOLE_OWNER = [{"tenant_id": TENANT_A, "role_name": "owner", "permissions": ["documents.read"]}]
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
        providers=[descope_config(DESCOPE_PROJECT), ory_config(ORY_ISSUER, ORY_AUDIENCE), FAKE_PROVIDER],
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
        assert _routes_depending_on(get_tenant_id) == DESCOPE_TENANT_ROUTES | set(PROVIDER_NEUTRAL_ROUTES)

    def test_no_project_route_takes_a_tenant(self):
        assert DESCOPE_TENANT_ROUTES & DESCOPE_PROJECT_ROUTES == set()

    def test_no_subject_route_takes_a_tenant(self):
        assert DESCOPE_SUBJECT_ROUTES & _routes_depending_on(get_tenant_id) == set()

    def test_every_route_handed_the_management_adapter_is_guarded_or_says_why(self):
        """A new route that depends on ``get_descope_management`` alone fails here until it chooses."""
        handed_the_adapter = _routes_depending_on(get_descope_management)

        assert handed_the_adapter - _routes_depending_on(require_descope_principal) == set(UNGUARDED_ADAPTER_ROUTES)

    def test_the_refusal_is_not_the_tenant_resolvers(self):
        """Selecting a tenant would not help the refused caller, so the body does not say to."""
        assert REFUSED == {"detail": NOT_A_DESCOPE_PRINCIPAL}
        assert NOT_A_DESCOPE_PRINCIPAL != SELECT_A_TENANT_FIRST

    def test_no_route_is_in_both(self):
        assert DESCOPE_BACKED_ROUTES & set(PROVIDER_NEUTRAL_ROUTES) == set()

    def test_the_global_gate_is_on_exactly_the_global_routes(self):
        assert _routes_gated_by(require_global_role) == GLOBAL_ROLE_ROUTES

    def test_a_global_route_has_no_tenant_gate_beside_it(self):
        tenant_scoped = _routes_depending_on(get_tenant_id) | _routes_gated_by(require_role, require_permission)

        assert GLOBAL_ROLE_ROUTES & tenant_scoped == set()

    def test_every_tenant_gated_route_resolves_its_tenant_or_is_guarded_or_says_why(self):
        """A new route behind ``require_role`` alone fails here until it chooses."""
        gated = _routes_gated_by(require_role, require_permission)
        accounted_for = _routes_depending_on(get_tenant_id) | _routes_depending_on(require_descope_principal)

        assert gated - accounted_for == set(SELF_SCOPED_ROLE_ROUTES)


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


class TestTheProfileTakesNoOtherProvidersSubjectToDescope:
    """``/api/profile`` hands Descope the caller's ``sub``, which is Descope's only for its own principal."""

    EMPTY_ORY_PROFILE = {"user_id": ORY_SUB, "name": "", "email": "", "custom_attributes": {}}

    def test_an_ory_principal_reads_an_empty_profile_and_descope_is_not_asked(self, client, descope_client):
        """What an Ory session got before, when Descope answered that it knew no such user."""
        response = client.get("/api/profile", headers=_bearer(ORY_OWNER))

        assert response.status_code == 200
        assert response.json() == self.EMPTY_ORY_PROFILE
        assert descope_client.mock_calls == []

    def test_another_claims_provider_reads_an_empty_profile_too(self, client, descope_client):
        response = client.get("/api/profile", headers=_bearer(FAKE_ADMIN))

        assert response.status_code == 200
        assert response.json() == {**self.EMPTY_ORY_PROFILE, "user_id": FAKE_ADMIN["sub"]}
        assert descope_client.mock_calls == []

    def test_a_descope_principal_reads_its_profile_from_descope(self, client, descope_client):
        descope_client.load_user.return_value = {
            "name": "A Member",
            "email": "member@example.test",
            "customAttributes": {"department": "Engineering"},
        }

        response = client.get("/api/profile", headers=_bearer(DESCOPE_MEMBER))

        assert response.status_code == 200
        assert response.json() == {
            "user_id": DESCOPE_MEMBER["sub"],
            "name": "A Member",
            "email": "member@example.test",
            "custom_attributes": {"department": "Engineering"},
        }
        descope_client.load_user.assert_awaited_once_with(DESCOPE_MEMBER["sub"])

    def test_an_ory_principal_cannot_write_an_attribute(self, client, descope_client):
        """A well-formed body with an allowed key: only the guard refuses it."""
        response = client.patch(
            "/api/profile", json={"key": "department", "value": "Engineering"}, headers=_bearer(ORY_OWNER)
        )

        assert response.status_code == 403
        assert response.json() == REFUSED
        assert descope_client.mock_calls == []

    def test_a_descope_principal_writes_its_attribute(self, client, descope_client):
        descope_client.resolve_login_id.return_value = "member@example.test"

        response = client.patch(
            "/api/profile", json={"key": "department", "value": "Engineering"}, headers=_bearer(DESCOPE_MEMBER)
        )

        assert response.status_code == 200
        assert response.json() == {"status": "updated", "key": "department", "value": "Engineering"}
        descope_client.update_user_custom_attribute.assert_awaited_once_with(
            "member@example.test", "department", "Engineering"
        )

    def test_a_request_with_no_token_gets_401(self, client):
        assert client.get("/api/profile").status_code == 401
        assert client.patch("/api/profile", json={"key": "department", "value": "x"}).status_code == 401


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


class TestProjectWideStateIsDescopeBacked:
    """Role and permission definitions and the FGA schema belong to no tenant and live in Descope."""

    @pytest.mark.parametrize(("method", "path"), sorted(DESCOPE_PROJECT_ROUTES))
    def test_an_admin_of_one_of_several_tenants_is_refused(
        self, client, store, descope_client, role_service, permission_service, method, path
    ):
        """Naming the tenant the caller administers would pass ``require_role``: only the guard refuses it."""
        store.roles = MEMBER_OF_A_ADMIN_OF_B

        response = client.request(method, _url(path), headers={**_bearer(ORY_OWNER), "X-Tenant-ID": TENANT_B})

        assert response.status_code == 403
        assert response.json() == REFUSED
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
        """The header picks nothing here, but a request that names the caller's one tenant is not wrong."""
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

    def test_a_dct_on_the_token_selects_as_it_did_before_the_header(self, client, store):
        store.roles = MEMBER_OF_A_OPERATOR_OF_B

        response = client.get("/api/users", headers=_bearer({**ORY_OWNER, "dct": TENANT_B}))

        assert response.status_code == 200
        assert response.json() == {"users": []}

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
