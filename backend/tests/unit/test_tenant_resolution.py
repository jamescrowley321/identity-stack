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
from pathlib import Path
from types import SimpleNamespace

import pytest
from expression import Error, Ok
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app.dependencies import rbac
from app.dependencies.rbac import RbacSessionOverrideError, get_rbac_session, require_permission, require_role
from app.dependencies.tenant import get_tenant_id
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER, resolve_tenant_access
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
# One with hex letters, so its upper-case spelling differs from the stored one.
TENANT_D = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
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


def _app(
    *,
    database: bool = True,
    providers: list[ProviderTokenConfig] | None = None,
    opened: list[str] | None = None,
) -> FastAPI:
    """``opened`` gets one entry each time the canonical-resolution session is opened."""
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
        if opened is not None:
            opened.append("session")
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

    @pytest.mark.parametrize(
        "spelling",
        [
            TENANT_D.upper(),
            f"{{{TENANT_D}}}",
            f"urn:uuid:{TENANT_D}",
            TENANT_D.replace("-", ""),
        ],
    )
    def test_another_spelling_of_the_same_uuid_selects_the_same_tenant(self, client, store, spelling):
        store.holds((TENANT_A, "admin", []), (TENANT_D, "member", []))

        response = _get(client, "/roles", _ory(), selector=spelling)

        assert response.status_code == 200
        assert response.json() == {"tenant_id": TENANT_D, "roles": ["member"]}

    def test_another_spelling_of_a_tenant_the_caller_is_not_in_is_still_refused(self, client, store):
        store.holds((TENANT_A, "admin", []))

        response = _get(client, "/admin", _ory(), selector=TENANT_D.upper())

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC

    def test_a_selector_that_is_not_a_uuid_is_compared_as_it_is(self, client, store):
        store.holds(("tenant-plain", "admin", []), (TENANT_A, "member", []))

        exact = _get(client, "/roles", _ory(), selector="tenant-plain")
        other_case = _get(client, "/roles", _ory(), selector="TENANT-PLAIN")

        assert exact.json() == {"tenant_id": "tenant-plain", "roles": ["admin"]}
        assert other_case.status_code == 403
        assert other_case.json() == NO_TENANT_FROM_GET_TENANT_ID

    def test_an_upper_case_selector_and_a_dct_for_the_same_tenant_agree(self, client, store):
        store.holds((TENANT_A, "admin", []), (TENANT_D, "member", []))

        response = _get(client, "/roles", _ory(dct=TENANT_D), selector=TENANT_D.upper())

        assert response.json() == {"tenant_id": TENANT_D, "roles": ["member"]}

    def test_an_upper_case_dct_names_the_tenant_as_the_header_would(self, client, store):
        store.holds((TENANT_A, "admin", []), (TENANT_D, "member", []))

        response = _get(client, "/roles", _ory(dct=TENANT_D.upper()))

        assert response.json() == {"tenant_id": TENANT_D, "roles": ["member"]}

    def test_an_upper_case_dct_and_a_selector_for_the_same_tenant_agree(self, client, store):
        store.holds((TENANT_A, "admin", []), (TENANT_D, "member", []))

        response = _get(client, "/roles", _ory(dct=TENANT_D.upper()), selector=TENANT_D)

        assert response.json() == {"tenant_id": TENANT_D, "roles": ["member"]}

    def test_an_upper_case_dct_for_a_tenant_the_caller_is_not_in_is_still_refused(self, client, store):
        store.holds((TENANT_A, "admin", []))

        response = _get(client, "/admin", _ory(dct=TENANT_D.upper()))

        assert response.status_code == 403
        assert response.json() == NO_TENANT_FROM_RBAC

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

    def test_a_selector_repeated_with_one_value_is_refused_too(self, client, store):
        store.holds((TENANT_A, "admin", []), (TENANT_B, "member", []))

        response = _get(client, "/admin", _ory(), selector=TENANT_A, extra_headers=[(TENANT_SELECTOR_HEADER, TENANT_A)])

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


class TestTheCanonicalSessionIsOpenedOnlyWhenItIsRead:
    """``get_tenant_id`` costs a claims-decided request no session and no canonical read."""

    DESCOPE_CLAIMS = {"dct": "t1", "tenants": {"t1": {"roles": ["member"]}}}

    def test_a_descope_request_opens_no_session_and_reads_nothing(self, store):
        opened: list[str] = []

        response = _get(TestClient(_app(opened=opened)), "/tenant", _descope(**self.DESCOPE_CLAIMS))

        assert response.json() == {"tenant_id": "t1"}
        assert opened == []
        assert store.calls == []

    def test_a_provider_with_its_own_extractor_opens_no_session(self, store):
        opened: list[str] = []

        response = _get(TestClient(_app(opened=opened)), "/tenant", _fake(org="org-1"))

        assert response.json() == {"tenant_id": "org-1"}
        assert opened == []
        assert store.calls == []

    def test_an_unauthorized_provider_opens_no_session(self, store):
        opened: list[str] = []

        response = _get(TestClient(_app(opened=opened)), "/tenant", _keycloak())

        assert response.status_code == 403
        assert opened == []

    def test_a_canonical_request_opens_one_session_for_its_one_read(self, store):
        store.holds((TENANT_A, "member", []))
        opened: list[str] = []

        response = _get(TestClient(_app(opened=opened)), "/tenant", _ory())

        assert response.json() == {"tenant_id": TENANT_A}
        assert opened == ["session"]
        assert store.calls == [("ory", ORY_SUB)]


async def _session_that_is_not_a_generator():
    return object()


async def _session_that_wants_an_argument(request):
    yield object()


class TestAnOverrideOfTheCanonicalSessionMustHaveItsShape:
    """``get_tenant_id`` calls the override itself, so it says so when it cannot."""

    @pytest.mark.parametrize(
        "override",
        [_session_that_is_not_a_generator, _session_that_wants_an_argument],
        ids=["not an async generator", "takes an argument"],
    )
    def test_an_override_of_another_shape_is_named_in_a_typed_error(self, store, override):
        store.holds((TENANT_A, "member", []))
        app = _app()
        app.dependency_overrides[get_rbac_session] = override

        with pytest.raises(RbacSessionOverrideError, match=override.__name__):
            _get(TestClient(app), "/tenant", _ory())

        assert store.calls == []

    def test_a_claims_decided_request_never_calls_the_override(self, store):
        app = _app()
        app.dependency_overrides[get_rbac_session] = _session_that_is_not_a_generator

        response = _get(TestClient(app), "/tenant", _descope(dct="t1", tenants={"t1": {"roles": ["member"]}}))

        assert response.json() == {"tenant_id": "t1"}


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

    @pytest.mark.parametrize("dct", [7, ["t1"], {"t1": True}], ids=["number", "list", "object"])
    def test_a_dct_that_is_not_a_string_names_no_tenant_on_every_dependency(self, client, store, dct):
        """The role and permission checks answer as they do for a token with no ``dct``."""
        grants = {"roles": ["admin"], "permissions": ["documents.read"]}
        claims = _descope(dct=dct, tenants={"t1": grants, "7": grants})

        tenant = _get(client, "/tenant", claims)
        admin = _get(client, "/admin", claims)
        permissions = _get(client, "/permissions", claims)

        assert (tenant.status_code, tenant.json()) == (403, NO_TENANT_FROM_GET_TENANT_ID)
        assert (admin.status_code, admin.json()) == (403, NO_TENANT_FROM_RBAC)
        assert (permissions.status_code, permissions.json()) == (403, NO_TENANT_FROM_RBAC)
        assert store.calls == []


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


class TestClaimsNoMiddlewareAttributed:
    """Read with Descope's extractor, as the claims path always was, and now said in the log (#591)."""

    CLAIMS = {"sub": "hand-built", "dct": "t1", "tenants": {"t1": {"roles": ["admin"]}}}

    def test_they_are_read_with_descopes_extractor_and_a_warning(self, caplog):
        request = SimpleNamespace(state=SimpleNamespace(), headers=Headers())

        with caplog.at_level("WARNING", logger="app.dependencies.tenant_access"):
            access = resolve_tenant_access(request, self.CLAIMS, None)

        assert (access.tenant_id, access.roles) == ("t1", ["admin"])
        (message,) = [
            record.getMessage() for record in caplog.records if record.name == "app.dependencies.tenant_access"
        ]
        assert message == (
            "tenant resolution: no provider is recorded for sub='hand-built'; its claims are read as Descope's"
        )

    def test_attributed_claims_are_resolved_without_that_warning(self, caplog):
        request = _attributed_request(descope_config(DESCOPE_PROJECT))

        with caplog.at_level("WARNING", logger="app.dependencies.tenant_access"):
            resolve_tenant_access(request, self.CLAIMS, None)

        assert [record for record in caplog.records if record.name == "app.dependencies.tenant_access"] == []


def _raising_tenant_claims(claims: dict) -> TenantClaims:
    raise KeyError("org")


def _attributed_request(provider: ProviderTokenConfig) -> SimpleNamespace:
    """A request the middleware attributed to ``provider``, with no selector header."""
    return SimpleNamespace(state=SimpleNamespace(provider=provider), headers=Headers())


class TestAnExtractorTheResolverCannotUse:
    """The resolver fails closed by itself, whatever the middleware let through."""

    def test_an_extractor_that_raises_is_refused(self):
        request = _attributed_request(replace(FAKE_PROVIDER, tenant_claims=_raising_tenant_claims))

        with pytest.raises(HTTPException) as refused:
            resolve_tenant_access(request, _fake(org="org-1"), None)

        assert refused.value.status_code == 403
        assert refused.value.detail == "No tenant context"

    @pytest.mark.parametrize("tenant_id", [7, ["org-1"], {"id": "org-1"}, True, ""])
    def test_a_tenant_that_is_not_a_non_empty_string_is_refused(self, tenant_id):
        request = _attributed_request(FAKE_PROVIDER)

        with pytest.raises(HTTPException) as refused:
            resolve_tenant_access(request, _fake(org=tenant_id, org_roles=["admin"]), None)

        assert refused.value.status_code == 403
        assert refused.value.detail == "No tenant context"

    @pytest.mark.parametrize(
        "answer",
        [
            None,
            {"tenant_id": "org-1", "roles": ["admin"], "permissions": []},
            ("org-1", ["admin"], []),
        ],
        ids=["nothing", "a dict", "a tuple"],
    )
    def test_an_answer_that_is_not_tenant_claims_is_refused(self, answer):
        request = _attributed_request(replace(FAKE_PROVIDER, tenant_claims=lambda _claims: answer))

        with pytest.raises(HTTPException) as refused:
            resolve_tenant_access(request, _fake(org="org-1"), None)

        assert refused.value.status_code == 403
        assert refused.value.detail == "No tenant context"

    @pytest.mark.parametrize(
        "grants",
        ["administrator", None, ("admin",), {"admin": True}, ["admin", 7], [["admin"]]],
        ids=["a string", "nothing", "a tuple", "a dict", "a number among them", "a nested list"],
    )
    @pytest.mark.parametrize("field", ["roles", "permissions"])
    def test_grants_that_are_not_a_list_of_strings_are_refused(self, field, grants):
        answer = replace(TenantClaims(tenant_id="org-1", roles=[], permissions=[]), **{field: grants})
        request = _attributed_request(replace(FAKE_PROVIDER, tenant_claims=lambda _claims: answer))

        with pytest.raises(HTTPException) as refused:
            resolve_tenant_access(request, _fake(org="org-1"), None)

        assert refused.value.status_code == 403
        assert refused.value.detail == "No tenant context"

    def test_a_role_string_is_never_searched_for_the_required_role(self, store):
        """``'admin' in 'administrator'`` is true of a string: the route must not be reached that way."""
        answer = TenantClaims(tenant_id="org-1", roles="administrator", permissions="documents.read.all")
        providers = [replace(FAKE_PROVIDER, tenant_claims=lambda _claims: answer)]
        client = TestClient(_app(providers=providers))

        by_role = _get(client, "/admin", _fake())
        by_permission = _get(client, "/permissions", _fake())

        assert by_role.status_code == 403
        assert by_role.json() == NO_TENANT_FROM_RBAC
        assert by_permission.status_code == 403
        assert by_permission.json() == NO_TENANT_FROM_RBAC

    def test_an_unusable_answer_is_logged_with_the_caller(self, caplog):
        request = _attributed_request(replace(FAKE_PROVIDER, tenant_claims=lambda _claims: None))

        with caplog.at_level("WARNING", logger="app.dependencies.tenant_access"), pytest.raises(HTTPException):
            resolve_tenant_access(request, _fake(org="org-1"), None)

        (record,) = caplog.records
        assert record.getMessage() == (
            "tenant resolution refused: the claims extractor of provider=Fake "
            "gave an answer that cannot be used for sub='fake-user'"
        )

    @pytest.mark.parametrize(
        ("provider", "claims"),
        [
            (descope_config(DESCOPE_PROJECT), {"sub": "descope-user", "tenants": {"t1": {"roles": ["admin"]}}}),
            (FAKE_PROVIDER, {"sub": "fake-user", "org_roles": ["admin"]}),
        ],
        ids=["a Descope token with no dct", "another provider's token with no tenant"],
    )
    def test_a_token_that_names_no_tenant_is_refused_without_a_warning(self, provider, claims, caplog):
        """Naming no tenant is an everyday answer, not an extractor at fault."""
        request = _attributed_request(provider)

        with caplog.at_level("DEBUG", logger="app.dependencies.tenant_access"), pytest.raises(HTTPException) as refused:
            resolve_tenant_access(request, claims, None)

        assert refused.value.status_code == 403
        assert refused.value.detail == "No tenant context"
        assert [record for record in caplog.records if record.name == "app.dependencies.tenant_access"] == []

    def test_a_well_formed_answer_is_returned(self):
        request = _attributed_request(FAKE_PROVIDER)

        access = resolve_tenant_access(request, _fake(org="org-1", org_roles=["admin"]), None)

        assert (access.tenant_id, access.roles, access.permissions) == ("org-1", ["admin"], [])


class TestRefusalsAreLoggedWithTheCaller:
    """The log names who was refused and why; the 403 body says neither."""

    def test_a_tenant_the_caller_is_not_in(self, client, store, caplog):
        store.holds((TENANT_A, "admin", []))

        with caplog.at_level("WARNING", logger="app.dependencies.tenant_access"):
            _get(client, "/admin", _ory(), selector=TENANT_C)

        (record,) = caplog.records
        assert record.getMessage() == (
            f"tenant selection refused: provider=Ory sub={ORY_SUB!r} holds no membership in the tenant it names"
        )

    def test_a_header_and_a_dct_that_disagree(self, client, store, caplog):
        store.holds((TENANT_A, "admin", []), (TENANT_B, "member", []))

        with caplog.at_level("WARNING", logger="app.dependencies.tenant_access"):
            _get(client, "/admin", _ory(dct=TENANT_B), selector=TENANT_A)

        (record,) = caplog.records
        assert record.getMessage() == (
            f"tenant selection refused: provider=Ory sub={ORY_SUB!r} sent a header that disagrees with the token's dct"
        )

    def test_a_repeated_header(self, client, store, caplog):
        store.holds((TENANT_A, "admin", []), (TENANT_B, "member", []))

        with caplog.at_level("WARNING", logger="app.dependencies.tenant_access"):
            _get(client, "/admin", _ory(), selector=TENANT_A, extra_headers=[(TENANT_SELECTOR_HEADER, TENANT_B)])

        (record,) = caplog.records
        assert record.getMessage() == (
            f"tenant selection refused: provider=Ory sub={ORY_SUB!r} sent more than one tenant selector"
        )


TYK_API_DEFINITION = Path(__file__).resolve().parents[3] / "tyk" / "apps" / "saas-backend.json"

# The Tyk settings that drop or rewrite a request header on its way to the backend.
TYK_REQUEST_HEADER_SETTINGS = frozenset(
    {"global_headers", "global_headers_remove", "transform_headers", "strip_auth_data", "CORS"}
)


def _settings_named(node: object, names: frozenset[str]) -> dict[str, object]:
    """Every setting in ``names`` anywhere in the document, by name."""
    found: dict[str, object] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if key in names:
                found[key] = value
            found.update(_settings_named(value, names))
    elif isinstance(node, list):
        for item in node:
            found.update(_settings_named(item, names))
    return found


class TestTheGatewayForwardsTheSelector:
    def test_the_api_definition_touches_no_request_header(self):
        """Tyk passes a request header through unless the definition says otherwise."""
        definition = json.loads(TYK_API_DEFINITION.read_text(encoding="utf-8"))

        assert _settings_named(definition, TYK_REQUEST_HEADER_SETTINGS) == {"strip_auth_data": False}
