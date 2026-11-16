"""One resolver answers "which tenant, which roles, which permissions" for a request.

The order (conventions §18): the provider's claims extractor; for a provider
authorized from the canonical model, the request's tenant selector checked
against the caller's memberships; then the caller's sole membership. Anything
else is 403.

Every request here goes through the shipped gateway middleware and the shipped
dependencies. Only the canonical store is a stub: it answers for one
``(provider, sub)`` and records each lookup, so a test can show that no other
subject or tenant was ever read.
"""

import base64
import json
import time
from dataclasses import replace

import pytest
from expression import Error, Ok
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.dependencies import rbac
from app.dependencies.rbac import get_rbac_session, require_permission, require_role
from app.dependencies.tenant import get_tenant_id
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.errors.identity import NotFound
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    ProviderTokenConfig,
    TenantClaims,
    descope_config,
    keycloak_config,
    ory_config,
)

DESCOPE_PROJECT = "P123"
DESCOPE_ISSUER = f"https://api.descope.com/{DESCOPE_PROJECT}"
ORY_ISSUER = "https://ory.example.test"
ORY_AUDIENCE = "https://identity-stack-api"
KEYCLOAK_ISSUER = "https://keycloak.example.test/realms/identity-stack"
KEYCLOAK_AUDIENCE = "member"
FAKE_ISSUER = "https://fake.example.test"

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
TENANT_C = "33333333-3333-3333-3333-333333333333"
ORY_SUB = "ory-subject-1"

NO_TENANT_FROM_GET_TENANT_ID = {"detail": "No tenant context — select a tenant first"}
NO_TENANT_FROM_RBAC = {"detail": "No tenant context"}
INSUFFICIENT = {"detail": "Insufficient permissions"}


def _fake_tenant_claims(claims: dict) -> TenantClaims:
    """A test-only source: the tenant is ``org``, roles and permissions sit beside it."""
    return TenantClaims(
        tenant_id=claims.get("org"),
        roles=list(claims.get("org_roles", [])),
        permissions=list(claims.get("org_permissions", [])),
    )


FAKE_PROVIDER = ProviderTokenConfig(
    name="Fake",
    accepted_issuers=frozenset({FAKE_ISSUER}),
    disco_address=f"{FAKE_ISSUER}/.well-known/openid-configuration",
    tenant_claims=_fake_tenant_claims,
)

PROVIDERS = [
    descope_config(DESCOPE_PROJECT),
    ory_config(ORY_ISSUER, ORY_AUDIENCE),
    keycloak_config(KEYCLOAK_ISSUER, frozenset({KEYCLOAK_AUDIENCE})),
    FAKE_PROVIDER,
]


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    """An unsigned JWT: gateway mode reads the payload the gateway already verified."""
    return f"{_segment({'alg': 'RS256'})}.{_segment({'exp': int(time.time()) + 300, **claims})}.sig"


def _ory(**claims) -> dict:
    return {"iss": ORY_ISSUER, "aud": ORY_AUDIENCE, "sub": ORY_SUB, **claims}


def _descope(**claims) -> dict:
    return {"iss": DESCOPE_ISSUER, "sub": "descope-user", **claims}


def _keycloak(**claims) -> dict:
    return {"iss": KEYCLOAK_ISSUER, "aud": KEYCLOAK_AUDIENCE, "azp": "member-app", "sub": "kc-user", **claims}


def _fake(**claims) -> dict:
    return {"iss": FAKE_ISSUER, "sub": "fake-user", **claims}


class _CanonicalStore:
    """Stands in for IdentityResolutionService: canned result, every lookup recorded."""

    def __init__(self) -> None:
        self.result = Error(NotFound("no identity"))
        self.calls: list[tuple[str, str]] = []

    def holds(self, *grants: tuple[str, str, list[str]], status: str = "active") -> None:
        self.result = Ok(
            {
                "user": {"id": "canonical-user-1", "status": status},
                "roles": [
                    {"tenant_id": tenant_id, "role_name": role, "permissions": list(permissions)}
                    for tenant_id, role, permissions in grants
                ],
            }
        )

    async def resolve(self, *, provider: str, sub: str):
        self.calls.append((provider, sub))
        return self.result


@pytest.fixture
def store(monkeypatch) -> _CanonicalStore:
    canonical = _CanonicalStore()
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: canonical)
    return canonical


def _app(*, database: bool = True, providers: list[ProviderTokenConfig] | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/tenant")
    async def tenant(tenant_id: str = Depends(get_tenant_id)):
        return {"tenant_id": tenant_id}

    @app.get("/roles")
    async def roles(
        tenant_id: str = Depends(get_tenant_id),
        held: list[str] = Depends(require_role("admin", "member", "operator")),
    ):
        return {"tenant_id": tenant_id, "roles": sorted(held)}

    @app.get("/admin")
    async def admin(held: list[str] = Depends(require_role("admin"))):
        return {"roles": sorted(held)}

    @app.get("/permissions")
    async def permissions(held: list[str] = Depends(require_permission("documents.read"))):
        return {"permissions": sorted(held)}

    async def _session():
        yield object() if database else None

    app.dependency_overrides[get_rbac_session] = _session
    app.add_middleware(GatewayClaimsMiddleware, providers=PROVIDERS if providers is None else providers)
    return app


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(_app())


def _get(client: TestClient, path: str, claims: dict, *, selector: str | None = None, extra_headers=()):
    headers = [("Authorization", f"Bearer {_jwt(claims)}"), *extra_headers]
    if selector is not None:
        headers.append((TENANT_SELECTOR_HEADER, selector))
    return client.get(path, headers=headers)


class TestTheSelectorHeader:
    def test_it_is_x_tenant_id(self):
        assert TENANT_SELECTOR_HEADER == "X-Tenant-ID"


class TestSoleCanonicalMembership:
    def test_get_tenant_id_returns_the_one_tenant_instead_of_403(self, client, store):
        store.holds((TENANT_A, "member", ["documents.read"]))

        response = _get(client, "/tenant", _ory())

        assert response.status_code == 200
        assert response.json() == {"tenant_id": TENANT_A}
        assert store.calls == [("ory", ORY_SUB)]

    def test_several_roles_in_one_tenant_are_still_one_membership(self, client, store):
        store.holds((TENANT_A, "member", []), (TENANT_A, "admin", []))

        response = _get(client, "/roles", _ory())

        assert response.json() == {"tenant_id": TENANT_A, "roles": ["admin", "member"]}

    def test_no_membership_is_refused(self, client, store):
        store.holds()

        response = _get(client, "/tenant", _ory())

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_GET_TENANT_ID

    def test_two_memberships_and_no_selector_are_refused(self, client, store):
        store.holds((TENANT_A, "admin", []), (TENANT_B, "member", []))

        tenant = _get(client, "/tenant", _ory())
        admin = _get(client, "/admin", _ory())

        assert tenant.status_code == 403
        assert tenant.json() == NO_TENANT_FROM_GET_TENANT_ID
        assert admin.status_code == 403
        assert admin.json() == NO_TENANT_FROM_RBAC


class TestGetTenantIdFailsClosedOnTheCanonicalPath:
    def test_no_canonical_identity(self, client, store):
        response = _get(client, "/tenant", _ory())

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC

    @pytest.mark.parametrize("status", ["inactive", "provisioned", "anything-else"])
    def test_a_user_who_is_not_active(self, client, store, status):
        store.holds((TENANT_A, "member", []), status=status)

        response = _get(client, "/tenant", _ory())

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC

    def test_no_database(self, store):
        store.holds((TENANT_A, "member", []))

        response = _get(TestClient(_app(database=False)), "/tenant", _ory())

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC
        assert store.calls == []

    def test_a_token_without_a_subject_is_unauthenticated(self, client, store):
        store.holds((TENANT_A, "member", []))

        response = _get(client, "/tenant", _ory(sub=None))

        assert response.status_code == 401
        assert response.json() == {"detail": "Token missing subject"}


class TestTenantSelector:
    def test_it_chooses_among_the_callers_memberships(self, client, store):
        store.holds((TENANT_A, "admin", ["documents.write"]), (TENANT_B, "member", ["documents.read"]))

        in_a = _get(client, "/roles", _ory(), selector=TENANT_A)
        in_b = _get(client, "/roles", _ory(), selector=TENANT_B)

        assert in_a.json() == {"tenant_id": TENANT_A, "roles": ["admin"]}
        assert in_b.json() == {"tenant_id": TENANT_B, "roles": ["member"]}

    def test_grants_come_only_from_the_selected_tenant(self, client, store):
        store.holds((TENANT_A, "admin", ["documents.read"]), (TENANT_B, "member", []))

        admin = _get(client, "/admin", _ory(), selector=TENANT_B)
        permissions = _get(client, "/permissions", _ory(), selector=TENANT_B)

        assert admin.status_code == 403
        assert admin.json() == INSUFFICIENT
        assert permissions.status_code == 403
        assert permissions.json() == INSUFFICIENT

    def test_a_tenant_the_caller_does_not_belong_to_is_refused(self, client, store):
        store.holds((TENANT_A, "admin", ["documents.read"]), (TENANT_B, "member", []))

        tenant = _get(client, "/tenant", _ory(), selector=TENANT_C)
        admin = _get(client, "/admin", _ory(), selector=TENANT_C)
        permissions = _get(client, "/permissions", _ory(), selector=TENANT_C)

        assert tenant.status_code == 403
        assert tenant.json() == NO_TENANT_FROM_GET_TENANT_ID
        assert admin.status_code == 403
        assert admin.json() == NO_TENANT_FROM_RBAC
        assert permissions.status_code == 403
        assert permissions.json() == NO_TENANT_FROM_RBAC

    def test_a_refused_selector_reads_no_grant_from_the_tenant_it_names(self, client, store):
        """The only lookups are the caller's own: nothing is read by tenant id."""
        store.holds((TENANT_A, "admin", ["documents.read"]))

        _get(client, "/admin", _ory(), selector=TENANT_C)

        assert store.calls == [("ory", ORY_SUB)]

    def test_a_refused_selector_does_not_fall_back_to_the_sole_membership(self, client, store):
        store.holds((TENANT_A, "admin", ["documents.read"]))

        response = _get(client, "/admin", _ory(), selector=TENANT_C)

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC

    def test_surrounding_whitespace_is_not_part_of_the_tenant_id(self, client, store):
        store.holds((TENANT_A, "admin", []), (TENANT_B, "member", []))

        response = _get(client, "/roles", _ory(), selector=f"  {TENANT_B} ")

        assert response.json() == {"tenant_id": TENANT_B, "roles": ["member"]}

    @pytest.mark.parametrize("selector", ["", "   "])
    def test_an_empty_selector_is_refused_even_with_one_membership(self, client, store, selector):
        store.holds((TENANT_A, "admin", []))

        response = _get(client, "/admin", _ory(), selector=selector)

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC

    def test_a_repeated_selector_is_refused(self, client, store):
        """Two values are two candidate tenants, whichever one a proxy would have kept."""
        store.holds((TENANT_A, "admin", []), (TENANT_B, "member", []))

        response = _get(client, "/admin", _ory(), selector=TENANT_A, extra_headers=[(TENANT_SELECTOR_HEADER, TENANT_B)])

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC

    def test_a_selector_and_a_dct_that_agree_select_that_tenant(self, client, store):
        store.holds((TENANT_A, "admin", []), (TENANT_B, "member", []))

        response = _get(client, "/roles", _ory(dct=TENANT_B), selector=TENANT_B)

        assert response.json() == {"tenant_id": TENANT_B, "roles": ["member"]}

    def test_a_selector_and_a_dct_that_disagree_are_refused(self, client, store):
        store.holds((TENANT_A, "admin", []), (TENANT_B, "member", []))

        response = _get(client, "/admin", _ory(dct=TENANT_B), selector=TENANT_A)

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC


class TestOneCanonicalReadPerRequest:
    def test_two_dependencies_on_one_route_share_the_lookup(self, client, store):
        store.holds((TENANT_A, "admin", []))

        response = _get(client, "/roles", _ory())

        assert response.status_code == 200
        assert store.calls == [("ory", ORY_SUB)]

    def test_the_lookup_is_not_shared_between_requests(self, client, store):
        store.holds((TENANT_A, "admin", []))
        _get(client, "/admin", _ory())
        store.holds((TENANT_A, "member", []))

        response = _get(client, "/admin", _ory())

        assert response.status_code == 403
        assert response.json() == INSUFFICIENT
        assert store.calls == [("ory", ORY_SUB), ("ory", ORY_SUB)]


class TestDescopeIsDecidedFromItsOwnClaims:
    CLAIMS = {
        "dct": "t1",
        "tenants": {
            "t1": {"roles": ["member"], "permissions": ["documents.read"]},
            "t2": {"roles": ["admin"], "permissions": []},
        },
    }

    def test_the_selector_does_not_move_a_descope_principal(self, client, store):
        store.holds(("t2", "admin", []))

        roles = _get(client, "/roles", _descope(**self.CLAIMS), selector="t2")
        admin = _get(client, "/admin", _descope(**self.CLAIMS), selector="t2")

        assert roles.json() == {"tenant_id": "t1", "roles": ["member"]}
        assert admin.status_code == 403
        assert admin.json() == INSUFFICIENT
        assert store.calls == []

    def test_no_dct_is_still_refused_whatever_the_selector_and_the_store_say(self, client, store):
        store.holds(("t1", "admin", []))
        claims = _descope(tenants={"t1": {"roles": ["admin"]}, "t2": {"roles": ["admin"]}})

        tenant = _get(client, "/tenant", claims, selector="t1")
        admin = _get(client, "/admin", claims, selector="t1")

        assert tenant.status_code == 403
        assert tenant.json() == NO_TENANT_FROM_GET_TENANT_ID
        assert admin.status_code == 403
        assert admin.json() == NO_TENANT_FROM_RBAC
        assert store.calls == []

    def test_permissions_come_from_the_current_tenant(self, client, store):
        response = _get(client, "/permissions", _descope(**self.CLAIMS))

        assert response.json() == {"permissions": ["documents.read"]}


class TestTheSourceIsTheProvidersOwn:
    def test_a_registered_extractor_supplies_tenant_roles_and_permissions(self, client, store):
        claims = _fake(org="org-1", org_roles=["admin"], org_permissions=["documents.read"])

        roles = _get(client, "/roles", claims)
        permissions = _get(client, "/permissions", claims)

        assert roles.json() == {"tenant_id": "org-1", "roles": ["admin"]}
        assert permissions.json() == {"permissions": ["documents.read"]}
        assert store.calls == []

    def test_another_sources_claim_shapes_are_not_read(self, client, store):
        """``dct`` and ``tenants`` on a token from a provider with its own extractor grant nothing."""
        claims = _fake(dct="t1", tenants={"t1": {"roles": ["admin"], "permissions": ["documents.read"]}})

        tenant = _get(client, "/tenant", claims)
        admin = _get(client, "/admin", claims)

        assert tenant.status_code == 403
        assert tenant.json() == NO_TENANT_FROM_GET_TENANT_ID
        assert admin.status_code == 403
        assert admin.json() == NO_TENANT_FROM_RBAC

    def test_a_provider_with_no_source_is_refused(self, client, store):
        """Keycloak has no extractor yet and is not authorized from the canonical model."""
        store.holds((TENANT_A, "admin", ["documents.read"]))
        claims = _keycloak(dct="t1", tenants={"t1": {"roles": ["admin"], "permissions": ["documents.read"]}})

        tenant = _get(client, "/tenant", claims, selector=TENANT_A)
        admin = _get(client, "/admin", claims, selector=TENANT_A)
        permissions = _get(client, "/permissions", claims, selector=TENANT_A)

        assert tenant.status_code == 403
        assert tenant.json() == NO_TENANT_FROM_GET_TENANT_ID
        assert admin.status_code == 403
        assert admin.json() == NO_TENANT_FROM_RBAC
        assert permissions.status_code == 403
        assert permissions.json() == NO_TENANT_FROM_RBAC
        assert store.calls == []

    def test_a_canonical_provider_ignores_its_own_extractor(self, store, monkeypatch):
        """Listing a provider in CANONICAL_RBAC_PROVIDERS makes the canonical model its only source."""
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "fake"}))
        store.holds((TENANT_A, "member", []))
        claims = _fake(org="org-1", org_roles=["admin"])

        response = _get(TestClient(_app()), "/roles", claims)

        assert response.json() == {"tenant_id": TENANT_A, "roles": ["member"]}
        assert store.calls == [("fake", "fake-user")]

    def test_an_extractor_that_names_no_tenant_is_refused(self, client, store):
        tenant = _get(client, "/tenant", _fake(org_roles=["admin"]))
        admin = _get(client, "/admin", _fake(org_roles=["admin"]))

        assert tenant.status_code == 403
        assert tenant.json() == NO_TENANT_FROM_GET_TENANT_ID
        assert admin.status_code == 403
        assert admin.json() == NO_TENANT_FROM_RBAC


class TestProviderRecordedByTheMiddleware:
    def test_the_resolver_goes_by_the_recorded_provider_not_the_claims(self, store):
        """Descope's extractor is not the fallback for a provider the middleware attributed."""
        providers = [descope_config(DESCOPE_PROJECT), replace(FAKE_PROVIDER, tenant_claims=None)]
        claims = _fake(dct="t1", tenants={"t1": {"roles": ["admin"]}})

        response = _get(TestClient(_app(providers=providers)), "/admin", claims)

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC
