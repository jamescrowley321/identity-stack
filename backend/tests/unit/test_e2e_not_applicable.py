"""The shared e2e suite's provider setting and its not-applicable table (``tests/e2e/not_applicable.py``).

The table is the one place a test is kept from a provider's run, so a key that
names no test would silently keep nothing out (or, once the test is renamed,
let it back in unannounced), and a Descope entry would change the Descope run.
"""

import ast
from pathlib import Path

import pytest

from tests.e2e import not_applicable as na

SUITE_DIR = Path(__file__).resolve().parents[1] / "e2e"


def _tests_in(module: Path) -> set[str]:
    """``test_x.py``, ``test_x.py::TestY`` and ``test_x.py[::TestY]::test_z`` for every test in the module."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    names = {module.name}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            names.add(f"{module.name}::{node.name}")
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            names.add(f"{module.name}::{node.name}")
            names.update(
                f"{module.name}::{node.name}::{method.name}"
                for method in node.body
                if isinstance(method, ast.FunctionDef) and method.name.startswith("test_")
            )
    return names


SUITE_TESTS = set().union(*(_tests_in(module) for module in SUITE_DIR.glob("test_*.py")))


class TestSelectedProvider:
    @pytest.mark.parametrize("environment", [{}, {"E2E_PROVIDER": ""}, {"E2E_PROVIDER": "  "}])
    def test_unset_is_descope(self, environment):
        assert na.selected_provider(environment) == na.DESCOPE

    @pytest.mark.parametrize("name", na.PROVIDERS)
    def test_a_known_provider_is_chosen(self, name):
        assert na.selected_provider({"E2E_PROVIDER": name}) == name

    @pytest.mark.parametrize("name", ["ory", "Keycloak", "auth0"])
    def test_an_unknown_provider_is_refused(self, name):
        with pytest.raises(na.UnknownE2EProvider, match=f"E2E_PROVIDER='{name}'"):
            na.selected_provider({"E2E_PROVIDER": name})


class TestTheTable:
    def test_every_key_names_a_test_of_the_suite(self):
        assert sorted(set(na.NOT_APPLICABLE) - SUITE_TESTS) == []

    def test_the_descope_run_loses_nothing(self):
        assert {key: reasons for key, reasons in na.NOT_APPLICABLE.items() if na.DESCOPE in reasons} == {}

    def test_every_entry_names_a_provider_and_a_reason(self):
        malformed = {
            key: reasons
            for key, reasons in na.NOT_APPLICABLE.items()
            if not reasons
            or not set(reasons) <= set(na.PROVIDERS)
            or not all(isinstance(reason, str) and reason.strip() for reason in reasons.values())
        }
        assert malformed == {}

    def test_no_entry_is_covered_by_another(self):
        """A key under another key for the same provider is dead: the outer one already deselects it."""
        covered = {
            (inner, provider)
            for inner, inner_reasons in na.NOT_APPLICABLE.items()
            for outer, outer_reasons in na.NOT_APPLICABLE.items()
            for provider in set(inner_reasons) & set(outer_reasons)
            if inner != outer and inner.startswith(f"{outer}::")
        }
        assert covered == set()


class TestReasonNotApplicable:
    @pytest.fixture(autouse=True)
    def _table(self, monkeypatch):
        monkeypatch.setattr(
            na,
            "NOT_APPLICABLE",
            {
                "test_module.py": {na.KEYCLOAK: "module reason"},
                "test_other.py::TestClass": {na.ZITADEL: "class reason"},
                "test_other.py::test_one": {na.KEYCLOAK: "test reason", na.ZITADEL: "test reason z"},
            },
        )

    @pytest.mark.parametrize(
        ("nodeid", "provider", "reason"),
        [
            ("tests/e2e/test_module.py::test_any", na.KEYCLOAK, "module reason"),
            ("tests/e2e/test_module.py::TestC::test_any", na.KEYCLOAK, "module reason"),
            ("tests/e2e/test_module.py::test_any[param]", na.KEYCLOAK, "module reason"),
            ("tests/e2e/test_module.py::test_any", na.ZITADEL, None),
            ("tests/e2e/test_module.py::test_any", na.DESCOPE, None),
            ("tests/e2e/test_other.py::TestClass::test_a", na.ZITADEL, "class reason"),
            ("tests/e2e/test_other.py::TestClassic::test_a", na.ZITADEL, None),
            ("tests/e2e/test_other.py::test_one", na.KEYCLOAK, "test reason"),
            ("tests/e2e/test_other.py::test_one[a-b]", na.ZITADEL, "test reason z"),
            ("tests/e2e/test_other.py::test_one_more", na.KEYCLOAK, None),
            ("tests/e2e/test_modules.py::test_any", na.KEYCLOAK, None),
            ("tests/e2e_keycloak/test_module.py::test_any", na.KEYCLOAK, None),
        ],
    )
    def test_matches_a_key_and_what_is_below_it_only(self, nodeid, provider, reason):
        assert na.reason_not_applicable(nodeid, provider) == reason
