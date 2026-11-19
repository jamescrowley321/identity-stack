"""The relationship store: its selection, the Descope store, and that only the store reaches Descope's FGA methods."""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app import main
from app.dependencies import identity
from app.services.descope import DescopeManagementClient
from app.services.relationship_store import (
    DescopeRelationshipStore,
    Relation,
    RelationshipStoreFault,
    RelationshipStoreKind,
    RelationshipStoreNotAvailable,
)
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


class TestSelection:
    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("descope", RelationshipStoreKind.DESCOPE),
            ("keycloak", RelationshipStoreKind.NONE),
            ("zitadel", RelationshipStoreKind.NONE),
        ],
    )
    def test_unset_follows_the_sync_provider(self, monkeypatch, provider, expected):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "")
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        assert identity.relationship_store_kind() is expected

    @pytest.mark.parametrize("provider", ["descope", "keycloak"])
    def test_descope_selects_descopes_store(self, monkeypatch, provider):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "descope")
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        assert identity.relationship_store_kind() is RelationshipStoreKind.DESCOPE

    @pytest.mark.parametrize(
        ("setting", "fault"),
        [("openfga", RelationshipStoreFault.NOT_BUILT), ("keto", RelationshipStoreFault.UNKNOWN)],
    )
    def test_a_store_not_built_is_refused(self, monkeypatch, setting, fault):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", setting)
        with pytest.raises(RelationshipStoreNotAvailable) as raised:
            identity.relationship_store_kind()
        assert (raised.value.fault, raised.value.selected) == (fault, setting)

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


async def test_the_application_does_not_start_with_openfga_selected(monkeypatch):
    """Selecting OpenFGA before identity-stack#682 stops startup loudly, never falling back."""
    monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "openfga")
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "descope")
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "P-local-test")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "local-test-key")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(_Engine, "instances", [])
    monkeypatch.setattr(main, "get_engine", _Engine)
    monkeypatch.setattr(main, "init_telemetry", lambda **_kwargs: None)
    served = []

    with pytest.raises(RelationshipStoreNotAvailable) as raised:
        async with main.lifespan(FastAPI()):
            served.append(True)

    assert served == []
    assert raised.value.fault is RelationshipStoreFault.NOT_BUILT
    assert [engine.disposed for engine in _Engine.instances] == [True]


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
