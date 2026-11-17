"""Which Keycloak realm the Keycloak tests log in to.

By default the realm ``keycloak-test`` imports from
``keycloak/realm/identity-stack-realm.json``: realm ``identity-stack``, the user
ids fixed in the import and the fixture passwords in
``keycloak/fixtures/README.md``.

The realm ``infra/keycloak/`` builds has the same users and clients, but
Keycloak picks the user ids and the directory client secret, and Terraform
generates the passwords. Five variables point the same tests at it, all or
none; ``infra/keycloak/scripts/check-against-compose.sh --integration-tests``
sets them from the run's Terraform outputs:

- ``KEYCLOAK_TEST_REALM``: the realm name.
- ``KEYCLOAK_TEST_USER_IDS``: JSON map of username to id (``terraform output -json user_ids``).
- ``KEYCLOAK_TEST_USER_PASSWORDS``: JSON map of username to password (output ``user_passwords``).
- ``KEYCLOAK_TEST_DIRECTORY_CLIENT_SECRET``: output ``keycloak_directory_client_secret``.
- ``KEYCLOAK_TEST_ADMIN_CLIENT_SECRET``: output ``keycloak_admin_client_secret``.

A partial or malformed set raises ``KeycloakTestTargetMisconfigured`` when the
tests are collected, so a run never mixes the two realms.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field


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
    directory_client_secret: str = field(repr=False)
    admin_client_secret: str = field(repr=False)


class KeycloakTestTargetMisconfigured(Exception):
    """The ``KEYCLOAK_TEST_*`` variables name only part of a realm, or name it wrongly."""


IMPORT_REALM = "identity-stack"
# Local fixture values from the realm import.
IMPORT_DIRECTORY_CLIENT_SECRET = "local-only-directory-client"
IMPORT_ADMIN_CLIENT_SECRET = "local-only-admin-client"
IMPORT_USERS = {
    user.username: user
    for user in (
        KeycloakUser("alice", "local-only-alice", "a11ce000-0000-4000-8000-000000000001"),
        KeycloakUser("bob", "local-only-bob", "b0b00000-0000-4000-8000-000000000002"),
        KeycloakUser("carol", "local-only-carol", "ca401000-0000-4000-8000-000000000003"),
        KeycloakUser("dave", "local-only-dave", "da4e0000-0000-4000-8000-000000000004"),
        KeycloakUser("erin", "local-only-erin", "e4140000-0000-4000-8000-000000000005"),
        KeycloakUser("frank", "local-only-frank", "f4a4c000-0000-4000-8000-000000000006"),
    )
}

REALM_VARIABLE = "KEYCLOAK_TEST_REALM"
USER_IDS_VARIABLE = "KEYCLOAK_TEST_USER_IDS"
USER_PASSWORDS_VARIABLE = "KEYCLOAK_TEST_USER_PASSWORDS"
DIRECTORY_CLIENT_SECRET_VARIABLE = "KEYCLOAK_TEST_DIRECTORY_CLIENT_SECRET"
ADMIN_CLIENT_SECRET_VARIABLE = "KEYCLOAK_TEST_ADMIN_CLIENT_SECRET"
TARGET_VARIABLES = (
    REALM_VARIABLE,
    USER_IDS_VARIABLE,
    USER_PASSWORDS_VARIABLE,
    DIRECTORY_CLIENT_SECRET_VARIABLE,
    ADMIN_CLIENT_SECRET_VARIABLE,
)


def keycloak_test_target(environ: Mapping[str, str]) -> KeycloakTestTarget:
    """The realm named by ``environ``: the import when no ``KEYCLOAK_TEST_*`` variable is set."""
    values = {name: environ.get(name, "") for name in TARGET_VARIABLES}
    unset = [name for name, value in values.items() if not value]
    if len(unset) == len(TARGET_VARIABLES):
        return KeycloakTestTarget(
            IMPORT_REALM, IMPORT_USERS, IMPORT_DIRECTORY_CLIENT_SECRET, IMPORT_ADMIN_CLIENT_SECRET
        )
    if unset:
        raise KeycloakTestTargetMisconfigured(f"set all of {', '.join(TARGET_VARIABLES)} or none; unset: {unset}")

    ids = _user_map(USER_IDS_VARIABLE, values[USER_IDS_VARIABLE])
    passwords = _user_map(USER_PASSWORDS_VARIABLE, values[USER_PASSWORDS_VARIABLE])
    return KeycloakTestTarget(
        realm=values[REALM_VARIABLE],
        users={name: KeycloakUser(name, passwords[name], ids[name]) for name in IMPORT_USERS},
        directory_client_secret=values[DIRECTORY_CLIENT_SECRET_VARIABLE],
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
