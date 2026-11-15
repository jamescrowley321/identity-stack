"""The context_layers import-linter contract fires on a violation and on a stale exemption."""

import grimp

from import_contracts import ContextLayersContract

RULES = ["app.routers -> app.models", "app.services -> app.models"]


def _graph(*imports: tuple[str, str]) -> grimp.ImportGraph:
    graph = grimp.ImportGraph()
    for package in ("app", "app.routers", "app.services", "app.models"):
        graph.add_module(package)
    for importer, imported in imports:
        graph.add_import(importer=importer, imported=imported)
    return graph


def _check(graph: grimp.ImportGraph, unmigrated: list[str] | None = None):
    contract = ContextLayersContract(
        name="layers",
        session_options={"root_packages": ["app"]},
        contract_options={"rules": RULES, "unmigrated": unmigrated or []},
    )
    return contract.check(graph, verbose=False)


def test_kept_when_no_rule_is_broken():
    check = _check(_graph(("app.routers.roles", "app.services.role")))

    assert check.kept


def test_broken_by_a_direct_import_of_a_forbidden_package():
    check = _check(_graph(("app.routers.roles", "app.models.identity.role")))

    assert not check.kept
    assert check.metadata["violations"] == [("app.routers.roles", "app.models.identity.role")]


def test_an_indirect_import_is_allowed():
    graph = _graph(("app.routers.roles", "app.dependencies.identity"), ("app.dependencies.identity", "app.models.x"))

    assert _check(graph).kept


def test_an_unmigrated_context_is_exempt():
    graph = _graph(("app.routers.tenants", "app.models.tenant"), ("app.services.tenant", "app.models.tenant"))

    check = _check(graph, ["Directory (#515): app.routers.tenants app.services.tenant"])

    assert check.kept


def test_an_exemption_covers_only_the_modules_it_names():
    graph = _graph(("app.routers.tenants", "app.models.tenant"), ("app.routers.roles", "app.models.tenant"))

    check = _check(graph, ["Directory (#515): app.routers.tenants"])

    assert not check.kept
    assert check.metadata["violations"] == [("app.routers.roles", "app.models.tenant")]


def test_broken_by_an_exemption_that_no_longer_breaks_a_rule():
    graph = _graph(("app.routers.tenants", "app.services.tenant"))

    check = _check(graph, ["Directory (#515): app.routers.tenants"])

    assert not check.kept
    assert check.metadata["stale"] == [("Directory (#515)", "app.routers.tenants")]


def test_broken_by_an_exemption_for_a_module_that_does_not_exist():
    check = _check(_graph(), ["Directory (#515): app.routers.gone"])

    assert not check.kept
    assert check.metadata["stale"] == [("Directory (#515)", "app.routers.gone")]
