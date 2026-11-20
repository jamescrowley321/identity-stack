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
from expression import Ok
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import identity, rbac
from app.dependencies.identity import get_machine_credential_service, get_member_directory
from app.dependencies.rbac import MACHINE_PRINCIPAL_REFUSED, MACHINE_ROLES_REFUSED, get_rbac_session
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.domain import credentials
from app.main import app as shipped_app
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import (
    KEYCLOAK_MACHINE_CLIENT_PREFIX,
    descope_access_key,
    descope_config,
    keycloak_config,
)
from app.services.machine_credentials import MachineCredentials, MachineCredentialService
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


# The routes that change people's roles, the role and permission definitions, or a
# tenant's members, with a body each would accept from an admin.
ASSIGNMENT = {"user_id": str(uuid.UUID(int=9)), "tenant_id": str(ACME), "role_names": ["admin"]}
ROLE_AND_MEMBER_ROUTES = [
    ("POST", "/api/roles/assign", ASSIGNMENT),
    ("POST", "/api/roles/remove", ASSIGNMENT),
    ("GET", "/api/roles", None),
    ("POST", "/api/roles", {"name": "robot-made", "permission_names": []}),
    ("PUT", "/api/roles/member", {"description": "changed by a machine"}),
    ("DELETE", "/api/roles/member", None),
    ("GET", "/api/permissions", None),
    ("POST", "/api/permissions", {"name": "robot:made"}),
    ("PUT", "/api/permissions/documents:read", {"new_name": "documents:read"}),
    ("DELETE", "/api/permissions/documents:read", None),
    ("GET", "/api/members", None),
    ("POST", "/api/members/invite", {"email": "new@acme.example", "role_names": ["admin"]}),
    ("POST", f"/api/members/{uuid.UUID(int=9)}/deactivate", None),
    ("POST", f"/api/members/{uuid.UUID(int=9)}/activate", None),
    ("DELETE", f"/api/members/{uuid.UUID(int=9)}", None),
]
DOCUMENT = uuid.UUID(int=11)
RELATION = {"resource_type": "document", "resource_id": str(DOCUMENT), "relation": "owner", "target": "bob"}
# The writes that change who holds a relation, or which account signs in as a person.
RELATION_AND_LINK_ROUTES = [
    ("POST", "/api/fga/relations", RELATION),
    ("DELETE", "/api/fga/relations", RELATION),
    ("PUT", "/api/fga/schema", {"schema": "model\n  schema 1.1"}),
    ("POST", f"/api/documents/{DOCUMENT}/share", {"user_id": str(uuid.UUID(int=9)), "relation": "editor"}),
    ("DELETE", f"/api/documents/{DOCUMENT}/share/{uuid.UUID(int=9)}", None),
    ("POST", f"/api/users/{uuid.UUID(int=9)}/idp-links", {"provider_id": str(uuid.UUID(int=3)), "external_sub": "x"}),
    ("DELETE", f"/api/users/{uuid.UUID(int=9)}/idp-links/{uuid.UUID(int=4)}", None),
]


def _assert_refused_as_a_machine(response, stored) -> None:
    assert (response.status_code, response.json()["type"], response.json()["detail"]) == (
        403,
        "/errors/forbidden",
        MACHINE_PRINCIPAL_REFUSED,
    )
    # Refused before any credential was looked up.
    assert stored.lookups == []


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

        _assert_refused_as_a_machine(response, stored)


class TestAMachineManagesNoRoleOrMember:
    """A credential holding admin would otherwise assign people roles and invite or deactivate members (#683)."""

    @pytest.mark.parametrize(
        ("method", "path", "body"), ROLE_AND_MEMBER_ROUTES, ids=[f"{m} {p}" for m, p, _ in ROLE_AND_MEMBER_ROUTES]
    )
    def test_every_role_and_member_route_refuses_a_machine_token(
        self, op, client, stored, monkeypatch, method, path, body
    ):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")

        response = client.request(
            method,
            path,
            json=body,
            headers={"Authorization": f"Bearer {_machine_token(op)}", TENANT_SELECTOR_HEADER: str(ACME)},
        )

        _assert_refused_as_a_machine(response, stored)


class TestAMachineChangesNoRelationOrLink:
    """A credential holding admin would otherwise make any user a document's owner on OpenFGA (#683)."""

    @pytest.mark.parametrize(
        ("method", "path", "body"), RELATION_AND_LINK_ROUTES, ids=[f"{m} {p}" for m, p, _ in RELATION_AND_LINK_ROUTES]
    )
    def test_every_relation_and_link_write_refuses_a_machine_token(
        self, op, client, stored, monkeypatch, method, path, body
    ):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")

        response = client.request(
            method,
            path,
            json=body,
            headers={"Authorization": f"Bearer {_machine_token(op)}", TENANT_SELECTOR_HEADER: str(ACME)},
        )

        _assert_refused_as_a_machine(response, stored)


# --- Descope access-key sessions (#699) ------------------------------------------------
#
# An access key exchanged for a session (POST /v1/auth/accesskey/exchange) is signed by
# the project's keys on the bare project id, with ``drn`` DS like a user's session. What
# tells them apart is ``amr``: Descope stamps it on every user session and none on an
# access key's (docs.descope.com/management/token; the live claims of the e2e admin's
# access-key session were drn, exp, iat, iss, rexp, sub and tenants).

DESCOPE_PROJECT = "P2machine"
DESCOPE_TENANT = "T2acme"
ACCESS_KEY_ID = "K2accesskey"
DESCOPE_USER_ID = "U2person"


def _descope_session(op: LocalProvider, **extra) -> str:
    """A Descope session token of an owner+admin of ``DESCOPE_TENANT``, as the exchange or a sign-in returns it."""
    claims = {
        "iss": DESCOPE_PROJECT,
        "drn": "DS",
        "sub": ACCESS_KEY_ID,
        "tenants": {DESCOPE_TENANT: {"roles": ["owner", "admin"], "permissions": []}},
    }
    return op.token(RS256, {**claims, **extra})


def _user_session(op: LocalProvider) -> str:
    return _descope_session(op, sub=DESCOPE_USER_ID, amr=["email"])


class _AccessKeys(MachineCredentials):
    """Descope's access keys, recording every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create(self, *, tenant_id, name, expire_time, role_names):
        self.calls.append("create")
        return Ok({"key": {"id": "K2successor"}, "cleartext": "secret"})

    async def list(self, *, tenant_id):
        self.calls.append("list")
        return Ok({"keys": []})

    async def get(self, *, tenant_id, credential_id):
        self.calls.append("get")
        return Ok({"key": {"id": credential_id}})

    async def set_active(self, *, tenant_id, credential_id, active):
        self.calls.append("set_active")
        return Ok(None)

    async def delete(self, *, tenant_id, credential_id):
        self.calls.append("delete")
        return Ok(None)


class _Members:
    """Descope's member directory, recording every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_members(self, tenant_id):
        self.calls.append("list_members")
        return Ok({"members": []})


@pytest.fixture
def descope_stores() -> tuple[_AccessKeys, _Members]:
    return _AccessKeys(), _Members()


@pytest.fixture
def descope_client(op, descope_stores, monkeypatch) -> TestClient:
    """The shipped routes behind the shipped middleware, Descope's keys moved to the loopback provider."""
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "descope")
    keys, members = descope_stores
    app = FastAPI()
    for exception, handler in shipped_app.exception_handlers.items():
        app.add_exception_handler(exception, handler)
    app.include_router(shipped_app.router)
    app.state.descope_client = object()
    app.dependency_overrides[get_machine_credential_service] = lambda: MachineCredentialService(keys)
    app.dependency_overrides[get_member_directory] = lambda: members
    descope = replace(
        descope_config(DESCOPE_PROJECT),
        disco_address=op.disco_address,
        accepted_issuers=frozenset({op.issuer, DESCOPE_PROJECT}),
    )
    app.add_middleware(TokenValidationMiddleware, providers=[descope])
    return TestClient(app)


def _assert_machine_refused(response) -> None:
    assert (response.status_code, response.json()["type"], response.json()["detail"]) == (
        403,
        "/errors/forbidden",
        MACHINE_PRINCIPAL_REFUSED,
    )


class TestADescopeAccessKeySessionIsAMachine:
    """An owner/admin access key would otherwise mint its successor and manage its siblings, roles and members."""

    @pytest.mark.parametrize(("method", "path"), KEY_ROUTES)
    def test_every_key_route_refuses_it(self, op, descope_client, descope_stores, method, path):
        response = descope_client.request(
            method,
            path,
            json={"name": "successor", "expire_time": 4_102_444_800, "role_names": ["admin"]},
            headers={"Authorization": f"Bearer {_descope_session(op)}"},
        )

        _assert_machine_refused(response)
        assert descope_stores[0].calls == []

    @pytest.mark.parametrize(
        ("method", "path", "body"), ROLE_AND_MEMBER_ROUTES, ids=[f"{m} {p}" for m, p, _ in ROLE_AND_MEMBER_ROUTES]
    )
    def test_every_role_and_member_route_refuses_it(self, op, descope_client, descope_stores, method, path, body):
        response = descope_client.request(
            method, path, json=body, headers={"Authorization": f"Bearer {_descope_session(op)}"}
        )

        _assert_machine_refused(response)
        assert descope_stores[1].calls == []

    def test_an_empty_authentication_method_list_names_no_person(self, op, descope_client):
        response = descope_client.get("/api/keys", headers={"Authorization": f"Bearer {_descope_session(op, amr=[])}"})

        _assert_machine_refused(response)

    def test_its_grants_still_serve_the_routes_a_machine_may_use(self, op, descope_client):
        response = descope_client.get(ADMIN_ROLE, headers={"Authorization": f"Bearer {_descope_session(op)}"})

        assert (response.status_code, response.json()) == (200, {"roles": ["owner", "admin"]})


class TestADescopeUserSessionIsAPerson:
    def test_it_lists_the_tenants_keys(self, op, descope_client, descope_stores):
        response = descope_client.get("/api/keys", headers={"Authorization": f"Bearer {_user_session(op)}"})

        assert (response.status_code, response.json()) == (200, {"keys": []})
        assert descope_stores[0].calls == ["list"]

    def test_it_lists_the_tenants_members(self, op, descope_client, descope_stores):
        response = descope_client.get("/api/members", headers={"Authorization": f"Bearer {_user_session(op)}"})

        assert (response.status_code, response.json()) == (200, {"members": []})
        assert descope_stores[1].calls == ["list_members"]


class TestDescopeAccessKeyReader:
    @pytest.mark.parametrize(
        ("claims", "access_key"),
        [
            ({"sub": ACCESS_KEY_ID}, ACCESS_KEY_ID),
            ({"sub": "ignored", "dsub": ACCESS_KEY_ID}, ACCESS_KEY_ID),
            ({"sub": ACCESS_KEY_ID, "amr": []}, ACCESS_KEY_ID),
            ({"sub": ACCESS_KEY_ID, "amr": "email"}, ACCESS_KEY_ID),
            ({}, ""),
            ({"sub": DESCOPE_USER_ID, "amr": ["email"]}, None),
            ({"sub": DESCOPE_USER_ID, "damr": ["oauth"]}, None),
        ],
        ids=[
            "no amr",
            "subject overridden by a template",
            "empty amr",
            "amr not a list",
            "no subject either",
            "user session",
            "user session under a template overriding amr",
        ],
    )
    def test_a_session_naming_no_authentication_method_is_an_access_keys(self, claims, access_key):
        assert descope_access_key(claims) == access_key
