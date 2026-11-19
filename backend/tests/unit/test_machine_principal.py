"""A token issued to a machine credential's client is authorized from that credential alone (conventions §18).

The token is the recorded Keycloak access token re-stamped as a client-credentials
token of a machine client and verified by the shipped middleware; the canonical
credential is stubbed at ``MachineCredentialRepository``.
``tests/integration/test_keycloak_machine_credentials.py`` reads it from Postgres
with a token Keycloak issued.
"""

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import identity, rbac
from app.dependencies.rbac import MACHINE_PRINCIPAL_REFUSED, MACHINE_ROLES_REFUSED, get_rbac_session
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.domain import credentials
from app.main import app as shipped_app
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import KEYCLOAK_MACHINE_CLIENT_PREFIX, descope_config, keycloak_config
from tests.helpers.keycloak_model import API_AUDIENCE, SPA_CLIENT_ID
from tests.helpers.local_op import RS256, LocalProvider, local_provider

FIXTURES = Path(__file__).resolve().parents[3] / "keycloak" / "fixtures"
ACCESS = json.loads((FIXTURES / "access-token.json").read_text())["claims"]

ACME = uuid.UUID("11111111-1111-4111-8111-111111111111")
GLOBEX = "22222222-2222-4222-8222-222222222222"
MACHINE_SUB = "5e4f1ce0-0000-4000-8000-000000000001"
MACHINE_CLIENT = f"{KEYCLOAK_MACHINE_CLIENT_PREFIX}{uuid.UUID(int=7)}"
ADMIN_ROLE = "/api/access-checks/admin-role"
SETTINGS_MANAGE = "/api/access-checks/settings-manage"
NO_TENANT = {"detail": "No tenant context"}

CREDENTIAL = credentials.MachineCredential(
    id=uuid.UUID(int=7),
    tenant_id=ACME,
    provider="keycloak",
    name="robot",
    client_id=MACHINE_CLIENT,
    subject=MACHINE_SUB,
    external_id="kc-client",
    status=credentials.CredentialStatus.ACTIVE,
    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    created_at=datetime.now(timezone.utc),
    role_names=("admin",),
)


class _Credentials:
    """Stands in for MachineCredentialRepository: one credential, every lookup recorded."""

    credential: credentials.MachineCredential | None = CREDENTIAL
    grants: dict[str, tuple[str, ...]] = {"admin": ("documents:read", "settings:manage")}
    lookups: list[tuple[str, str]] = []

    def __init__(self, _session) -> None:
        pass

    async def by_subject(self, provider: str, subject: str):
        type(self).lookups.append((provider, subject))
        found = type(self).credential
        return found if found is not None and found.subject == subject else None

    async def permissions(self, credential_id):
        return type(self).grants


class _NoUserLookups:
    def __init__(self, **_kwargs) -> None:
        raise AssertionError("a machine token must not be resolved as a user")


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


@pytest.fixture
def stored(monkeypatch) -> type[_Credentials]:
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
    monkeypatch.setattr(rbac, "MachineCredentialRepository", _Credentials)
    monkeypatch.setattr(rbac, "IdentityResolutionService", _NoUserLookups)
    monkeypatch.setattr(_Credentials, "credential", CREDENTIAL)
    monkeypatch.setattr(_Credentials, "lookups", [])
    return _Credentials


@pytest.fixture
def client(op) -> TestClient:
    app = FastAPI()
    for exception, handler in shipped_app.exception_handlers.items():
        app.add_exception_handler(exception, handler)
    app.include_router(shipped_app.router)

    async def _session():
        yield object()

    app.dependency_overrides[get_rbac_session] = _session
    keycloak = keycloak_config(op.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)
    app.add_middleware(TokenValidationMiddleware, providers=[descope_config("P-machine"), keycloak])
    return TestClient(app)


def _machine_token(op: LocalProvider, **extra) -> str:
    claims = {
        "sub": MACHINE_SUB,
        "azp": MACHINE_CLIENT,
        "aud": API_AUDIENCE,
        "typ": "Bearer",
        "client_id": MACHINE_CLIENT,
    }
    return op.token(RS256, {**claims, "iss": op.issuer, **extra})


def _get(client: TestClient, path: str, token: str, tenant: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        headers[TENANT_SELECTOR_HEADER] = tenant
    return client.get(path, headers=headers)


class TestAMachineTokenIsItsCredential:
    def test_its_tenant_and_roles_are_the_credentials(self, op, client, stored):
        response = _get(client, ADMIN_ROLE, _machine_token(op))

        assert (response.status_code, response.json()) == (200, {"roles": ["admin"]})
        assert stored.lookups == [("keycloak", MACHINE_SUB)]

    def test_its_roles_permissions_are_granted(self, op, client, stored):
        response = _get(client, SETTINGS_MANAGE, _machine_token(op), tenant=str(ACME))

        assert (response.status_code, response.json()) == (
            200,
            {"tenant_id": str(ACME), "permission": "settings:manage"},
        )

    def test_another_tenant_is_refused(self, op, client, stored):
        response = _get(client, ADMIN_ROLE, _machine_token(op), tenant=GLOBEX)

        assert (response.status_code, response.json()) == (403, NO_TENANT)

    @pytest.mark.parametrize(
        "grants",
        [{}, {"operator": ("billing:manage",)}, {"admin": ("settings:manage",), "operator": ()}],
        ids=["every role deleted since", "its role renamed operator", "operator beside admin"],
    )
    @pytest.mark.parametrize("path", [ADMIN_ROLE, SETTINGS_MANAGE, "/api/sync/status"])
    def test_roles_it_may_no_longer_hold_grant_nothing(self, op, client, stored, monkeypatch, grants, path):
        """Checked on each request: a role renamed or deleted after create changes what it holds."""
        monkeypatch.setattr(_Credentials, "grants", grants)

        response = _get(client, path, _machine_token(op))

        assert (response.status_code, response.json()["type"], response.json()["detail"]) == (
            403,
            "/errors/forbidden",
            MACHINE_ROLES_REFUSED,
        )


class TestARefusedCredential:
    @pytest.mark.parametrize(
        "credential",
        [
            None,
            replace(CREDENTIAL, status=credentials.CredentialStatus.INACTIVE),
            replace(CREDENTIAL, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
            replace(CREDENTIAL, client_id=f"{KEYCLOAK_MACHINE_CLIENT_PREFIX}another"),
        ],
        ids=["deleted", "deactivated", "expired", "issued to another client"],
    )
    def test_grants_nothing(self, op, client, stored, monkeypatch, credential):
        monkeypatch.setattr(_Credentials, "credential", credential)

        response = _get(client, ADMIN_ROLE, _machine_token(op))

        assert (response.status_code, response.json()) == (403, NO_TENANT)

    def test_a_persons_subject_on_a_machine_clients_token_grants_nothing(self, op, client, stored):
        """A user signing in through a machine client (were its flows ever switched on) is no person here."""
        response = _get(client, ADMIN_ROLE, _machine_token(op, sub=ACCESS["sub"]))

        assert (response.status_code, response.json()) == (403, NO_TENANT)


KEY_ROUTES = [
    ("POST", "/api/keys"),
    ("GET", "/api/keys"),
    ("GET", "/api/keys/k1"),
    ("POST", "/api/keys/k1/deactivate"),
    ("POST", "/api/keys/k1/activate"),
    ("DELETE", "/api/keys/k1"),
]


class TestAMachineManagesNoCredential:
    """A credential holding admin would otherwise mint its own successor before it expires."""

    @pytest.mark.parametrize(("method", "path"), KEY_ROUTES)
    def test_every_key_route_refuses_a_machine_token(self, op, client, stored, monkeypatch, method, path):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")

        response = client.request(
            method,
            path,
            json={"name": "successor", "expire_time": 4_102_444_800, "role_names": ["admin"]},
            headers={"Authorization": f"Bearer {_machine_token(op)}"},
        )

        assert (response.status_code, response.json()["type"], response.json()["detail"]) == (
            403,
            "/errors/forbidden",
            MACHINE_PRINCIPAL_REFUSED,
        )
        # Refused before any credential was looked up.
        assert stored.lookups == []
