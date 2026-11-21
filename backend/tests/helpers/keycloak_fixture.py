"""Keycloak's additional test identities, kept out of the shared provider fixture."""

import json
from pathlib import Path

from tests.helpers.identity_fixture import FIXTURE_PATH, IdentityFixtureInvalid, load_identity_fixture

KEYCLOAK_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "infra" / "keycloak" / "test-identities.json"
_shared = json.loads(FIXTURE_PATH.read_text())
_keycloak = json.loads(KEYCLOAK_FIXTURE_PATH.read_text())
if set(_shared["users"]) & set(_keycloak["users"]):
    raise IdentityFixtureInvalid("Keycloak fixture users must not override shared users")
KEYCLOAK_IDENTITIES = load_identity_fixture({**_shared, "users": {**_shared["users"], **_keycloak["users"]}})
OPERATOR = KEYCLOAK_IDENTITIES.users[_keycloak["operator"]]
