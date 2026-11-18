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

    def test_the_suite_refuses_an_unknown_provider_once_at_configure(self, monkeypatch):
        from tests.e2e import conftest as e2e_conftest

        monkeypatch.setenv("E2E_PROVIDER", "auth0")

        with pytest.raises(pytest.UsageError, match="E2E_PROVIDER='auth0'"):
            e2e_conftest.pytest_configure(None)


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


def _environment_names_read(module: Path) -> set[str]:
    """Every ``os.environ.get("X")`` / ``os.environ["X"]`` name ``module`` reads."""
    names = set()
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            target, key = node.func.value, node.args[0] if node.args else None
        elif isinstance(node, ast.Subscript):
            target, key = node.value, node.slice
        else:
            continue
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "environ"
            and isinstance(key, ast.Constant)
            and isinstance(key.value, str)
        ):
            names.add(key.value)
    return names


class TestInternalEndpointSecrets:
    """A Keycloak or Zitadel run needs the secrets the internal-endpoint tests would otherwise skip without."""

    def test_they_are_every_variable_the_internal_endpoint_tests_read(self):
        modules = ("test_internal_endpoints_e2e.py", "test_multi_idp_e2e.py")
        read = set().union(*(_environment_names_read(SUITE_DIR / module) for module in modules))
        assert set(na.INTERNAL_ENDPOINT_SECRETS) == read

    @pytest.mark.parametrize("provider", [na.KEYCLOAK, na.ZITADEL])
    def test_a_provider_run_names_each_one_unset(self, provider):
        environment = {"DESCOPE_WEBHOOK_SECRET": "set", "INTERNAL_IDENTITY_KEY": ""}
        assert na.missing_internal_endpoint_secrets(provider, environment) == [
            "DESCOPE_FLOW_SYNC_SECRET",
            "INTERNAL_IDENTITY_KEY",
        ]

    @pytest.mark.parametrize("provider", [na.KEYCLOAK, na.ZITADEL])
    def test_a_provider_run_with_all_of_them_misses_none(self, provider):
        environment = dict.fromkeys(na.INTERNAL_ENDPOINT_SECRETS, "set")
        assert na.missing_internal_endpoint_secrets(provider, environment) == []

    def test_the_descope_run_still_skips_instead(self):
        assert na.missing_internal_endpoint_secrets(na.DESCOPE, {}) == []
