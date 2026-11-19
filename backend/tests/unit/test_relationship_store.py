"""The relationship store: its selection, the Descope store, and that only the store calls Descope's FGA methods."""

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
# The client itself and the store over it.
FGA_CALLERS = frozenset({APP / "services" / "descope.py", APP / "services" / "relationship_store.py"})


def _fga_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        f"{path.relative_to(APP.parent)}:{node.lineno} .{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in DESCOPE_FGA_METHODS
    ]


def test_the_client_has_every_listed_fga_method():
    assert DESCOPE_FGA_METHODS <= set(dir(DescopeManagementClient))


def test_no_module_outside_the_store_calls_descopes_fga_store():
    calls = [call for path in sorted(APP.rglob("*.py")) if path not in FGA_CALLERS for call in _fga_calls(path)]
    assert calls == []


# Modules that build or hold the Descope client for its other APIs; the test above
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

    def test_the_built_store_wraps_the_descope_client(self, monkeypatch):
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "descope")
        request = MagicMock()
        store = identity.get_relationship_store(request)
        assert isinstance(store, DescopeRelationshipStore)
        assert store._client is request.app.state.descope_client


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

    async def test_relations_strip_the_tenant_key(self, client):
        client.list_all_relations.return_value = [{"resource": "t1:d1", "relationDefinition": "owner", "target": "u1"}]
        assert await DescopeRelationshipStore(client).all_relations("t1", "document", "d1") == [
            {"resource": "d1", "relationDefinition": "owner", "target": "u1"}
        ]

    async def test_an_unset_schema_reads_empty(self, client):
        client.get_fga_schema.return_value = None
        assert await DescopeRelationshipStore(client).read_schema() == ""
