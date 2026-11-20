"""A Keycloak principal's tenant, roles and permissions come from the canonical store, never from its token.

The token is the recorded SPA access token (``keycloak/fixtures/access-token.json``)
re-stamped onto a loopback provider and verified by the shipped middleware.
Requests go to the shipped ``/api/access-checks`` routes. The canonical answer
is stubbed at ``IdentityResolutionService``, as in ``test_tenant_resolution.py``;
``tests/integration/test_keycloak_canonical.py`` reads it from Postgres.
"""

import ast
import json
from pathlib import Path

import pytest
from expression import Error, Ok
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import rbac
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.errors.identity import NotFound
from app.main import app as shipped_app
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import ProviderTokenConfig, descope_config, keycloak_config
from tests.helpers.canonical_payload import active_memberships
from tests.helpers.keycloak_model import API_AUDIENCE, SPA_CLIENT_ID
from tests.helpers.local_op import RS256, LocalProvider, local_provider

FIXTURES = Path(__file__).resolve().parents[3] / "keycloak" / "fixtures"
ACCESS = json.loads((FIXTURES / "access-token.json").read_text())["claims"]
SUB = ACCESS["sub"]

ACME = "11111111-1111-4111-8111-111111111111"
GLOBEX = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
ADMIN_PERMISSIONS = ["documents:read", "settings:manage"]
VIEWER_PERMISSIONS = ["documents:read"]

ADMIN_ROLE = "/api/access-checks/admin-role"
VIEWER_ROLE = "/api/access-checks/viewer-role"
DOCUMENTS_READ = "/api/access-checks/documents-read"
SETTINGS_MANAGE = "/api/access-checks/settings-manage"
NO_TENANT = {"detail": "No tenant context"}
INSUFFICIENT = {"detail": "Insufficient permissions"}

# Every claim another provider, or a realm with role mappers, would grant by.
FORGED = {
    "resource_access": {API_AUDIENCE: {"roles": ["owner", "admin", "settings:manage", "billing:manage"]}},
    "realm_access": {"roles": ["admin", "owner"]},
    "roles": ["owner", "admin"],
    "tenants": {ACME: {"roles": ["owner"], "permissions": ["settings:manage"]}},
}


class _CanonicalStore:
    """Stands in for IdentityResolutionService: canned grants, every lookup recorded."""

    def __init__(self) -> None:
        self.result = Error(NotFound("no identity"))
        self.calls: list[tuple[str, str]] = []

    def holds(self, *grants: tuple[str, str, list[str]], status: str = "active") -> None:
        roles = [
            {"tenant_id": tenant_id, "role_name": role, "permissions": list(permissions)}
            for tenant_id, role, permissions in grants
        ]
        self.result = Ok(
            {
                "user": {"id": "canonical-user-1", "status": status},
                "roles": roles,
                "tenant_memberships": active_memberships(roles),
            }
        )

    async def resolve(self, *, provider: str, sub: str):
        self.calls.append((provider, sub))
        return self.result


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


@pytest.fixture
def store(monkeypatch) -> _CanonicalStore:
    canonical = _CanonicalStore()
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: canonical)
    return canonical


def _keycloak(op: LocalProvider) -> ProviderTokenConfig:
    return keycloak_config(op.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)


@pytest.fixture
def client(op) -> TestClient:
    app = FastAPI()
    app.include_router(shipped_app.router)

    async def _session():
        yield object()

    app.dependency_overrides[get_rbac_session] = _session
    app.add_middleware(TokenValidationMiddleware, providers=[descope_config("P-keycloak-canonical"), _keycloak(op)])
    return TestClient(app)


def _token(op: LocalProvider, **extra) -> str:
    claims = {k: v for k, v in ACCESS.items() if k not in {"iat", "exp", "nbf"}}
    return op.token(RS256, {**claims, "iss": op.issuer, **extra})


def _get(client: TestClient, path: str, token: str, *, tenant: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        headers[TENANT_SELECTOR_HEADER] = tenant
    return client.get(path, headers=headers)


class TestTheCanonicalStoreDecides:
    def test_the_canonical_role_and_permissions_are_granted(self, op, client, store):
        store.holds((ACME, "admin", ADMIN_PERMISSIONS))
        token = _token(op)

        assert _get(client, ADMIN_ROLE, token).json() == {"roles": ["admin"]}
        assert _get(client, SETTINGS_MANAGE, token).json() == {"tenant_id": ACME, "permission": "settings:manage"}
        assert store.calls == [("keycloak", SUB), ("keycloak", SUB)]

    def test_what_the_store_does_not_grant_is_refused(self, op, client, store):
        store.holds((ACME, "viewer", VIEWER_PERMISSIONS))
        token = _token(op)

        assert _get(client, DOCUMENTS_READ, token).json() == {"tenant_id": ACME, "permission": "documents:read"}
        for path in (ADMIN_ROLE, SETTINGS_MANAGE):
            response = _get(client, path, token)
            assert (response.status_code, response.json()) == (403, INSUFFICIENT)

    def test_without_a_canonical_identity_nothing_is_granted(self, op, client, store):
        response = _get(client, VIEWER_ROLE, _token(op))

        assert (response.status_code, response.json()) == (403, NO_TENANT)


class TestForgedClaimsChangeNothing:
    @pytest.mark.parametrize("claim", sorted(FORGED))
    @pytest.mark.parametrize("path", [ADMIN_ROLE, SETTINGS_MANAGE])
    def test_a_claim_granting_more_than_the_store_grants_nothing(self, op, client, store, claim, path):
        store.holds((ACME, "viewer", VIEWER_PERMISSIONS))

        response = _get(client, path, _token(op, **{claim: FORGED[claim]}))

        assert (response.status_code, response.json()) == (403, INSUFFICIENT)

    def test_all_of_them_at_once_grant_nothing(self, op, client, store):
        store.holds((ACME, "viewer", VIEWER_PERMISSIONS))

        response = _get(client, VIEWER_ROLE, _token(op, **FORGED))

        assert (response.status_code, response.json()) == (200, {"roles": ["viewer"]})

    def test_a_dct_naming_a_tenant_outside_the_store_grants_nothing(self, op, client, store):
        """A ``dct`` is read as a tenant selector (conventions §18): it chooses among memberships, never adds one."""
        store.holds((ACME, "viewer", VIEWER_PERMISSIONS))

        response = _get(client, DOCUMENTS_READ, _token(op, dct=OTHER, tenants={OTHER: {"roles": ["owner"]}}))

        assert (response.status_code, response.json()) == (403, NO_TENANT)

    def test_the_organization_claim_selects_no_tenant(self, op, client, store):
        """The token names ``acme``; a user in two tenants still has to select one."""
        store.holds((ACME, "admin", ADMIN_PERMISSIONS), (GLOBEX, "admin", ADMIN_PERMISSIONS))

        response = _get(client, ADMIN_ROLE, _token(op))

        assert (response.status_code, response.json()) == (403, NO_TENANT)


class TestAUserInTwoTenants:
    @pytest.mark.parametrize("tenant", [ACME, GLOBEX])
    def test_is_a_tenant_admin_in_each_the_store_says(self, op, client, store, tenant):
        store.holds((ACME, "admin", ADMIN_PERMISSIONS), (GLOBEX, "admin", ADMIN_PERMISSIONS))
        token = _token(op)

        assert _get(client, ADMIN_ROLE, token, tenant=tenant).json() == {"roles": ["admin"]}
        assert _get(client, SETTINGS_MANAGE, token, tenant=tenant).json() == {
            "tenant_id": tenant,
            "permission": "settings:manage",
        }

    def test_is_admin_only_where_the_store_says(self, op, client, store):
        store.holds((ACME, "admin", ADMIN_PERMISSIONS), (GLOBEX, "viewer", VIEWER_PERMISSIONS))
        token = _token(op)

        assert _get(client, ADMIN_ROLE, token, tenant=ACME).json() == {"roles": ["admin"]}
        refused = _get(client, ADMIN_ROLE, token, tenant=GLOBEX)
        assert (refused.status_code, refused.json()) == (403, INSUFFICIENT)
        assert _get(client, VIEWER_ROLE, token, tenant=GLOBEX).json() == {"roles": ["viewer"]}

    def test_a_tenant_outside_the_store_is_refused(self, op, client, store):
        store.holds((ACME, "admin", ADMIN_PERMISSIONS), (GLOBEX, "admin", ADMIN_PERMISSIONS))

        response = _get(client, ADMIN_ROLE, _token(op), tenant=OTHER)

        assert (response.status_code, response.json()) == (403, NO_TENANT)


class TestADeactivatedUser:
    @pytest.mark.parametrize("status", ["inactive", "provisioned"])
    @pytest.mark.parametrize("path", [VIEWER_ROLE, DOCUMENTS_READ])
    def test_is_refused_with_a_valid_token(self, op, client, store, status, path):
        store.holds((ACME, "admin", ADMIN_PERMISSIONS), status=status)

        response = _get(client, path, _token(op))

        assert (response.status_code, response.json()) == (403, NO_TENANT)


class TestWithoutTheCanonicalListing:
    def test_nothing_is_granted(self, op, client, monkeypatch):
        """Keycloak has no claims extractor: unlisted, it has no authorization source at all."""
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory"}))

        response = _get(client, VIEWER_ROLE, _token(op, **FORGED))

        assert (response.status_code, response.json()) == (403, NO_TENANT)


APP_DIR = Path(__file__).resolve().parents[2] / "app"
# ``dependencies/identity.py`` picks the sync adapter by ``SYNC_PROVIDER`` and names
# each adapter, Keycloak's among them, and ``dependencies/keycloak_sync.py`` wires
# that adapter's canonical links: they push canonical changes out and decide no
# request's access. ``dependencies/keycloak_pull.py`` wires the event pull, which
# brings Keycloak's changes in, in a background task: no request goes through it.
NOT_AUTHORIZATION = {
    "dependencies/identity.py",
    "dependencies/keycloak_sync.py",
    "dependencies/keycloak_pull.py",
    "dependencies/keycloak_reconciliation.py",
}


def _code_mentions(tree: ast.AST) -> list[str]:
    """Every identifier, imported name and non-docstring string in ``tree`` that names Keycloak."""
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(node.name)
        elif isinstance(node, ast.ImportFrom):
            found.extend([node.module or "", *(alias.name for alias in node.names)])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.append(node.value)
    return [mention for mention in found if "keycloak" in mention.lower()]


def test_no_keycloak_branch_remains_in_dependencies_or_routers():
    mentions = {
        path.relative_to(APP_DIR).as_posix(): _code_mentions(ast.parse(path.read_text()))
        for directory in ("dependencies", "routers")
        for path in sorted((APP_DIR / directory).rglob("*.py"))
        if path.relative_to(APP_DIR).as_posix() not in NOT_AUTHORIZATION
    }

    assert {path: found for path, found in mentions.items() if found} == {}
