"""The tenant routes serve every provider's principal from the tenant resolver's sources (#673, conventions §18).

``GET /api/tenants``, ``GET /api/tenants/current`` and ``GET``/``POST
/api/tenants/{tenant_id}/resources``: a Keycloak, Ory or Zitadel principal's
tenants are its canonical memberships; a Descope principal's are its signed
``tenants`` claim. A member of a tenant is served that tenant's resources and
anyone else gets 403.

Requests go through the shipped gateway middleware and the shipped routers.
Only the canonical store, the database session and the tenant service are stubs.
"""

import base64
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Ok
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import rbac
from app.dependencies.identity import get_tenant_service
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant_access import SELECT_A_TENANT_FIRST, TENANT_SELECTOR_HEADER, TenantRefusal
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import descope_config, keycloak_config, ory_config, zitadel_config
from app.models.database import get_async_session
from app.models.identity.tenant import TenantStatus
from app.models.tenant import TenantIdSpace, TenantResource
from app.routers.tenants import NOT_A_MEMBER

DESCOPE_PROJECT = "P2tenantroutes"
SUB = "subject-1"

ACME = "11111111-1111-4111-8111-111111111111"
GLOBEX = "22222222-2222-4222-8222-222222222222"
# A tenant the caller holds no membership in.
INITECH = "33333333-3333-4333-8333-333333333333"
NAMES = {ACME: "Acme", GLOBEX: "Globex"}

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

REFUSED_NOT_A_MEMBER = {"detail": NOT_A_MEMBER}
# Whose tenant ids each provider's principal names (#690): Descope's are its own,
# however they are spelled; here they are spelled as the canonical tenants' UUIDs.
SPACE = {"descope": TenantIdSpace.descope, **{provider: TenantIdSpace.canonical for provider in CANONICAL}}
CREATED = datetime(2026, 11, 18, tzinfo=timezone.utc)
DOMAINS = {ACME: ["acme.test"], GLOBEX: ["globex.test"]}

# What the caller holds in each of its tenants, the same for every provider.
EXPECTED_TENANTS = {
    ACME: {"id": ACME, "name": "Acme", "roles": ["admin"], "permissions": ["settings:manage"]},
    GLOBEX: {"id": GLOBEX, "name": "Globex", "roles": ["member"], "permissions": []},
}


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    """An unsigned JWT: gateway mode reads the payload the gateway already verified."""
    return f"{_segment({'alg': 'RS256'})}.{_segment({'exp': int(time.time()) + 300, **claims})}.sig"


def _token(provider: str) -> str:
    if provider == "descope":
        tenants = {
            ACME: {"roles": ["admin"], "permissions": ["settings:manage"]},
            GLOBEX: {"roles": ["member"], "permissions": []},
        }
        return _jwt({"iss": f"https://api.descope.com/{DESCOPE_PROJECT}", "sub": SUB, "dct": ACME, "tenants": tenants})
    (issuer,) = CANONICAL[provider].accepted_issuers
    return _jwt({"iss": issuer, "sub": SUB, **_BOUND[provider]})


def _headers(provider: str, tenant: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {_token(provider)}"}
    if tenant is not None and provider != "descope":
        headers[TENANT_SELECTOR_HEADER] = tenant
    return headers


class _CanonicalStore:
    """The canonical caller is an admin of Acme and a member of Globex; lookups are recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.status = {ACME: TenantStatus.active, GLOBEX: TenantStatus.active}

    async def resolve(self, *, provider: str, sub: str):
        self.calls.append((provider, sub))
        return Ok(
            {
                "user": {"id": "canonical-user-1", "status": "active"},
                "roles": [
                    {"tenant_id": ACME, "role_name": "admin", "permissions": ["settings:manage"]},
                    {"tenant_id": GLOBEX, "role_name": "member", "permissions": []},
                ],
                "tenant_memberships": [
                    {"tenant_id": tenant_id, "tenant_name": name, "status": self.status[tenant_id].value}
                    for tenant_id, name in NAMES.items()
                ],
            }
        )


class _Stubs:
    def __init__(self) -> None:
        self.store = _CanonicalStore()
        self.resources = [TenantResource(id="r-1", tenant_id=ACME, name="Ledger", created_at="2026-11-18T00:00:00")]
        self.session = MagicMock()
        self.session.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: list(self.resources)))
        )
        self.session.commit = AsyncMock()
        self.session.refresh = AsyncMock()
        self.session.add = MagicMock()
        self.tenant_service = MagicMock()
        self.tenant_service.get_tenant = AsyncMock(side_effect=self._get_tenant)
        self.descope_client = AsyncMock()
        self.descope_client.load_tenant.side_effect = lambda tenant_id: {
            "id": tenant_id,
            "name": NAMES[tenant_id],
            "selfProvisioningDomains": DOMAINS[tenant_id],
            "createdTime": int(CREATED.timestamp()),
            "customAttributes": {"plan_tier": "pro"},
        }

    @staticmethod
    async def _get_tenant(*, tenant_id: uuid.UUID):
        return Ok(
            {
                "id": tenant_id,
                "name": NAMES[str(tenant_id)],
                "domains": DOMAINS[str(tenant_id)],
                "external_org_id": "org-alias",
                "external_org_provider_id": uuid.uuid4(),
                "status": "active",
                "created_at": CREATED,
                "updated_at": CREATED,
            }
        )


@pytest.fixture
def stubs(monkeypatch) -> _Stubs:
    built = _Stubs()
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset(CANONICAL))
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: built.store)
    return built


@pytest.fixture
def client(stubs) -> TestClient:
    app = FastAPI()
    app.state.descope_client = stubs.descope_client
    app.include_router(shipped_app.router)

    async def _rbac_session():
        yield object()

    async def _session():
        yield stubs.session

    app.dependency_overrides[get_rbac_session] = _rbac_session
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_tenant_service] = lambda: stubs.tenant_service
    app.add_middleware(GatewayClaimsMiddleware, providers=[DESCOPE, *CANONICAL.values()])
    return TestClient(app)


PROVIDERS = ["descope", *sorted(CANONICAL)]


@pytest.mark.parametrize("provider", PROVIDERS)
class TestEveryProvidersPrincipal:
    def test_the_tenant_list_is_its_memberships(self, client, provider):
        response = client.get("/api/tenants", headers=_headers(provider))

        assert response.status_code == 200
        assert {tenant["id"]: tenant for tenant in response.json()["tenants"]} == EXPECTED_TENANTS

    def test_its_own_tenants_resources_are_served(self, client, provider):
        response = client.get(f"/api/tenants/{ACME}/resources", headers=_headers(provider, ACME))

        assert response.status_code == 200
        assert {resource["id"]: resource for resource in response.json()["resources"]} == {
            "r-1": {
                "id": "r-1",
                "tenant_id": ACME,
                "name": "Ledger",
                "description": "",
                "created_at": "2026-11-18T00:00:00",
            }
        }

    def test_another_tenants_resources_are_refused(self, client, stubs, provider):
        response = client.get(f"/api/tenants/{INITECH}/resources", headers=_headers(provider, ACME))

        assert (response.status_code, response.json()) == (403, REFUSED_NOT_A_MEMBER)
        stubs.session.execute.assert_not_awaited()

    def test_it_creates_a_resource_in_its_own_tenant(self, client, stubs, provider):
        response = client.post(
            f"/api/tenants/{GLOBEX}/resources", headers=_headers(provider, GLOBEX), json={"name": "Plans"}
        )

        assert response.status_code == 200
        (added,) = [call.args[0] for call in stubs.session.add.call_args_list]
        assert (added.tenant_space, added.tenant_id, added.name) == (SPACE[provider], GLOBEX, "Plans")
        assert "tenant_space" not in response.json()

    def test_it_reads_resources_only_in_its_own_id_space(self, client, stubs, provider):
        client.get(f"/api/tenants/{ACME}/resources", headers=_headers(provider, ACME))

        (statement,) = [call.args[0] for call in stubs.session.execute.await_args_list]
        filters = {clause.left.name: clause.right.value for clause in statement.whereclause.clauses}
        assert filters == {"tenant_space": SPACE[provider], "tenant_id": ACME}

    def test_it_creates_no_resource_in_another_tenant(self, client, stubs, provider):
        response = client.post(
            f"/api/tenants/{INITECH}/resources", headers=_headers(provider, ACME), json={"name": "Plans"}
        )

        assert (response.status_code, response.json()) == (403, REFUSED_NOT_A_MEMBER)
        stubs.session.add.assert_not_called()

    def test_its_current_tenant_is_served(self, client, provider):
        response = client.get("/api/tenants/current", headers=_headers(provider, ACME))

        assert (response.status_code, response.json()) == (
            200,
            {
                "tenant_id": ACME,
                "tenant": {
                    "id": ACME,
                    "name": "Acme",
                    "domains": ["acme.test"],
                    "created_at": CREATED.isoformat(),
                    "updated_at": CREATED.isoformat(),
                },
            },
        )


@pytest.mark.parametrize("provider", sorted(CANONICAL))
class TestACanonicalPrincipal:
    def test_its_tenants_come_from_the_canonical_store_not_descope(self, client, stubs, provider):
        client.get("/api/tenants", headers=_headers(provider))
        client.get("/api/tenants/current", headers=_headers(provider, GLOBEX))

        assert stubs.store.calls == [(provider, SUB), (provider, SUB)]
        assert stubs.descope_client.mock_calls == []
        stubs.tenant_service.get_tenant.assert_awaited_once_with(tenant_id=uuid.UUID(GLOBEX))

    def test_its_current_tenant_needs_a_selection_among_several(self, client, stubs, provider):
        response = client.get("/api/tenants/current", headers=_headers(provider))

        assert (response.status_code, response.json()) == (403, {"detail": SELECT_A_TENANT_FIRST})
        stubs.tenant_service.get_tenant.assert_not_awaited()

    def test_a_selector_naming_another_tenant_is_refused(self, client, stubs, provider):
        response = client.get("/api/tenants/current", headers=_headers(provider, INITECH))

        assert (response.status_code, response.json()) == (403, {"detail": SELECT_A_TENANT_FIRST})
        stubs.tenant_service.get_tenant.assert_not_awaited()


@pytest.mark.parametrize("provider", sorted(CANONICAL))
class TestASuspendedCanonicalTenant:
    """#689: Globex is suspended; its member is still served Acme and refused Globex."""

    @pytest.fixture(autouse=True)
    def _globex_suspended(self, stubs):
        stubs.store.status[GLOBEX] = TenantStatus.suspended

    def test_it_is_not_listed(self, client, provider):
        response = client.get("/api/tenants", headers=_headers(provider))

        assert response.status_code == 200
        assert {tenant["id"]: tenant for tenant in response.json()["tenants"]} == {ACME: EXPECTED_TENANTS[ACME]}

    def test_it_is_not_the_current_tenant(self, client, stubs, provider):
        response = client.get("/api/tenants/current", headers=_headers(provider, GLOBEX))

        assert (response.status_code, response.json()) == (403, {"detail": TenantRefusal.NOT_ACTIVE})
        stubs.tenant_service.get_tenant.assert_not_awaited()

    def test_the_active_tenant_is_current_without_a_selection(self, client, provider):
        response = client.get("/api/tenants/current", headers=_headers(provider))

        assert (response.status_code, response.json()["tenant_id"]) == (200, ACME)

    def test_its_resources_are_not_read(self, client, stubs, provider):
        response = client.get(f"/api/tenants/{GLOBEX}/resources", headers=_headers(provider, GLOBEX))

        assert (response.status_code, response.json()) == (403, {"detail": TenantRefusal.NOT_ACTIVE})
        stubs.session.execute.assert_not_awaited()

    def test_no_resource_is_created_in_it(self, client, stubs, provider):
        response = client.post(
            f"/api/tenants/{GLOBEX}/resources", headers=_headers(provider, GLOBEX), json={"name": "Plans"}
        )

        assert (response.status_code, response.json()) == (403, {"detail": TenantRefusal.NOT_ACTIVE})
        stubs.session.add.assert_not_called()


class TestADescopePrincipal:
    def test_its_tenants_are_read_from_its_claims_not_the_canonical_store(self, client, stubs):
        client.get("/api/tenants", headers=_headers("descope"))
        client.get(f"/api/tenants/{ACME}/resources", headers=_headers("descope"))

        assert stubs.store.calls == []

    def test_its_current_tenant_is_loaded_from_descope(self, client, stubs):
        client.get("/api/tenants/current", headers=_headers("descope"))

        stubs.descope_client.load_tenant.assert_awaited_once_with(ACME)
        stubs.tenant_service.get_tenant.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/tenants"),
        ("GET", "/api/tenants/current"),
        ("GET", f"/api/tenants/{ACME}/resources"),
        ("POST", f"/api/tenants/{ACME}/resources"),
    ],
)
def test_a_request_without_a_token_gets_401(client, method, path):
    response = client.request(method, path, json={"name": "x"})

    assert (response.status_code, response.json()) == (401, {"detail": "Missing or invalid authorization header"})
