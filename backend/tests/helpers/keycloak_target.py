"""Which Keycloak realm the Keycloak tests log in to.

By default the realm ``keycloak-test`` imports from
``keycloak/realm/identity-stack-realm.json``: realm ``identity-stack``, the user
ids fixed in the import and the fixture passwords in
``keycloak/fixtures/README.md``.

The realm ``infra/keycloak/`` builds has the same users and clients, but
Keycloak picks the user ids and the admin client secret, and Terraform
generates the passwords. Four variables point the same tests at it, all or
none; ``infra/keycloak/scripts/check-against-compose.sh --integration-tests``
sets them from the run's Terraform outputs:

- ``KEYCLOAK_TEST_REALM``: the realm name.
- ``KEYCLOAK_TEST_USER_IDS``: JSON map of username to id (``terraform output -json user_ids``).
- ``KEYCLOAK_TEST_USER_PASSWORDS``: JSON map of username to password (output
  ``test_credentials``, under ``users``).
- ``KEYCLOAK_TEST_ADMIN_CLIENT_SECRET``: ``KEYCLOAK_ADMIN_CLIENT_SECRET`` of output
  ``backend_environment``.

A partial or malformed set raises ``KeycloakTestTargetMisconfigured`` when the
tests are collected, so a run never mixes the two realms.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class KeycloakUser:
    username: str
    # Out of the repr: pytest prints reprs in failed assertions, and Terraform's are real passwords.
    password: str = field(repr=False)
    # The ``sub`` of every token issued to the user in the target realm.
    id: str


@dataclass(frozen=True)
class KeycloakTestTarget:
    realm: str
    users: dict[str, KeycloakUser]
    admin_client_secret: str = field(repr=False)


class KeycloakTestTargetMisconfigured(Exception):
    """The ``KEYCLOAK_TEST_*`` variables name only part of a realm, or name it wrongly."""


IMPORT_REALM = "identity-stack"
# Local fixture values from the realm import.
IMPORT_ADMIN_CLIENT_SECRET = "local-only-admin-client"
REALM_IMPORT_PATH = Path(__file__).resolve().parents[3] / "keycloak" / "realm" / "identity-stack-realm.json"


def import_users(realm: Mapping) -> dict[str, KeycloakUser]:
    """The realm import's login users (not service accounts): fixed id and local fixture password."""
    users = {}
    for user in realm["users"]:
        credentials = user.get("credentials", [])
        passwords = [credential["value"] for credential in credentials if credential["type"] == "password"]
        if passwords:
            users[user["username"]] = KeycloakUser(user["username"], passwords[0], user["id"])
    return users


IMPORT_USERS = import_users(json.loads(REALM_IMPORT_PATH.read_text()))

REALM_VARIABLE = "KEYCLOAK_TEST_REALM"
USER_IDS_VARIABLE = "KEYCLOAK_TEST_USER_IDS"
USER_PASSWORDS_VARIABLE = "KEYCLOAK_TEST_USER_PASSWORDS"
ADMIN_CLIENT_SECRET_VARIABLE = "KEYCLOAK_TEST_ADMIN_CLIENT_SECRET"
TARGET_VARIABLES = (
    REALM_VARIABLE,
    USER_IDS_VARIABLE,
    USER_PASSWORDS_VARIABLE,
    ADMIN_CLIENT_SECRET_VARIABLE,
)


def keycloak_test_target(environ: Mapping[str, str]) -> KeycloakTestTarget:
    """The realm named by ``environ``: the import when no ``KEYCLOAK_TEST_*`` variable is set."""
    values = {name: environ.get(name, "") for name in TARGET_VARIABLES}
    unset = [name for name, value in values.items() if not value]
    if len(unset) == len(TARGET_VARIABLES):
        return KeycloakTestTarget(IMPORT_REALM, IMPORT_USERS, IMPORT_ADMIN_CLIENT_SECRET)
    if unset:
        raise KeycloakTestTargetMisconfigured(f"set all of {', '.join(TARGET_VARIABLES)} or none; unset: {unset}")

    ids = _user_map(USER_IDS_VARIABLE, values[USER_IDS_VARIABLE])
    passwords = _user_map(USER_PASSWORDS_VARIABLE, values[USER_PASSWORDS_VARIABLE])
    return KeycloakTestTarget(
        realm=values[REALM_VARIABLE],
        users={name: KeycloakUser(name, passwords[name], ids[name]) for name in IMPORT_USERS},
        admin_client_secret=values[ADMIN_CLIENT_SECRET_VARIABLE],
    )


def _user_map(variable: str, raw: str) -> dict[str, str]:
    """A JSON object of every fixture username to a string. Refusals name keys, never values."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise KeycloakTestTargetMisconfigured(f"{variable} is not JSON") from None
    if not isinstance(parsed, dict) or not all(isinstance(value, str) for value in parsed.values()):
        raise KeycloakTestTargetMisconfigured(f"{variable} must be a JSON object of username to string")
    missing, unknown = sorted(set(IMPORT_USERS) - set(parsed)), sorted(set(parsed) - set(IMPORT_USERS))
    if missing or unknown:
        raise KeycloakTestTargetMisconfigured(
            f"{variable} must name exactly {sorted(IMPORT_USERS)}; missing {missing}, unknown {unknown}"
        )
    return parsed
