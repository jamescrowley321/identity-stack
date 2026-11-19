"""The FGA and document routes on OpenFGA: canonical tenant and user ids, never the token's ``sub``.

An Ory principal (``CANONICAL_RBAC_PROVIDERS``) owns ``TENANT_A`` in the canonical
model. The store and the user directory are in-memory stand-ins at the
``RelationshipStore`` and ``UserDirectory`` interfaces; the OpenFGA store itself
runs the contract against the compose server
(``tests/integration/test_openfga_relationship_store.py``).
"""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Ok
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.dependencies import identity, rbac
from app.dependencies.identity import get_user_directory
from app.dependencies.rbac import get_rbac_session
from app.errors.identity import IdentityErrorRaised
from app.errors.problem_detail import identity_error_raised_handler
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import descope_config, ory_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from app.services.openfga import MODEL_IN_TERRAFORM
from app.services.relationship_store import (
    Relation,
    RelationshipStoreError,
    RelationshipStoreErrorKind,
    RelationshipStoreKind,
)
from app.services.relationship_users import DirectoryUser
from tests.unit.test_descope_backed_routes import (
    DESCOPE_ADMIN,
    DESCOPE_PROJECT,
    ORY_AUDIENCE,
    ORY_ISSUER,
    ORY_OWNER,
    TENANT_A,
    TENANT_B,
    _bearer,
)

CALLER = "11111111-0000-4000-8000-000000000001"
COLLEAGUE = "11111111-0000-4000-8000-000000000002"
OUTSIDER = "11111111-0000-4000-8000-000000000003"


class MemoryStore:
    """A ``RelationshipStore`` over a set of (tenant, type, id, relation, subject) tuples."""

    DERIVED = {"can_view": {"viewer", "editor", "owner"}, "can_edit": {"editor", "owner"}, "can_delete": {"owner"}}

    def __init__(self) -> None:
        self.tuples: set[tuple[str, str, str, str, str]] = set()

    async def check(self, tenant_id, object_type, object_id, relation, subject):
        held = self.DERIVED.get(relation, {relation})
        return any((tenant_id, object_type, object_id, r, subject) in self.tuples for r in held)

    async def write(self, tenant_id, object_type, object_id, relation, subject):
        self.tuples.add((tenant_id, object_type, object_id, relation, subject))

    async def delete(self, tenant_id, object_type, object_id, relation, subject):
        self.tuples.discard((tenant_id, object_type, object_id, relation, subject))

    async def read_schema(self):
        return "{}"

    async def write_schema(self, schema):
        raise RelationshipStoreError(RelationshipStoreErrorKind.SCHEMA_READ_ONLY, MODEL_IN_TERRAFORM)

    async def relations(self, tenant_id, object_type, object_id, relation=None):
        return [
            Relation(r, s)
            for (t, ty, o, r, s) in sorted(self.tuples)
            if (t, ty, o) == (tenant_id, object_type, object_id) and relation in (None, r)
        ]

    async def all_relations(self, tenant_id, object_type, object_id):
        return await self.relations(tenant_id, object_type, object_id)

    async def objects(self, tenant_id, object_type, relation, subject):
        ids = sorted({o for (t, ty, o, _r, _s) in self.tuples if (t, ty) == (tenant_id, object_type)})
        return [o for o in ids if await self.check(tenant_id, object_type, o, relation, subject)]


class MemoryDirectory:
    USERS = {
        CALLER: DirectoryUser(CALLER, frozenset({TENANT_A})),
        COLLEAGUE: DirectoryUser(COLLEAGUE, frozenset({TENANT_A})),
        OUTSIDER: DirectoryUser(OUTSIDER, frozenset({TENANT_B})),
    }
    EMAILS = {"colleague@acme.test": COLLEAGUE, "outsider@globex.test": OUTSIDER}

    async def find(self, identifier):
        return self.USERS.get(self.EMAILS.get(identifier, identifier))


class _Canonical:
    async def resolve(self, *, provider: str, sub: str):
        return Ok(
            {
                "user": {"id": CALLER, "status": "active"},
                "roles": [{"tenant_id": TENANT_A, "role_name": "owner", "permissions": []}],
            }
        )


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
async def client(monkeypatch, store):
    monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "openfga")
    # The registration's own store, built over the stand-in: require_fga builds it outside FastAPI's overrides.
    registration = identity.RELATIONSHIP_STORES[RelationshipStoreKind.OPENFGA]
    monkeypatch.setitem(
        identity.RELATIONSHIP_STORES, RelationshipStoreKind.OPENFGA, replace(registration, build=lambda _state: store)
    )
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: _Canonical())
    limiter.reset()
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _session():
        async with sessions() as session:
            yield session

    async def _rbac_session():
        yield MagicMock()

    app = FastAPI()
    app.state.limiter = limiter
    app.state.descope_client = AsyncMock()
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_exception_handler(IdentityErrorRaised, identity_error_raised_handler)
    app.include_router(shipped_app.router)
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_rbac_session] = _rbac_session
    app.dependency_overrides[get_user_directory] = MemoryDirectory
    app.add_middleware(
        GatewayClaimsMiddleware, providers=[descope_config(DESCOPE_PROJECT), ory_config(ORY_ISSUER, ORY_AUDIENCE)]
    )
    # Not entered as a context manager: the shipped lifespan would start.
    yield TestClient(app), app
    await engine.dispose()


OWNER = _bearer(ORY_OWNER)


def _create(test_client) -> str:
    response = test_client.post("/api/documents", json={"title": "Plan"}, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()["id"]


class TestDocuments:
    def test_the_owner_tuple_keys_on_the_canonical_tenant_and_user(self, client, store):
        test_client, _ = client
        document_id = _create(test_client)

        assert store.tuples == {(TENANT_A, "document", document_id, "owner", CALLER)}
        listed = test_client.get("/api/documents", headers=OWNER).json()["documents"]
        assert {d["id"]: d["created_by"] for d in listed} == {document_id: CALLER}
        assert test_client.get(f"/api/documents/{document_id}", headers=OWNER).status_code == 200

    def test_a_document_the_caller_holds_nothing_on_is_refused(self, client, store):
        test_client, _ = client
        document_id = _create(test_client)
        store.tuples.clear()

        assert test_client.get(f"/api/documents/{document_id}", headers=OWNER).status_code == 403

    def test_sharing_by_email_keys_on_the_colleagues_canonical_id(self, client, store):
        test_client, _ = client
        document_id = _create(test_client)

        response = test_client.post(
            f"/api/documents/{document_id}/share",
            json={"user_id": "colleague@acme.test", "relation": "viewer"},
            headers=OWNER,
        )

        assert (response.status_code, response.json()["user_id"]) == (200, COLLEAGUE)
        assert (TENANT_A, "document", document_id, "viewer", COLLEAGUE) in store.tuples

    @pytest.mark.parametrize(
        ("target", "status"), [("outsider@globex.test", 403), ("nobody@acme.test", 404), (CALLER, 400)]
    )
    def test_sharing_with_anyone_else_is_refused(self, client, store, target, status):
        test_client, _ = client
        document_id = _create(test_client)

        response = test_client.post(
            f"/api/documents/{document_id}/share", json={"user_id": target, "relation": "viewer"}, headers=OWNER
        )

        assert response.status_code == status
        assert store.tuples == {(TENANT_A, "document", document_id, "owner", CALLER)}

    def test_revoking_removes_the_colleagues_tuples(self, client, store):
        test_client, _ = client
        document_id = _create(test_client)
        store.tuples.add((TENANT_A, "document", document_id, "editor", COLLEAGUE))

        response = test_client.delete(f"/api/documents/{document_id}/share/{COLLEAGUE}", headers=OWNER)

        assert (response.status_code, response.json()["user_id"]) == (200, COLLEAGUE)
        assert store.tuples == {(TENANT_A, "document", document_id, "owner", CALLER)}


class TestFgaAdministration:
    def test_a_relation_written_by_email_keys_on_the_canonical_id(self, client, store):
        test_client, _ = client
        body = {"resource_type": "document", "resource_id": "d1", "relation": "viewer", "target": "colleague@acme.test"}

        response = test_client.post("/api/fga/relations", json=body, headers=OWNER)

        assert (response.status_code, response.json()["target"]) == (201, COLLEAGUE)
        assert store.tuples == {(TENANT_A, "document", "d1", "viewer", COLLEAGUE)}

    def test_a_user_outside_the_tenant_is_left_unresolved(self, client, store):
        test_client, _ = client
        body = {
            "resource_type": "document",
            "resource_id": "d1",
            "relation": "viewer",
            "target": "outsider@globex.test",
        }

        test_client.post("/api/fga/relations", json=body, headers=OWNER)

        assert store.tuples == {(TENANT_A, "document", "d1", "viewer", "outsider@globex.test")}

    def test_the_schema_is_read_only(self, client):
        test_client, _ = client

        response = test_client.put("/api/fga/schema", json={"schema": "model"}, headers=OWNER)

        assert (response.status_code, response.json()) == (409, {"detail": MODEL_IN_TERRAFORM})


def test_a_descope_principal_is_not_served_by_openfga(client, store):
    test_client, app = client

    response = test_client.get("/api/documents", headers=_bearer(DESCOPE_ADMIN))

    assert (response.status_code, response.json()["type"]) == (404, "/errors/capability-missing")
    assert app.state.descope_client.mock_calls == []
