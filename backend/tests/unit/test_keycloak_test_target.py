"""Which Keycloak realm the Keycloak integration tests log in to.

By default the realm ``keycloak-test`` imports, with its fixed user ids and
fixture passwords. The four ``KEYCLOAK_TEST_*`` variables point the same tests
at the realm ``infra/keycloak/`` builds, whose ids and secrets Keycloak and
Terraform generate. Half a target is refused, so a run can never mix the two. The two places that
set them, the Makefile's ``test-e2e-keycloak`` and
``infra/keycloak/scripts/check-against-compose.sh --integration-tests``, set
exactly those four (and the Makefile the base URL ``tests/helpers/keycloak.py``
reads).
"""

import json
import re
from pathlib import Path

import pytest

from tests.helpers.keycloak_fixture import KEYCLOAK_IDENTITIES
from tests.helpers.keycloak_target import (
    IMPORT_ADMIN_CLIENT_SECRET,
    IMPORT_REALM,
    IMPORT_USERS,
    TARGET_VARIABLES,
    KeycloakTestTargetMisconfigured,
    KeycloakUser,
    keycloak_test_target,
)

USERNAMES = tuple(KEYCLOAK_IDENTITIES.users)
TERRAFORM_IDS = {name: f"{index:08d}-tf00-4000-8000-000000000000" for index, name in enumerate(USERNAMES)}
TERRAFORM_PASSWORDS = {name: f"generated-{name}" for name in USERNAMES}


def _terraform_environment(**overrides: str) -> dict[str, str]:
    return {
        "KEYCLOAK_TEST_REALM": "identity-stack-terraform",
        "KEYCLOAK_TEST_USER_IDS": json.dumps(TERRAFORM_IDS),
        "KEYCLOAK_TEST_USER_PASSWORDS": json.dumps(TERRAFORM_PASSWORDS),
        "KEYCLOAK_TEST_ADMIN_CLIENT_SECRET": "generated-admin-secret",
        **overrides,
    }


def test_no_variable_targets_the_imported_realm():
    target = keycloak_test_target({})

    assert target.realm == IMPORT_REALM == "identity-stack"
    assert target.users == IMPORT_USERS
    assert target.admin_client_secret == IMPORT_ADMIN_CLIENT_SECRET == "local-only-admin-client"


def test_the_imported_realm_has_the_fixture_users():
    assert set(IMPORT_USERS) == set(USERNAMES)
    assert IMPORT_USERS["alice"] == KeycloakUser("alice", "local-only-alice", "a11ce000-0000-4000-8000-000000000001")


def test_the_four_variables_target_the_terraform_realm():
    target = keycloak_test_target(_terraform_environment())

    assert target.realm == "identity-stack-terraform"
    assert target.users == {
        name: KeycloakUser(name, TERRAFORM_PASSWORDS[name], TERRAFORM_IDS[name]) for name in USERNAMES
    }
    assert target.admin_client_secret == "generated-admin-secret"


@pytest.mark.parametrize(
    "unset",
    [
        "KEYCLOAK_TEST_REALM",
        "KEYCLOAK_TEST_USER_IDS",
        "KEYCLOAK_TEST_USER_PASSWORDS",
        "KEYCLOAK_TEST_ADMIN_CLIENT_SECRET",
    ],
)
def test_half_a_target_is_refused(unset):
    environment = _terraform_environment()
    del environment[unset]

    with pytest.raises(KeycloakTestTargetMisconfigured, match=unset):
        keycloak_test_target(environment)


def test_an_empty_variable_counts_as_unset():
    with pytest.raises(KeycloakTestTargetMisconfigured, match="KEYCLOAK_TEST_REALM"):
        keycloak_test_target(_terraform_environment(KEYCLOAK_TEST_REALM=""))


def test_a_map_that_is_not_json_is_refused():
    with pytest.raises(KeycloakTestTargetMisconfigured, match="KEYCLOAK_TEST_USER_IDS"):
        keycloak_test_target(_terraform_environment(KEYCLOAK_TEST_USER_IDS="alice=1"))


@pytest.mark.parametrize("value", [json.dumps(["alice"]), json.dumps({**TERRAFORM_IDS, "alice": 7})])
def test_a_map_of_anything_but_strings_is_refused(value):
    with pytest.raises(KeycloakTestTargetMisconfigured, match="KEYCLOAK_TEST_USER_IDS"):
        keycloak_test_target(_terraform_environment(KEYCLOAK_TEST_USER_IDS=value))


def test_a_missing_user_is_refused():
    passwords = {name: value for name, value in TERRAFORM_PASSWORDS.items() if name != "erin"}

    with pytest.raises(KeycloakTestTargetMisconfigured, match="erin"):
        keycloak_test_target(_terraform_environment(KEYCLOAK_TEST_USER_PASSWORDS=json.dumps(passwords)))


def test_an_unknown_user_is_refused():
    ids = {**TERRAFORM_IDS, "mallory": "0000"}

    with pytest.raises(KeycloakTestTargetMisconfigured, match="mallory"):
        keycloak_test_target(_terraform_environment(KEYCLOAK_TEST_USER_IDS=json.dumps(ids)))


def test_the_refusal_never_carries_a_password():
    passwords = {**TERRAFORM_PASSWORDS, "alice": 7}

    with pytest.raises(KeycloakTestTargetMisconfigured) as refused:
        keycloak_test_target(_terraform_environment(KEYCLOAK_TEST_USER_PASSWORDS=json.dumps(passwords)))

    assert not any(password in str(refused.value) for password in TERRAFORM_PASSWORDS.values())


def test_a_user_repr_never_carries_the_password():
    # pytest prints the repr of values in a failed assertion to the CI log.
    user = keycloak_test_target(_terraform_environment()).users["alice"]

    assert TERRAFORM_PASSWORDS["alice"] not in repr(user)
    assert user.id in repr(user)


def test_a_target_repr_never_carries_a_secret():
    target = keycloak_test_target(_terraform_environment())

    assert not any(password in repr(target) for password in TERRAFORM_PASSWORDS.values())
    assert "generated-admin-secret" not in repr(target)


REPO = Path(__file__).resolve().parents[3]
# The base URL is read by tests/helpers/keycloak.py, not by the target: the
# Makefile sets it, and check-against-compose.sh keeps keycloak-test's default.
BASE_URL_VARIABLE = "KEYCLOAK_TEST_BASE_URL"
# A shell assignment (``NAME=``) or a jq object key (``NAME:``).
_SET_VARIABLE = re.compile(r"\b(KEYCLOAK_TEST_[A-Z_]+)[=:]")


def _variables_set_in(path: Path) -> set[str]:
    return set(_SET_VARIABLE.findall(path.read_text()))


@pytest.mark.parametrize(
    ("path", "also"),
    [
        (REPO / "Makefile", {BASE_URL_VARIABLE}),
        (REPO / "infra" / "keycloak" / "scripts" / "check-against-compose.sh", set()),
    ],
    ids=["Makefile", "check-against-compose.sh"],
)
def test_the_runners_set_exactly_the_target_variables(path, also):
    """A variable a runner stops setting, or one it sets that nothing reads, fails here."""
    assert _variables_set_in(path) == set(TARGET_VARIABLES) | also


@pytest.mark.parametrize(
    "path",
    [REPO / "Makefile", REPO / "infra" / "keycloak" / "scripts" / "check-against-compose.sh"],
    ids=["Makefile", "check-against-compose.sh"],
)
def test_what_the_runners_set_names_the_terraform_realm(path):
    values = {
        "KEYCLOAK_TEST_REALM": "identity-stack-terraform",
        "KEYCLOAK_TEST_USER_IDS": json.dumps(TERRAFORM_IDS),
        "KEYCLOAK_TEST_USER_PASSWORDS": json.dumps(TERRAFORM_PASSWORDS),
        "KEYCLOAK_TEST_ADMIN_CLIENT_SECRET": "generated-admin-secret",
        BASE_URL_VARIABLE: "http://keycloak-test.localhost:18180",
    }
    environment = {name: values[name] for name in _variables_set_in(path)}

    target = keycloak_test_target(environment)

    assert target.realm == "identity-stack-terraform"
    assert target.admin_client_secret == "generated-admin-secret"
