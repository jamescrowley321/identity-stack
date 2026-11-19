"""The relationship stores: selection, startup, the Descope and OpenFGA stores, their user directories and errors."""

import ast
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from app import main
from app.dependencies import identity
from app.dependencies.capabilities import served
from app.dependencies.fga import store_error_response
from app.middleware.providers import descope_config, ory_config
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.adapters.ory import OrySyncAdapter
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.descope import DescopeManagementClient
from app.services.openfga import (
    MODEL_IN_TERRAFORM,
    OpenFgaApi,
    OpenFgaRelationshipStore,
    OpenFgaSettings,
)
from app.services.relationship_store import (
    DescopeRelationshipStore,
    Relation,
    RelationshipStoreError,
    RelationshipStoreErrorKind,
    RelationshipStoreFault,
    RelationshipStoreKind,
    RelationshipStoreNotAvailable,
)
from app.services.relationship_users import (
    CanonicalUserDirectory,
    DescopeUserDirectory,
    DirectoryUser,
    UserDirectoryUnavailable,
)
from tests.helpers.relationship_store_contract import RelationshipStoreContract
from tests.unit.test_zitadel_sync_selection import _Engine

APP = Path(__file__).resolve().parents[2] / "app"

# The DescopeManagementClient methods that read or write its FGA store.
DESCOPE_FGA_METHODS = frozenset(
    {
        "check_permission",
        "create_relation",
        "delete_relation",
        "get_fga_schema",
        "list_all_relations",
        "list_relations",
        "list_user_resources",
        "update_fga_schema",
    }
)
# The client's own module may use its methods anywhere; the store's module only in this class.
CLIENT_MODULE = APP / "services" / "descope.py"
STORE_MODULE = APP / "services" / "relationship_store.py"
STORE_CLASS = "DescopeRelationshipStore"


def _mentions_descope(node: ast.AST) -> bool:
    return "descope" in ast.unparse(node).lower()


def _descope_names(tree: ast.AST) -> set[str]:
    """Names the module binds to the Descope client: assigned from it, or parameters annotated with it."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and _mentions_descope(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {t.id for t in targets if isinstance(t, ast.Name)}
        elif (
            isinstance(node, ast.AnnAssign) and _mentions_descope(node.annotation) and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
        elif isinstance(node, ast.arg) and node.annotation is not None and _mentions_descope(node.annotation):
            names.add(node.arg)
    return names


def _descope_fga_references(source: str, exempt_class: str | None = None) -> list[str]:
    """Each reference to one of the Descope client's FGA methods, on a receiver that is the Descope client.

    A receiver is the Descope client when its expression names Descope (``request.app.state.descope_client``,
    ``self._descope``) or is a name the module binds to one (``client = request.app.state.descope_client``,
    ``client: DescopeManagementClient``). A reference is any attribute access, called or not, and a
    ``getattr`` with the method's name as a literal. Code inside ``exempt_class`` is not checked.
    """
    tree = ast.parse(source)
    bound = _descope_names(tree)
    exempt = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == exempt_class
        for inner in ast.walk(node)
    }

    def is_descope(receiver: ast.AST) -> bool:
        return _mentions_descope(receiver) or (isinstance(receiver, ast.Name) and receiver.id in bound)

    found = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        if isinstance(node, ast.Attribute) and node.attr in DESCOPE_FGA_METHODS and is_descope(node.value):
            found.append(f"{node.lineno} .{node.attr}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in DESCOPE_FGA_METHODS
            and is_descope(node.args[0])
        ):
            found.append(f"{node.lineno} getattr {node.args[1].value}")
    return found


def test_the_client_has_every_listed_fga_method():
    assert DESCOPE_FGA_METHODS <= set(dir(DescopeManagementClient))


def test_no_module_outside_the_store_reaches_descopes_fga_store():
    references = {
        path.relative_to(APP.parent).as_posix(): found
        for path in sorted(APP.rglob("*.py"))
        if path != CLIENT_MODULE
        and (found := _descope_fga_references(path.read_text(), STORE_CLASS if path == STORE_MODULE else None))
    }
    assert references == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("await request.app.state.descope_client.check_permission(t, r, rel, s)", ["1 .check_permission"]),
        ("method = request.app.state.descope_client.create_relation", ["1 .create_relation"]),
        ("method = getattr(request.app.state.descope_client, 'get_fga_schema')", ["1 getattr get_fga_schema"]),
        ("client = request.app.state.descope_client\nawait client.list_relations(t, r)", ["2 .list_relations"]),
        (
            "async def f(c: DescopeManagementClient):\n    await c.list_user_resources(t, r, s)",
            ["2 .list_user_resources"],
        ),
        ("await self._descope.delete_relation(t, r, rel, s)", ["1 .delete_relation"]),
        ("await openfga.list_relations(t, r)", []),
        ("await store.relations(tenant, t, r)", []),
        ("class DescopeRelationshipStore:\n    def f(self):\n        return self.descope.list_relations", []),
    ],
)
def test_the_guard_finds_every_reference_to_the_descope_client(source, expected):
    assert _descope_fga_references(source, STORE_CLASS) == expected


# Modules that build or hold the Descope client for its other APIs; the guard above
# keeps them off its FGA methods.
DESCOPE_CLIENT_IMPORTERS = frozenset(
    {
        "app/main.py",
        "app/services/adapters/descope.py",
        "app/services/descope_management.py",
        "app/services/reconciliation.py",
        "app/services/relationship_store.py",
        "app/services/relationship_users.py",
    }
)


def _imports_the_client(path: Path) -> bool:
    tree = ast.parse(path.read_text(), filename=str(path))
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.services.descope"
        and any(alias.name == "DescopeManagementClient" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_no_other_module_imports_the_descope_client():
    importers = {path.relative_to(APP.parent).as_posix() for path in APP.rglob("*.py") if _imports_the_client(path)}
    assert importers == DESCOPE_CLIENT_IMPORTERS


MODEL_JSON = APP.parent.parent / "infra" / "openfga" / "model.json"


class TestSelection:
    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("descope", RelationshipStoreKind.DESCOPE),
            ("keycloak", RelationshipStoreKind.OPENFGA),
            ("zitadel", RelationshipStoreKind.OPENFGA),
        ],
    )
    def test_unset_follows_the_sync_provider(self, monkeypatch, provider, expected):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "")
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        assert identity.relationship_store_kind() is expected

    @pytest.mark.parametrize("setting", list(RelationshipStoreKind))
    @pytest.mark.parametrize("provider", ["descope", "keycloak"])
    def test_a_setting_selects_its_store_whatever_the_provider(self, monkeypatch, provider, setting):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", setting.value)
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        assert identity.relationship_store_kind() is setting

    def test_an_unknown_store_is_refused(self, monkeypatch):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "keto")
        with pytest.raises(RelationshipStoreNotAvailable) as raised:
            identity.relationship_store_kind()
        assert (raised.value.fault, raised.value.selected) == (RelationshipStoreFault.UNKNOWN, "keto")

    @pytest.mark.parametrize(
        ("adapter", "store"),
        [
            (DescopeSyncAdapter, RelationshipStoreKind.DESCOPE),
            (KeycloakSyncAdapter, RelationshipStoreKind.OPENFGA),
            (ZitadelSyncAdapter, RelationshipStoreKind.OPENFGA),
            (OrySyncAdapter, RelationshipStoreKind.OPENFGA),
            (NoOpSyncAdapter, RelationshipStoreKind.OPENFGA),
        ],
    )
    def test_each_adapter_declares_its_default_store(self, adapter, store):
        """Only Descope has a Zanzibar-style store of its own; Ory gets OpenFGA too (owner decision 1 of #672)."""
        assert adapter.relationship_store is store

    def test_every_store_but_none_is_registered(self):
        assert set(identity.RELATIONSHIP_STORES) == set(RelationshipStoreKind) - {RelationshipStoreKind.NONE}

    async def test_the_built_store_checks_in_the_descope_client(self, monkeypatch):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "descope")
        request = MagicMock()
        request.app.state.descope_client = AsyncMock(spec=DescopeManagementClient)
        request.app.state.descope_client.check_permission.return_value = True
        store = identity.get_relationship_store(request)
        assert await store.check("t1", "document", "d1", "can_view", "u1") is True
        assert request.app.state.descope_client.check_permission.await_args.args == (
            "document",
            "t1:d1",
            "can_view",
            "u1",
        )

    def test_each_store_has_the_directory_of_its_subjects(self, monkeypatch):
        request = MagicMock()
        for setting, directory in (("descope", DescopeUserDirectory), ("openfga", CanonicalUserDirectory)):
            monkeypatch.setattr(identity, "RELATIONSHIP_STORE", setting)
            assert type(identity.get_user_directory(request, session=MagicMock())) is directory


def _principal(provider: str) -> MagicMock:
    """A request whose recorded provider is ``provider`` (``descope`` or ``ory``)."""
    request = MagicMock()
    request.state.claims = {"sub": "s"}
    request.state.provider = (
        descope_config("P1") if provider == "descope" else ory_config("https://ory.test", "https://api.test")
    )
    request.state.principal.identity.authentication_type = provider
    return request


class TestWhomEachStoreServes:
    """Each store serves only the principals whose ids it keys on (conventions §18)."""

    @pytest.mark.parametrize(
        ("setting", "provider", "expected"),
        [
            ("descope", "descope", RelationshipStoreKind.DESCOPE),
            ("descope", "ory", RelationshipStoreKind.NONE),
            ("openfga", "descope", RelationshipStoreKind.NONE),
            ("openfga", "ory", RelationshipStoreKind.OPENFGA),
            ("none", "ory", RelationshipStoreKind.NONE),
        ],
    )
    def test_served(self, monkeypatch, setting, provider, expected):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", setting)
        assert served(_principal(provider)).relationship_store is expected


def _lifespan_environment(monkeypatch, setting: str) -> None:
    monkeypatch.setattr(identity, "RELATIONSHIP_STORE", setting)
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "descope")
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "P-local-test")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "local-test-key")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(_Engine, "instances", [])
    monkeypatch.setattr(main, "get_engine", _Engine)
    monkeypatch.setattr(main, "init_telemetry", lambda **_kwargs: None)


OPENFGA_ENVIRONMENT = {
    "OPENFGA_API_URL": "http://openfga.test/",
    "OPENFGA_STORE_ID": "store-1",
    "OPENFGA_AUTHORIZATION_MODEL_ID": "model-1",
}


class TestStartup:
    async def test_an_unknown_store_stops_startup(self, monkeypatch):
        _lifespan_environment(monkeypatch, "keto")
        served_requests = []

        with pytest.raises(RelationshipStoreNotAvailable) as raised:
            async with main.lifespan(FastAPI()):
                served_requests.append(True)

        assert served_requests == []
        assert raised.value.fault is RelationshipStoreFault.UNKNOWN
        assert [engine.disposed for engine in _Engine.instances] == [True]

    @pytest.mark.parametrize("unset", sorted(OPENFGA_ENVIRONMENT))
    async def test_openfga_without_a_setting_stops_startup_naming_it(self, monkeypatch, unset):
        _lifespan_environment(monkeypatch, "openfga")
        for name, value in OPENFGA_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv(unset)

        with pytest.raises(RelationshipStoreNotAvailable, match=unset) as raised:
            async with main.lifespan(FastAPI()):
                pass

        assert raised.value.fault is RelationshipStoreFault.MISSING_SETTING
        assert [engine.disposed for engine in _Engine.instances] == [True]

    @pytest.mark.parametrize(("status", "body"), [(404, {"code": "authorization_model_not_found"}), (500, {})])
    async def test_openfga_without_the_model_stops_startup(self, monkeypatch, status, body):
        _lifespan_environment(monkeypatch, "openfga")
        for name, value in OPENFGA_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        clients = _mock_clients(monkeypatch, lambda _request: httpx.Response(status, json=body))

        with pytest.raises(RelationshipStoreNotAvailable, match="model-1") as raised:
            async with main.lifespan(FastAPI()):
                pass

        assert raised.value.fault is RelationshipStoreFault.UNREACHABLE
        assert [client.is_closed for client in clients] == [True, True]

    async def test_openfga_reads_the_configured_model_once_and_is_closed_at_shutdown(self, monkeypatch):
        _lifespan_environment(monkeypatch, "openfga")
        for name, value in OPENFGA_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("OPENFGA_API_TOKEN", "local-test-preshared")
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"authorization_model": {"id": "model-1"}})

        clients = _mock_clients(monkeypatch, answer)
        app = FastAPI()

        async with main.lifespan(app):
            assert app.state.openfga.settings == OpenFgaSettings(
                "http://openfga.test", "store-1", "model-1", "local-test-preshared"
            )

        assert [(r.method, str(r.url), r.headers["authorization"]) for r in seen] == [
            ("GET", "http://openfga.test/stores/store-1/authorization-models/model-1", "Bearer local-test-preshared")
        ]
        assert [client.is_closed for client in clients] == [True, True]


def _mock_clients(monkeypatch, handler) -> list[httpx.AsyncClient]:
    """Every client the lifespan builds answers with ``handler``; returns them as built."""
    built: list[httpx.AsyncClient] = []

    def build(*, timeout: float) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        built.append(client)
        return client

    monkeypatch.setattr(main, "outbound_client", build)
    monkeypatch.setattr(identity, "outbound_client", build)
    return built


def _status_error(status: int, text: str = "error") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/authz/re/save")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request, text=text)
    )


class TestDescopeStore:
    @pytest.fixture
    def client(self):
        return AsyncMock(spec=DescopeManagementClient)

    async def test_keys_objects_by_tenant(self, client):
        client.check_permission.return_value = True
        store = DescopeRelationshipStore(client)
        assert await store.check("t1", "document", "d1", "can_view", "u1") is True
        await store.write("t1", "document", "d1", "owner", "u1")
        await store.delete("t1", "document", "d1", "owner", "u1")
        assert client.check_permission.await_args.args == ("document", "t1:d1", "can_view", "u1")
        assert client.create_relation.await_args.args == ("document", "t1:d1", "owner", "u1")
        assert client.delete_relation.await_args.args == ("document", "t1:d1", "owner", "u1")

    async def test_objects_are_the_tenants_own_ids(self, client):
        client.list_user_resources.return_value = [
            {"resource": "t1:d1"},
            {"resource": "t2:d2"},
            {"resource": None},
            "junk",
        ]
        assert await DescopeRelationshipStore(client).objects("t1", "document", "can_view", "u1") == ["d1"]

    async def test_relations_are_typed(self, client):
        client.list_all_relations.return_value = [
            {"resource": "t1:d1", "relationDefinition": "owner", "target": "u1"},
            {"resource": "t1:d1", "relationDefinition": "", "target": "u2"},
            {"resource": "t1:d1", "relationDefinition": "viewer"},
            None,
        ]
        assert await DescopeRelationshipStore(client).all_relations("t1", "document", "d1") == [
            Relation(relation="owner", subject="u1")
        ]
        assert client.list_all_relations.await_args.args == ("document", "t1:d1")

    async def test_an_unset_schema_reads_empty(self, client):
        client.get_fga_schema.return_value = None
        assert await DescopeRelationshipStore(client).read_schema() == ""

    @pytest.mark.parametrize(
        ("raised", "kind", "status"),
        [
            (ValueError("target contains invalid characters"), RelationshipStoreErrorKind.INVALID, 422),
            (json.JSONDecodeError("bad", "x", 0), RelationshipStoreErrorKind.UNAVAILABLE, 502),
            (_status_error(400, '{"message": "unknown relation"}'), RelationshipStoreErrorKind.REFUSED, 400),
            (_status_error(404), RelationshipStoreErrorKind.ABSENT, 502),
            (_status_error(503), RelationshipStoreErrorKind.UNAVAILABLE, 502),
            (
                httpx.ConnectError("refused", request=httpx.Request("POST", "https://api.descope.com")),
                RelationshipStoreErrorKind.UNAVAILABLE,
                502,
            ),
        ],
    )
    async def test_every_failure_is_a_typed_store_error(self, client, raised, kind, status):
        client.create_relation.side_effect = raised

        with pytest.raises(RelationshipStoreError) as caught:
            await DescopeRelationshipStore(client).write("t1", "document", "d1", "owner", "u1")

        assert (caught.value.kind, store_error_response(caught.value).status_code) == (kind, status)


# Descope's document schema (infra/fga.tf): the permissions each relation gives.
_DERIVED = {"can_view": {"viewer", "editor", "owner"}, "can_edit": {"editor", "owner"}, "can_delete": {"owner"}}


class FakeDescopeFga:
    """Descope's FGA methods over an in-memory tuple set, with the schema's derivations."""

    def __init__(self) -> None:
        self.tuples: set[tuple[str, str, str, str]] = set()

    async def create_relation(self, resource_type, resource, relation, target):
        self.tuples.add((resource_type, resource, relation, target))

    async def delete_relation(self, resource_type, resource, relation, target):
        self.tuples.discard((resource_type, resource, relation, target))

    async def check_permission(self, resource_type, resource, relation, target):
        held = _DERIVED.get(relation, {relation})
        return any((resource_type, resource, r, target) in self.tuples for r in held)

    def _dicts(self, resource_type, resource, relation=None):
        return [
            {"resource": res, "relationDefinition": rel, "target": target}
            for (rt, res, rel, target) in sorted(self.tuples)
            if rt == resource_type and res == resource and (relation is None or rel == relation)
        ]

    async def list_relations(self, resource_type, resource, relation=None):
        return self._dicts(resource_type, resource, relation)

    async def list_all_relations(self, resource_type, resource):
        return self._dicts(resource_type, resource)

    async def list_user_resources(self, resource_type, relation, target):
        resources = {res for (rt, res, _rel, _target) in self.tuples if rt == resource_type}
        return [
            {"resource": res}
            for res in sorted(resources)
            if await self.check_permission(resource_type, res, relation, target)
        ]

    async def get_fga_schema(self):
        return (APP.parent.parent / "infra" / "fga.tf").read_text()


class TestDescopeStoreContract(RelationshipStoreContract):
    """The contract, with Descope's client replaced at its FGA methods."""

    @pytest.fixture
    def store(self):
        return DescopeRelationshipStore(FakeDescopeFga())


def _openfga(handler) -> OpenFgaRelationshipStore:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)
    return OpenFgaRelationshipStore(OpenFgaApi(OpenFgaSettings("http://openfga.test", "store-1", "model-1"), http))


class TestOpenFgaStore:
    """The requests the store sends and how it reads the answers. The contract runs against a real server."""

    async def test_a_tuple_names_the_tenant_in_the_object_and_the_user_type(self):
        seen: list[tuple[str, dict]] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"allowed": True} if request.url.path.endswith("/check") else {})

        store = _openfga(answer)
        assert await store.check("t1", "document", "d1", "can_view", "u1") is True
        await store.write("t1", "document", "d1", "owner", "u1")
        await store.delete("t1", "document", "d1", "owner", "u1")

        key = {"user": "user:u1", "relation": "owner", "object": "document:t1/d1"}
        assert seen == [
            (
                "/stores/store-1/check",
                {"authorization_model_id": "model-1", "tuple_key": {**key, "relation": "can_view"}},
            ),
            (
                "/stores/store-1/write",
                {"authorization_model_id": "model-1", "writes": {"tuple_keys": [key], "on_duplicate": "ignore"}},
            ),
            (
                "/stores/store-1/write",
                {"authorization_model_id": "model-1", "deletes": {"tuple_keys": [key], "on_missing": "ignore"}},
            ),
        ]

    async def test_relations_are_read_page_by_page_as_user_ids(self):
        pages = {
            "": {
                "tuples": [
                    {"key": {"user": "user:u1", "relation": "owner", "object": "document:t1/d1"}},
                    {"key": {"user": "group:g#member", "relation": "viewer", "object": "document:t1/d1"}},
                ],
                "continuation_token": "next",
            },
            "next": {
                "tuples": [{"key": {"user": "user:u2", "relation": "viewer", "object": "document:t1/d1"}}],
                "continuation_token": "",
            },
        }
        bodies: list[dict] = []

        def answer(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(200, json=pages[body.get("continuation_token", "")])

        assert await _openfga(answer).all_relations("t1", "document", "d1") == [
            Relation("owner", "u1"),
            Relation("viewer", "u2"),
        ]
        assert [body["tuple_key"] for body in bodies] == [{"object": "document:t1/d1"}] * 2

    async def test_objects_are_the_tenants_own_ids(self):
        def answer(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"objects": ["document:t1/d1", "document:t2/d2", "document:t1/", "folder:t1/f"]}
            )

        assert await _openfga(answer).objects("t1", "document", "can_view", "u1") == ["d1"]

    async def test_the_schema_is_read_only(self):
        with pytest.raises(RelationshipStoreError) as caught:
            await _openfga(lambda _request: httpx.Response(500)).write_schema("model\n  schema 1.1")

        assert (caught.value.kind, caught.value.detail) == (
            RelationshipStoreErrorKind.SCHEMA_READ_ONLY,
            MODEL_IN_TERRAFORM,
        )
        assert store_error_response(caught.value).status_code == 409

    @pytest.mark.parametrize(
        ("answer", "kind"),
        [
            (
                httpx.Response(400, json={"code": "validation_error", "message": "relation 'x' not found"}),
                RelationshipStoreErrorKind.REFUSED,
            ),
            (httpx.Response(404, json={"code": "store_id_not_found"}), RelationshipStoreErrorKind.UNAVAILABLE),
            (httpx.Response(500, text="boom"), RelationshipStoreErrorKind.UNAVAILABLE),
            (httpx.Response(200, text="not json"), RelationshipStoreErrorKind.UNAVAILABLE),
            (httpx.Response(200, json=["not", "an", "object"]), RelationshipStoreErrorKind.UNAVAILABLE),
        ],
    )
    async def test_every_failure_is_a_typed_store_error(self, answer, kind):
        with pytest.raises(RelationshipStoreError) as caught:
            await _openfga(lambda _request: answer).check("t1", "document", "d1", "x", "u1")

        assert caught.value.kind is kind

    async def test_a_refusal_carries_openfgas_message(self):
        answer = httpx.Response(400, json={"code": "validation_error", "message": "relation 'x' not found"})
        with pytest.raises(RelationshipStoreError) as caught:
            await _openfga(lambda _request: answer).check("t1", "document", "d1", "x", "u1")

        assert caught.value.detail == "Validation error: relation 'x' not found"

    async def test_no_answer_is_unavailable(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(RelationshipStoreError) as caught:
            await _openfga(refuse).check("t1", "document", "d1", "can_view", "u1")

        assert (caught.value.kind, caught.value.detail) == (
            RelationshipStoreErrorKind.UNAVAILABLE,
            "Failed to reach OpenFGA",
        )

    def test_the_model_file_defines_the_descope_schemas_relations(self):
        model = json.loads(MODEL_JSON.read_text())
        document = {t["type"]: t for t in model["type_definitions"]}["document"]

        assert set(document["relations"]) == {"owner", "editor", "viewer"} | set(_DERIVED)


class TestDescopeUserDirectory:
    async def test_a_user_with_their_tenants(self):
        client = MagicMock()
        client.load_user = AsyncMock(
            return_value={"userId": "U1", "userTenants": [{"tenantId": "t1"}, {"tenantId": ""}, "junk"]}
        )
        assert await DescopeUserDirectory(client).find("a@example.test") == DirectoryUser("U1", frozenset({"t1"}))

    @pytest.mark.parametrize("answer", [{}, None, {"userTenants": []}])
    async def test_no_user_id_is_no_user(self, answer):
        client = MagicMock()
        client.load_user = AsyncMock(return_value=answer)
        assert await DescopeUserDirectory(client).find("x") is None

    async def test_a_404_is_no_user_and_any_other_failure_is_unavailable(self):
        client = MagicMock()
        client.load_user = AsyncMock(side_effect=_status_error(404))
        assert await DescopeUserDirectory(client).find("x") is None
        client.load_user = AsyncMock(side_effect=_status_error(500))
        with pytest.raises(UserDirectoryUnavailable):
            await DescopeUserDirectory(client).find("x")


class TestCanonicalUserDirectory:
    USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
    TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000bb")

    @pytest.fixture
    def repositories(self):
        users = MagicMock()
        user = SimpleNamespace(id=self.USER_ID)
        users.get = AsyncMock(side_effect=lambda user_id: user if user_id == self.USER_ID else None)
        users.get_by_email = AsyncMock(side_effect=lambda email: user if email == "a@example.test" else None)
        assignments = MagicMock()
        assignments.list_by_user = AsyncMock(return_value=[SimpleNamespace(tenant_id=self.TENANT)] * 2)
        return users, assignments

    @pytest.mark.parametrize("identifier", [str(USER_ID), str(USER_ID).upper(), "a@example.test", " a@example.test "])
    async def test_by_id_or_email(self, repositories, identifier):
        expected = DirectoryUser(str(self.USER_ID), frozenset({str(self.TENANT)}))
        assert await CanonicalUserDirectory(*repositories).find(identifier) == expected

    @pytest.mark.parametrize("identifier", [str(uuid.UUID(int=1)), "nobody@example.test", "a-username"])
    async def test_anyone_else_is_no_user(self, repositories, identifier):
        assert await CanonicalUserDirectory(*repositories).find(identifier) is None
