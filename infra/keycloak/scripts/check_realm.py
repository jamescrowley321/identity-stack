"""Compare the Terraform-built realm with the imported one, in the same Keycloak.

Run by ``check-against-compose.sh``, which applies this root to a fresh realm in
the compose ``keycloak-test`` container (where the import file's realm already
exists) and passes ``terraform output -json``. Needs ``httpx`` and the backend's
test helpers on ``PYTHONPATH`` (``backend/``).

It checks, against the admin API and against real tokens from both realms:

1. the realm settings and event settings, every client (built-in ones by
   presence only) with its protocol mappers, the API client's roles and their
   composites and that no user or group holds one, the admin service account's
   client and realm roles and its fine-grained admin permissions, every user
   and their client, realm and group memberships, organizations, domains,
   members, attributes and organization groups (their members and role
   mappings) and the user profile (its unmanaged attribute policy included) match
   the import, in both directions (user ids differ: Keycloak
   picks them in the Terraform realm, so everything is compared by username);
2. a token from each realm has the same ``iss`` (up to the realm name),
   ``typ``, ``aud``, ``azp``, ``organization``, ``resource_access``,
   ``realm_access``, ``email_verified`` and claim names, and the Terraform
   realm's tokens have the claim names of the recorded fixtures in
   ``keycloak/fixtures/``;
3. in both realms, the SPA's access token is ``typ`` ``Bearer`` with the API
   audience in ``aud``, ``azp`` the SPA, ``email_verified`` and no
   ``resource_access`` or ``realm_access``, also for a user with role
   assignments; its ID token is ``typ`` ``ID``; the other client's access token
   lacks the API audience; and no organization group or realm group maps a role;
4. no protocol mapper in the Terraform realm writes a user attribute or user
   property to ``aud``, ``azp``, ``organization``, ``resource_access``, ``dct``
   or ``tenants``;
5. the outputs name the realm's issuer, the API audience, the SPA and other
   client ids and the organization aliases, ``frontend_environment`` builds the
   SPA for the SPA client, and a login with its scope (``VITE_OIDC_SCOPE``)
   carries the user's organization;
6. the admin client, with the secret in output ``backend_environment``, reads
   its own realm-management roles, organizations, admin events, user events
   and the API client's roles, and is refused the SPA client.

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
    CAROL,
    DAVE,
    ERIN,
    FRANK,
    GLOBEX_SCOPE,
    KeycloakTokenSource,
    KeycloakUser,
    decode_unverified,
)

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "keycloak" / "fixtures"
FIXTURE_ACCESS_TOKEN = FIXTURES / "access-token.json"
FIXTURE_ID_TOKEN = FIXTURES / "id-token.json"
FIXTURE_OTHER_CLIENT_TOKEN = FIXTURES / "other-client-access-token.json"
FIXTURE_TENANTS = REPO / "infra" / "fixtures" / "test-identities.json"

SPA_CLIENT = "identity-stack-spa"
API_CLIENT = "identity-stack-api"
OTHER_CLIENT = "identity-stack-other"
ADMIN_CLIENT = "identity-stack-admin"
ADMIN_PERMISSIONS_CLIENT = "admin-permissions"
REALM_MANAGEMENT_CLIENT = "realm-management"

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
    "adminPermissionsEnabled",
    "bruteForceProtected",
    "permanentLockout",
    "failureFactor",
    "waitIncrementSeconds",
    "quickLoginCheckMilliSeconds",
    "minimumQuickLoginWaitSeconds",
    "maxFailureWaitSeconds",
    "maxDeltaTimeSeconds",
    "maxTemporaryLockouts",
    "bruteForceStrategy",
)
REALM_ATTRIBUTES = ("adminEventsExpiration",)
EVENT_SETTINGS = (
    "eventsEnabled",
    "eventsExpiration",
    "eventsListeners",
    "enabledEventTypes",
    "adminEventsEnabled",
    "adminEventsDetailsEnabled",
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
MAPPER_CONFIG = (
    "included.client.audience",
    "included.custom.audience",
    "access.token.claim",
    "id.token.claim",
    "introspection.token.claim",
)
USER_SETTINGS = ("enabled", "email", "emailVerified", "firstName", "lastName")

# Logins compared between the two realms: (user, client, scope).
LOGINS = (
    (BOB, SPA_CLIENT, ACME_SCOPE),
    (BOB, SPA_CLIENT, GLOBEX_SCOPE),
    (ALICE, SPA_CLIENT, ACME_SCOPE),
    (CAROL, SPA_CLIENT, ACME_SCOPE),
    (DAVE, SPA_CLIENT, ACME_SCOPE),
    (ERIN, SPA_CLIENT, ACME_SCOPE),
    (ALICE, OTHER_CLIENT, ACME_SCOPE),
)
ABSENT = "<absent>"
# A member of acme alone, logged in with the scope output frontend_environment gives the SPA.
FRONTEND_LOGIN_USER = ALICE


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
    """A realm's settings, clients, users, groups and organizations, keyed by name rather than id."""
    with realm.admin() as http:
        settings = _get(http, "")
        events = _get(http, "/events/config")
        names_by_id = {user["id"]: user["username"] for user in _list(http, "/users", briefRepresentation="false")}

        clients = {}
        client_ids = {}
        for client in _get(http, "/clients"):
            client_id = client["clientId"]
            client_ids[client["id"]] = client_id
            if client_id == ADMIN_PERMISSIONS_CLIENT:
                clients[client_id] = _admin_permissions(http, client["id"])
                continue
            if client_id in BUILT_IN_CLIENTS:
                # Keycloak creates these with every realm; only their presence is compared.
                clients[client_id] = "built in"
                continue
            scope = _get(http, f"/clients/{client['id']}/scope-mappings")
            clients[client_id] = {
                **{key: _sorted(client.get(key, ABSENT)) for key in CLIENT_SETTINGS},
                **{f"attribute {key}": client.get("attributes", {}).get(key, ABSENT) for key in CLIENT_ATTRIBUTES},
                "protocol mappers": {
                    mapper["name"]: {
                        "type": mapper["protocolMapper"],
                        **{key: mapper.get("config", {}).get(key, ABSENT) for key in MAPPER_CONFIG},
                    }
                    for mapper in client.get("protocolMappers", [])
                },
                "roles": _client_roles_of(http, client["id"], names_by_id),
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
        for user_id, username in names_by_id.items():
            user = _get(http, f"/users/{user_id}")
            users[username] = {
                **{key: user.get(key, ABSENT) for key in USER_SETTINGS},
                "client roles": _client_roles(http, user_id),
                "realm roles": _realm_roles(http, realm, user_id),
                "realm groups": sorted(group["path"] for group in _get(http, f"/users/{user_id}/groups")),
            }

        organizations = {}
        for organization in _list(http, "/organizations", briefRepresentation="false"):
            base = f"/organizations/{organization['id']}"
            members = _list(http, f"{base}/members")
            organizations[organization["alias"]] = {
                "name": organization["name"],
                "enabled": organization["enabled"],
                "domains": sorted((d["name"], d.get("verified", False)) for d in organization.get("domains", [])),
                "members": sorted((m["username"], m.get("membershipType")) for m in members),
                "attributes": organization.get("attributes") or {},
                "groups": {
                    group["name"]: {
                        "members": sorted(m["username"] for m in _list(http, f"{base}/groups/{group['id']}/members")),
                        "role mappings": _get(http, f"{base}/groups/{group['id']}/role-mappings"),
                        "subgroups": group.get("subGroupCount", len(group.get("subGroups", []))),
                    }
                    for group in _list(http, f"{base}/groups", briefRepresentation="false")
                },
            }

        realm_groups = {group["path"]: group.get("subGroupCount", 0) for group in _get(http, "/groups")}
        user_profile = _get(http, "/users/profile")

    return {
        "realm": {key: settings.get(key, ABSENT) for key in REALM_SETTINGS},
        "realm attributes": {key: (settings.get("attributes") or {}).get(key, ABSENT) for key in REALM_ATTRIBUTES},
        "events": {key: _sorted(events.get(key, ABSENT)) for key in EVENT_SETTINGS},
        "clients": clients,
        "users": users,
        "realm groups": realm_groups,
        "organizations": organizations,
        "user profile": user_profile,
    }


def _client_roles_of(http: httpx.Client, client_uuid: str, names_by_id: dict[str, str]) -> dict:
    """A client's roles: each one's composites, and the users and groups that hold it directly."""
    roles = {}
    for role in _get(http, f"/clients/{client_uuid}/roles", briefRepresentation="false"):
        composites = _get(http, f"/roles-by-id/{role['id']}/composites") if role.get("composite") else []
        roles[role["name"]] = {
            "composites": sorted(composite["name"] for composite in composites),
            "users": sorted(
                names_by_id.get(user["id"], user["username"])
                for user in _get(http, f"/clients/{client_uuid}/roles/{role['name']}/users")
            ),
            "groups": sorted(
                group["path"] for group in _get(http, f"/clients/{client_uuid}/roles/{role['name']}/groups")
            ),
        }
    return roles


def _admin_permissions(http: httpx.Client, client_uuid: str) -> dict:
    """The fine-grained admin permissions: each one's resource type, clients, scopes and the users its policies name."""
    server = f"/clients/{client_uuid}/authz/resource-server"
    clients_by_uuid = {client["id"]: client["clientId"] for client in _get(http, "/clients")}
    permissions = {}
    for permission in _get(http, f"{server}/permission"):
        policies = _get(http, f"{server}/policy/{permission['id']}/associatedPolicies")
        permissions[permission["name"]] = {
            "resource type": permission.get("resourceType", ABSENT),
            "clients": sorted(
                clients_by_uuid.get(resource["name"], resource["name"])
                for resource in _get(http, f"{server}/permission/{permission['id']}/resources")
            ),
            "scopes": sorted(scope["name"] for scope in _get(http, f"{server}/permission/{permission['id']}/scopes")),
            "policies": {
                # By username: a service account's user is not in the /users listing.
                policy["name"]: sorted(
                    _get(http, f"/users/{user}")["username"]
                    for user in _get(http, f"{server}/policy/user/{policy['id']}")["users"]
                )
                for policy in policies
                if policy["type"] == "user"
            },
            "other policies": sorted(policy["name"] for policy in policies if policy["type"] != "user"),
        }
    return {"permissions": permissions}


def _realm_roles(http: httpx.Client, realm: Realm, user_id: str) -> list[str]:
    """A user's effective realm roles. The default role is named after the realm: default-roles-<realm>."""
    return sorted(
        role["name"].replace(realm.source.realm, "<realm>")
        for role in _get(http, f"/users/{user_id}/role-mappings/realm/composite")
    )


def _client_roles(http: httpx.Client, user_id: str) -> dict[str, list[str]]:
    mappings = _get(http, f"/users/{user_id}/role-mappings").get("clientMappings") or {}
    return {name: sorted(role["name"] for role in mapping["mappings"]) for name, mapping in mappings.items()}


def _audiences(aud: object) -> list[str]:
    """A token's ``aud`` as a list: Keycloak writes one audience as a string; absent is none."""
    if aud == ABSENT:
        return []
    return [aud] if isinstance(aud, str) else list(aud)


def token_shape(source: KeycloakTokenSource, token: str) -> dict:
    _header, claims = decode_unverified(token)
    iss = claims.get("iss", ABSENT)
    return {
        "iss": "<issuer>" if iss == source.issuer else iss,
        "typ": claims.get("typ", ABSENT),
        "aud": claims.get("aud", ABSENT),
        "azp": claims.get("azp", ABSENT),
        "organization": _sorted(claims.get("organization", ABSENT)),
        "resource_access": claims.get("resource_access", ABSENT),
        "realm_access": claims.get("realm_access", ABSENT),
        "email_verified": claims.get("email_verified", ABSENT),
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


def token_binding(label: str, shapes: dict) -> list[str]:
    """What the backend's binding needs from each realm's tokens (S2): typ, aud, azp, no role claims."""
    found = []
    for key, shape in shapes.items():
        access = key.endswith(": access token")
        spa = f" via {SPA_CLIENT} " in key
        audiences = _audiences(shape["aud"])
        expected_typ = "Bearer" if access else "ID"
        if shape["typ"] != expected_typ:
            found.append(f"{label} {key}: typ {shape['typ']!r}, expected {expected_typ!r}")
        for claim in ("resource_access", "realm_access"):
            if shape[claim] != ABSENT:
                found.append(f"{label} {key}: carries {claim} {shape[claim]!r}")
        if shape["email_verified"] is not True:
            found.append(f"{label} {key}: email_verified {shape['email_verified']!r}")
        if access and spa and (API_CLIENT not in audiences or shape["azp"] != SPA_CLIENT):
            found.append(
                f"{label} {key}: aud {shape['aud']!r}, azp {shape['azp']!r}; expected {API_CLIENT} and {SPA_CLIENT}"
            )
        if access and not spa and API_CLIENT in audiences:
            found.append(f"{label} {key}: another client's token carries the API audience")
    return found


def nothing_grants_api_roles(label: str, description: dict) -> list[str]:
    """No user or group holds an API client role, and no group maps any role."""
    found = []
    for role, holders in description["clients"][API_CLIENT]["roles"].items():
        if holders["users"] or holders["groups"]:
            found.append(f"{label} API client role {role!r} is held by {holders['users']} {holders['groups']}")
    for alias, organization in description["organizations"].items():
        for name, group in organization["groups"].items():
            if group["role mappings"]:
                found.append(f"{label} organization {alias} group {name} maps roles {group['role mappings']!r}")
    if description["realm groups"]:
        found.append(
            f"{label} has realm groups {sorted(description['realm groups'])}: role assignments are organization groups"
        )
    return found


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


def fixture_claim_names(realm: Realm) -> list[str]:
    """The Terraform realm's tokens have the claim names of the recorded fixtures."""
    found = []
    bob = KeycloakUser(BOB.username, realm.passwords[BOB.username], BOB.id)
    spa = realm.source.login(bob, client_id=SPA_CLIENT, scope=ACME_SCOPE)
    other = realm.source.login(bob, client_id=OTHER_CLIENT, scope=ACME_SCOPE)
    for path, token in (
        (FIXTURE_ACCESS_TOKEN, spa.access_token),
        (FIXTURE_ID_TOKEN, spa.id_token),
        (FIXTURE_OTHER_CLIENT_TOKEN, other.access_token),
    ):
        recorded = sorted(json.loads(path.read_text())["claims"])
        _header, claims = decode_unverified(token)
        if sorted(claims) != recorded:
            found.append(f"fixture {path.name} claim names {recorded!r}, terraform token {sorted(claims)!r}")
    return found


def outputs_name_the_realm(realm: Realm, outputs: dict) -> list[str]:
    backend = outputs["backend_environment"]
    frontend = outputs["frontend_environment"]
    tenants = sorted(json.loads(FIXTURE_TENANTS.read_text())["tenants"])
    expected = {
        "issuer_url": realm.source.issuer,
        "backend KEYCLOAK_ISSUER_URL": realm.source.issuer,
        "frontend VITE_OIDC_AUTHORITY": realm.source.issuer,
        "api_audience": API_CLIENT,
        "backend KEYCLOAK_AUDIENCE": API_CLIENT,
        "spa_client_id": SPA_CLIENT,
        "backend KEYCLOAK_CLIENT_ID": SPA_CLIENT,
        "frontend VITE_OIDC_CLIENT_ID": SPA_CLIENT,
        "other_client_id": OTHER_CLIENT,
        "backend KEYCLOAK_ADMIN_CLIENT_ID": ADMIN_CLIENT,
        "organization_aliases": {tenant: tenant for tenant in tenants},
        "test_credentials clients": {},
    }
    actual = {
        "issuer_url": outputs["issuer_url"],
        "backend KEYCLOAK_ISSUER_URL": backend["KEYCLOAK_ISSUER_URL"],
        "frontend VITE_OIDC_AUTHORITY": frontend["VITE_OIDC_AUTHORITY"],
        "api_audience": outputs["api_audience"],
        "backend KEYCLOAK_AUDIENCE": backend["KEYCLOAK_AUDIENCE"],
        "spa_client_id": outputs["spa_client_id"],
        "backend KEYCLOAK_CLIENT_ID": backend["KEYCLOAK_CLIENT_ID"],
        "frontend VITE_OIDC_CLIENT_ID": frontend["VITE_OIDC_CLIENT_ID"],
        "other_client_id": outputs["other_client_id"],
        "backend KEYCLOAK_ADMIN_CLIENT_ID": backend["KEYCLOAK_ADMIN_CLIENT_ID"],
        "organization_aliases": outputs["organization_aliases"],
        "test_credentials clients": outputs["test_credentials"]["clients"],
    }
    return [
        f"output {key} is {actual[key]!r}, expected {value!r}"
        for key, value in expected.items()
        if actual[key] != value
    ]


def frontend_login(realm: Realm, frontend_environment: dict) -> list[str]:
    """A member of one organization, logged in with the SPA's scope, gets a token naming it."""
    scope = frontend_environment["VITE_OIDC_SCOPE"]
    user = FRONTEND_LOGIN_USER
    login_as = KeycloakUser(user.username, realm.passwords[user.username], user.id)
    tokens = realm.source.login(login_as, client_id=frontend_environment["VITE_OIDC_CLIENT_ID"], scope=scope)
    _header, claims = decode_unverified(tokens.access_token)
    if claims.get("organization") != ["acme"]:
        organization = claims.get("organization", ABSENT)
        return [f"{user.username} with output VITE_OIDC_SCOPE {scope!r}: organization {organization!r}"]
    return []


# The admin service account's realm-management roles, which it reads back at startup.
ADMIN_CLIENT_ROLES = sorted(["manage-organizations", "manage-users", "query-clients", "view-events"])
# What the admin service account must be able to read (200), and the SPA client it must not (403).
ADMIN_CLIENT_READS = (
    ("/organizations", httpx.codes.OK),
    ("/admin-events", httpx.codes.OK),
    ("/events", httpx.codes.OK),
    ("/clients/{api}/roles", httpx.codes.OK),
    ("/clients/{spa}", httpx.codes.FORBIDDEN),
)


def admin_client_reads(realm: Realm, secret: str) -> list[str]:
    """The admin client's reads with its own credential, as the backend makes them."""
    response = httpx.post(
        f"{realm.source.issuer}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials"},
        auth=(ADMIN_CLIENT, secret),
        timeout=20,
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    with realm.admin() as http:
        ids = {client["clientId"]: client["id"] for client in _get(http, "/clients")}
        (account,) = _get(http, "/users", username=f"service-account-{ADMIN_CLIENT}", exact="true")
    found = []
    base = f"{realm.source.base_url}/admin/realms/{realm.source.realm}"
    own_roles = httpx.get(
        f"{base}/users/{account['id']}/role-mappings/clients/{ids[REALM_MANAGEMENT_CLIENT]}/composite",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    held = sorted(role["name"] for role in own_roles.json()) if own_roles.status_code == httpx.codes.OK else []
    if held != ADMIN_CLIENT_ROLES:
        found.append(f"admin client reads its own realm-management roles as {held!r} ({own_roles.status_code})")
    for path, expected in ADMIN_CLIENT_READS:
        url = base + path.format(api=ids[API_CLIENT], spa=ids[SPA_CLIENT])
        status = httpx.get(url, params={"first": 0, "max": 1}, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        if status.status_code != expected:
            found.append(f"admin client GET {path}: {status.status_code}, expected {expected}")
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
    {"account", "account-console", "admin-cli", "broker", REALM_MANAGEMENT_CLIENT, "security-admin-console"}
)


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
    built = Realm(
        KeycloakTokenSource(base_url=args.base_url, realm=args.terraform_realm), outputs["test_credentials"]["users"]
    )

    failures: list[str] = []
    try:
        compare(imported, built, outputs, failures)
    finally:
        # Printed even when a later step raises, so the differences already found are not lost.
        for failure in failures:
            print(f"DIFFERENT {failure}")
    print(f"{len(failures)} difference(s) between the imported realm and the Terraform-built realm")
    return 1 if failures else 0


def compare(imported: Realm, built: Realm, outputs: dict, failures: list[str]) -> None:
    """Append every difference to ``failures`` as it is found."""
    imported_description, built_description = describe(imported), describe(built)
    failures += differences("realm", imported_description, built_description)
    failures += nothing_grants_api_roles("import", imported_description)
    failures += nothing_grants_api_roles("terraform", built_description)

    imported_shapes, built_shapes = login_shapes(imported), login_shapes(built)
    failures += differences("tokens", imported_shapes, built_shapes)
    failures += token_binding("import", imported_shapes)
    failures += token_binding("terraform", built_shapes)
    failures += fixture_claim_names(built)

    failures += guarded_mappers(built)
    failures += outputs_name_the_realm(built, outputs)
    failures += frontend_login(built, outputs["frontend_environment"])
    failures += admin_client_reads(built, outputs["backend_environment"]["KEYCLOAK_ADMIN_CLIENT_SECRET"])


if __name__ == "__main__":
    sys.exit(main())
