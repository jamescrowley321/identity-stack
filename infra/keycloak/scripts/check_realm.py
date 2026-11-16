"""Compare the Terraform-built realm with the imported one, in the same Keycloak.

Run by ``check-against-compose.sh``, which applies this root to a fresh realm in
the compose ``keycloak-test`` container (where the import file's realm already
exists) and passes ``terraform output -json``. Needs ``httpx`` and the backend's
test helpers on ``PYTHONPATH`` (``backend/``).

It checks, against the admin API and against real tokens from both realms:

1. the realm settings, every client (built-in ones by presence only), client
   roles, scope mappings, the directory service account's client and realm
   roles, every user, their client and realm roles, organizations, domains,
   members and ``type:`` attributes match the import, in both directions (user
   ids differ: Keycloak picks them in the Terraform realm, so ``type:``
   attributes are compared by username);
2. a token from each realm has the same ``iss`` (up to the realm name),
   ``aud``, ``azp``, ``organization``, ``resource_access`` and claim names, and
   the Terraform realm's token has the claim names of
   ``keycloak/fixtures/access-token.json``;
3. no protocol mapper in the Terraform realm writes a user attribute or user
   property to ``aud``, ``azp``, ``organization``, ``resource_access``, ``dct``
   or ``tenants``;
4. a login through each app client with the scope output
   ``frontend_environment`` gives the SPA (``VITE_OIDC_SCOPE``) carries the
   user's organization;
5. the directory client reads a user's organizations through the Admin API
   with the secret Terraform output.

Exits non-zero with every difference listed.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from tests.helpers.keycloak import (
    ACME_SCOPE,
    ALICE,
    BOB,
    BOTH_ORGANIZATIONS_SCOPE,
    CAROL,
    DAVE,
    ERIN,
    FRANK,
    GLOBEX_SCOPE,
    MEMBER_APP_CLIENT,
    TENANT_ADMIN_APP_CLIENT,
    KeycloakTokenSource,
    KeycloakUser,
    decode_unverified,
)

REPO = Path(__file__).resolve().parents[3]
FIXTURE_TOKEN = REPO / "keycloak" / "fixtures" / "access-token.json"

GUARDED_CLAIMS = frozenset({"aud", "azp", "organization", "resource_access", "dct", "tenants"})
# Mapper types that copy something the user record holds into a claim.
USER_DATA_MAPPERS = frozenset({"oidc-usermodel-attribute-mapper", "oidc-usermodel-property-mapper"})

REALM_SETTINGS = (
    "enabled",
    "sslRequired",
    "registrationAllowed",
    "duplicateEmailsAllowed",
    "verifyEmail",
    "organizationsEnabled",
)
CLIENT_SETTINGS = (
    "name",
    "enabled",
    "publicClient",
    "standardFlowEnabled",
    "implicitFlowEnabled",
    "directAccessGrantsEnabled",
    "serviceAccountsEnabled",
    "fullScopeAllowed",
    "redirectUris",
    "webOrigins",
    "defaultClientScopes",
    "optionalClientScopes",
)
CLIENT_ATTRIBUTES = ("pkce.code.challenge.method", "post.logout.redirect.uris")
USER_SETTINGS = ("enabled", "email", "emailVerified", "firstName", "lastName")

# Logins compared between the two realms: (user, app client, scope).
LOGINS = (
    (BOB, MEMBER_APP_CLIENT, ACME_SCOPE),
    (BOB, TENANT_ADMIN_APP_CLIENT, GLOBEX_SCOPE),
    (BOB, MEMBER_APP_CLIENT, BOTH_ORGANIZATIONS_SCOPE),
    (ALICE, MEMBER_APP_CLIENT, ACME_SCOPE),
    (ALICE, MEMBER_APP_CLIENT, GLOBEX_SCOPE),
    (CAROL, MEMBER_APP_CLIENT, ACME_SCOPE),
    (DAVE, TENANT_ADMIN_APP_CLIENT, ACME_SCOPE),
    (ERIN, MEMBER_APP_CLIENT, ACME_SCOPE),
    (FRANK, TENANT_ADMIN_APP_CLIENT, ACME_SCOPE),
)
ABSENT = "<absent>"
# Users of one organization (acme) holding each app client's type, logged in
# with the scope output frontend_environment gives the SPA.
FRONTEND_LOGIN_USERS = {MEMBER_APP_CLIENT: ALICE, TENANT_ADMIN_APP_CLIENT: DAVE}


@dataclass(frozen=True)
class Realm:
    source: KeycloakTokenSource
    passwords: dict[str, str]

    def admin(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"{self.source.base_url}/admin/realms/{self.source.realm}",
            headers={"Authorization": f"Bearer {self.source.admin_access_token()}"},
            timeout=20,
        )


# Keycloak pages /users, /organizations and their members. Every listing asks for
# one page this size and refuses a full page: it may have been cut off.
LISTING_PAGE_SIZE = 500


class TruncatedListing(RuntimeError):
    """An Admin API listing came back with a full page, so items past it would be missed."""


def _get(http: httpx.Client, path: str, **params: object) -> object:
    response = http.get(path, params=params)
    response.raise_for_status()
    return response.json()


def _list(http: httpx.Client, path: str, **params: object) -> list:
    """One page of a paged Admin API listing, refused if it may be truncated."""
    items = _get(http, path, **params, first=0, max=LISTING_PAGE_SIZE)
    if len(items) >= LISTING_PAGE_SIZE:
        raise TruncatedListing(
            f"{str(http.base_url).rstrip('/')}{path} returned {len(items)} items, the page size: "
            "raise LISTING_PAGE_SIZE"
        )
    return items


def _sorted(value: object) -> object:
    return sorted(value) if isinstance(value, list) else value


def describe(realm: Realm) -> dict:
    """A realm's settings, clients, users and organizations, keyed by name rather than id."""
    with realm.admin() as http:
        settings = _get(http, "")
        clients = {}
        for client in _get(http, "/clients"):
            client_id = client["clientId"]
            if client_id in BUILT_IN_CLIENTS:
                # Keycloak creates these with every realm; only their presence is compared.
                clients[client_id] = "built in"
                continue
            roles = sorted(role["name"] for role in _get(http, f"/clients/{client['id']}/roles"))
            scope = _get(http, f"/clients/{client['id']}/scope-mappings")
            clients[client_id] = {
                **{key: _sorted(client.get(key, ABSENT)) for key in CLIENT_SETTINGS},
                **{f"attribute {key}": client.get("attributes", {}).get(key, ABSENT) for key in CLIENT_ATTRIBUTES},
                "roles": roles,
                "scope mappings": {
                    name: sorted(role["name"] for role in mapping["mappings"])
                    for name, mapping in (scope.get("clientMappings") or {}).items()
                },
                "realm scope mappings": sorted(role["name"] for role in scope.get("realmMappings", [])),
            }
            if client.get("serviceAccountsEnabled"):
                account = _get(http, f"/clients/{client['id']}/service-account-user")
                clients[client_id]["service account roles"] = _client_roles(http, account["id"])
                clients[client_id]["service account realm roles"] = _realm_roles(http, realm, account["id"])

        users = {}
        names_by_id = {}
        for user in _list(http, "/users", briefRepresentation="false"):
            names_by_id[user["id"]] = user["username"]
            users[user["username"]] = {
                **{key: user.get(key, ABSENT) for key in USER_SETTINGS},
                "client roles": _client_roles(http, user["id"]),
                "realm roles": _realm_roles(http, realm, user["id"]),
            }

        organizations = {}
        for organization in _list(http, "/organizations", briefRepresentation="false"):
            members = _list(http, f"/organizations/{organization['id']}/members")
            organizations[organization["alias"]] = {
                "name": organization["name"],
                "enabled": organization["enabled"],
                "domains": sorted((d["name"], d.get("verified", False)) for d in organization.get("domains", [])),
                "members": sorted((m["username"], m.get("membershipType")) for m in members),
                "attributes": {
                    key: sorted(names_by_id.get(value, value) for value in values)
                    for key, values in (organization.get("attributes") or {}).items()
                },
            }

    return {
        "realm": {key: settings.get(key, ABSENT) for key in REALM_SETTINGS},
        "clients": clients,
        "users": users,
        "organizations": organizations,
    }


def _realm_roles(http: httpx.Client, realm: Realm, user_id: str) -> list[str]:
    """A user's effective realm roles. The default role is named after the realm: default-roles-<realm>."""
    return sorted(
        role["name"].replace(realm.source.realm, "<realm>")
        for role in _get(http, f"/users/{user_id}/role-mappings/realm/composite")
    )


def _client_roles(http: httpx.Client, user_id: str) -> dict[str, list[str]]:
    mappings = _get(http, f"/users/{user_id}/role-mappings").get("clientMappings") or {}
    return {name: sorted(role["name"] for role in mapping["mappings"]) for name, mapping in mappings.items()}


def token_shape(source: KeycloakTokenSource, token: str) -> dict:
    _header, claims = decode_unverified(token)
    iss = claims.get("iss", ABSENT)
    return {
        "iss": "<issuer>" if iss == source.issuer else iss,
        "aud": claims.get("aud", ABSENT),
        "azp": claims.get("azp", ABSENT),
        "organization": _sorted(claims.get("organization", ABSENT)),
        "resource_access": {client: sorted(entry["roles"]) for client, entry in claims["resource_access"].items()}
        if "resource_access" in claims
        else ABSENT,
        "scope": sorted(claims.get("scope", "").split()),
        "claim names": sorted(claims),
    }


def login_shapes(realm: Realm) -> dict:
    shapes = {}
    for user, client_id, scope in LOGINS:
        login_as = KeycloakUser(user.username, realm.passwords[user.username], user.id)
        tokens = realm.source.login(login_as, client_id=client_id, scope=scope)
        key = f"{user.username} via {client_id} with {scope!r}"
        shapes[f"{key}: access token"] = token_shape(realm.source, tokens.access_token)
        shapes[f"{key}: ID token"] = token_shape(realm.source, tokens.id_token)
    return shapes


def guarded_mappers(realm: Realm) -> list[str]:
    """Every protocol mapper that writes user data into a guarded claim."""
    found = []
    with realm.admin() as http:
        holders = [(f"client {c['clientId']}", c) for c in _get(http, "/clients")]
        holders += [(f"client scope {s['name']}", s) for s in _get(http, "/client-scopes")]
        for label, holder in holders:
            for mapper in holder.get("protocolMappers", []):
                claim = (mapper.get("config") or {}).get("claim.name", "")
                if mapper["protocolMapper"] in USER_DATA_MAPPERS and claim.split(".")[0] in GUARDED_CLAIMS:
                    found.append(f"{label}: mapper {mapper['name']!r} writes {claim!r}")
    return found


def directory_reads_organizations(realm: Realm, secret: str, user_id: str) -> list[str]:
    response = httpx.post(
        f"{realm.source.issuer}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials"},
        auth=(DIRECTORY_CLIENT, secret),
        timeout=20,
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    response = httpx.get(
        f"{realm.source.base_url}/admin/realms/{realm.source.realm}/organizations/members/{user_id}/organizations",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    response.raise_for_status()
    return sorted(organization["alias"] for organization in response.json())


def frontend_logins(realm: Realm, frontend_environment: dict) -> list[str]:
    """Log in through each app client with the scope output ``frontend_environment`` gives the SPA.

    The user is a member of one organization, so the token must name it: an
    organization-bound type is refused without an ``organization`` claim.
    """
    found = []
    for client_id, environment in sorted(frontend_environment.items()):
        user = FRONTEND_LOGIN_USERS[client_id]
        login_as = KeycloakUser(user.username, realm.passwords[user.username], user.id)
        tokens = realm.source.login(login_as, client_id=client_id, scope=environment["VITE_OIDC_SCOPE"])
        _header, claims = decode_unverified(tokens.access_token)
        if claims.get("organization") != ["acme"]:
            found.append(
                f"{user.username} via {client_id} with output VITE_OIDC_SCOPE {environment['VITE_OIDC_SCOPE']!r}: "
                f"organization {claims.get('organization', ABSENT)!r}, expected ['acme']"
            )
    return found


def differences(path: str, left: object, right: object) -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        found = []
        for key in sorted(set(left) | set(right)):
            found += differences(f"{path}.{key}", left.get(key, ABSENT), right.get(key, ABSENT))
        return found
    return [] if left == right else [f"{path}: import {left!r}, terraform {right!r}"]


# Clients Keycloak creates in every realm.
BUILT_IN_CLIENTS = frozenset(
    {"account", "account-console", "admin-cli", "broker", "realm-management", "security-admin-console"}
)
DIRECTORY_CLIENT = "identity-stack-directory"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--import-realm", required=True)
    parser.add_argument("--terraform-realm", required=True)
    parser.add_argument("--terraform-outputs", required=True, type=Path, help="terraform output -json")
    args = parser.parse_args()

    outputs = {name: entry["value"] for name, entry in json.loads(args.terraform_outputs.read_text()).items()}
    imported = Realm(
        KeycloakTokenSource(base_url=args.base_url, realm=args.import_realm),
        {user.username: user.password for user in (ALICE, BOB, CAROL, DAVE, ERIN, FRANK)},
    )
    built = Realm(KeycloakTokenSource(base_url=args.base_url, realm=args.terraform_realm), outputs["user_passwords"])

    failures = differences("realm", describe(imported), describe(built))
    failures += differences("tokens", login_shapes(imported), login_shapes(built))

    fixture_claims = sorted(json.loads(FIXTURE_TOKEN.read_text())["claims"])
    bob = KeycloakUser(BOB.username, built.passwords[BOB.username], BOB.id)
    _header, claims = decode_unverified(built.source.login(bob, client_id=MEMBER_APP_CLIENT).access_token)
    if sorted(claims) != fixture_claims:
        failures.append(f"fixture claim names {fixture_claims!r}, terraform token {sorted(claims)!r}")
    if claims["iss"] != outputs["backend_environment"]["KEYCLOAK_ISSUER_URL"]:
        failures.append(f"token iss {claims['iss']!r} is not output KEYCLOAK_ISSUER_URL")

    failures += guarded_mappers(built)
    failures += frontend_logins(built, outputs["frontend_environment"])

    organizations = directory_reads_organizations(
        built, outputs["keycloak_directory_client_secret"], outputs["user_ids"][BOB.username]
    )
    if organizations != ["acme", "globex"]:
        failures.append(f"directory client read bob's organizations as {organizations!r}")

    for failure in failures:
        print(f"DIFFERENT {failure}")
    print(f"{len(failures)} difference(s) between the imported realm and the Terraform-built realm")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
