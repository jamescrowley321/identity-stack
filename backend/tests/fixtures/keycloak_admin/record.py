"""Record keycloak-test's Admin API answers for the Keycloak admin adapter's unit tests.

Run from ``backend/`` with ``keycloak-test`` up and freshly created (README.md beside this file).
Writes the recording to stdout.
"""

import json
import sys

import httpx

BASE = "http://keycloak-test.localhost:18180"
REALM = f"{BASE}/realms/identity-stack"
ADMIN = f"{BASE}/admin/realms/identity-stack"
TOKEN_URL = f"{REALM}/protocol/openid-connect/token"
GRANT = {"grant_type": "client_credentials"}
# Local fixture values from the realm import.
ADMIN_CLIENT = ("identity-stack-admin", "local-only-admin-client")
API_CLIENT = "identity-stack-api"
TENANT = "7e5a0000-0000-4000-8000-000000000586"
USER = "05e40000-0000-4000-8000-000000000586"
ROLE = "40e00000-0000-4000-8000-000000000586"
PERMISSION = "9e400000-0000-4000-8000-000000000586"
ROLE_ATTRIBUTE = "identity-stack.role-id"
PERMISSION_ATTRIBUTE = "identity-stack.permission-id"


def main() -> None:
    out: dict[str, dict] = {}

    def keep(name: str, response: httpx.Response, *, drop_token: str | None = None) -> httpx.Response:
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = response.text
        if drop_token and isinstance(body, dict):
            body = {**body, "access_token": drop_token}
            body.pop("refresh_token", None)
        if isinstance(body, list):
            # A client's secret is not recorded, even a local fixture's.
            body = [
                {k: v for k, v in entry.items() if k != "secret"} if isinstance(entry, dict) else entry
                for entry in body
            ]
        entry = {
            "method": response.request.method,
            "url": str(response.request.url).replace(BASE, "{base}"),
            "status": response.status_code,
            "body": body,
        }
        if "location" in response.headers:
            entry["location"] = response.headers["location"].replace(BASE, "{base}")
        out[name] = entry
        return response

    http = httpx.Client(timeout=20)
    token = keep("token", http.post(TOKEN_URL, data=GRANT, auth=ADMIN_CLIENT), drop_token="recorded-access-token")
    keep("token_wrong_secret", http.post(TOKEN_URL, data=GRANT, auth=(ADMIN_CLIENT[0], "wrong")))
    keep("token_unknown_client", http.post(TOKEN_URL, data=GRANT, auth=("no-such-client", "wrong")))
    bearer = {"Authorization": f"Bearer {token.json()['access_token']}"}
    first = {"first": 0, "max": 1}
    bad = {"Authorization": "Bearer not-a-token"}
    keep("admin_api_bad_token", http.get(f"{ADMIN}/organizations", params=first, headers=bad))
    keep("credential_check", http.get(f"{ADMIN}/organizations", params=first, headers=bearer))

    # The startup check: the service account's own realm-management roles, then the API client.
    by_name = {"username": f"service-account-{ADMIN_CLIENT[0]}", "exact": "true"}
    account = keep("service_account_find", http.get(f"{ADMIN}/users", params=by_name, headers=bearer))
    mappings_path = f"{ADMIN}/users/{account.json()[0]['id']}/role-mappings"
    mappings = keep("service_account_role_mappings", http.get(mappings_path, headers=bearer))
    realm_management = mappings.json()["clientMappings"]["realm-management"]
    roles_path = f"{mappings_path}/clients/{realm_management['id']}/composite"
    keep("service_account_roles", http.get(roles_path, headers=bearer))
    api_client = keep("api_client_find", http.get(f"{ADMIN}/clients", params={"clientId": API_CLIENT}, headers=bearer))
    keep("api_client_find_none", http.get(f"{ADMIN}/clients", params={"clientId": "no-such-client"}, headers=bearer))
    # A client the service account may not read: Keycloak answers as if it were not there.
    keep("other_client_find", http.get(f"{ADMIN}/clients", params={"clientId": "identity-stack-spa"}, headers=bearer))

    def admin(method: str, path: str, **kwargs) -> httpx.Response:
        return http.request(method, f"{ADMIN}{path}", headers=bearer, **kwargs)

    by_alias = {"q": f"alias:{TENANT}", "exact": "true"}
    keep("organization_find_none", admin("GET", "/organizations", params=by_alias))
    keep("organization_create", admin("POST", "/organizations", json={"name": "Recorded Co", "alias": TENANT}))
    organization = keep("organization_find", admin("GET", "/organizations", params=by_alias)).json()[0]
    taken_name = {"name": "Recorded Co", "alias": "other-alias"}
    keep("organization_create_name_taken", admin("POST", "/organizations", json=taken_name))
    path = f"/organizations/{organization['id']}"
    renamed = {"name": "Recorded Co Renamed", "alias": TENANT}
    keep("organization_update", admin("PUT", path, json={**renamed, "domains": [{"name": "recorded.test"}]}))
    taken_domain = {**renamed, "domains": [{"name": "acme.test"}]}
    keep("organization_update_domain_taken", admin("PUT", path, json=taken_domain))

    by_username = {"username": USER, "exact": "true"}
    keep("user_find_none", admin("GET", "/users", params=by_username))
    new_user = {"username": USER, "email": "recorded@recorded.test", "enabled": True}
    keep("user_create", admin("POST", "/users", json=new_user))
    user = keep("user_find", admin("GET", "/users", params=by_username)).json()[0]
    keep("user_get", admin("GET", f"/users/{user['id']}"))
    taken_email = {"username": "another", "email": "alice@acme.test", "enabled": True}
    keep("user_create_email_taken", admin("POST", "/users", json=taken_email))
    keep("user_update", admin("PUT", f"/users/{user['id']}", json={"email": "renamed@recorded.test", "enabled": False}))

    keep("organization_get", admin("GET", path))
    keep(
        "organizations_list",
        admin("GET", "/organizations", params={"briefRepresentation": "true", "first": "0", "max": "100"}),
    )

    # The API client's roles: a permission, and a role whose composite it is.
    client_path = f"/clients/{api_client.json()[0]['id']}"
    brief_false = {"briefRepresentation": "false"}
    keep("client_roles", admin("GET", f"{client_path}/roles", params=brief_false))
    permission = {
        "name": "recorded:read",
        "description": "Recorded",
        "attributes": {PERMISSION_ATTRIBUTE: [PERMISSION]},
    }
    keep("client_role_create_permission", admin("POST", f"{client_path}/roles", json=permission))
    role = {"name": "recorded-role", "description": "Recorded role", "attributes": {ROLE_ATTRIBUTE: [ROLE]}}
    keep("client_role_create_role", admin("POST", f"{client_path}/roles", json=role))
    keep("client_role_create_taken", admin("POST", f"{client_path}/roles", json=role))
    roles = keep("client_roles_created", admin("GET", f"{client_path}/roles", params=brief_false)).json()
    role_id = next(entry["id"] for entry in roles if entry["name"] == "recorded-role")
    permission_id = next(entry["id"] for entry in roles if entry["name"] == "recorded:read")
    by_id = f"/roles-by-id/{role_id}"
    keep("role_composites_none", admin("GET", f"{by_id}/composites"))
    keep("composites_add", admin("POST", f"{by_id}/composites", json=[{"id": permission_id}]))
    keep("role_composites", admin("GET", f"{by_id}/composites"))
    keep("composites_remove", admin("DELETE", f"{by_id}/composites", json=[{"id": permission_id}]))
    renamed_role = {**role, "name": "recorded-role-renamed"}
    keep("role_update", admin("PUT", by_id, json=renamed_role))

    members = f"{path}/members"
    keep("member_add", admin("POST", members, json=user["id"]))
    keep("member_add_again", admin("POST", members, json=user["id"]))
    keep("member_organizations", admin("GET", f"/organizations/members/{user['id']}/organizations"))

    # The organization's group for the role: created, renamed, given the member and emptied again.
    groups = f"{path}/groups"
    paged = {"briefRepresentation": "false", "first": "0", "max": "100"}
    keep("organization_groups_none", admin("GET", groups, params=paged))
    group = {"name": "recorded-role", "attributes": {ROLE_ATTRIBUTE: [ROLE]}}
    keep("organization_group_create", admin("POST", groups, json=group))
    keep("organization_group_create_taken", admin("POST", groups, json=group))
    group_id = keep("organization_groups", admin("GET", groups, params=paged)).json()[0]["id"]
    keep(
        "organization_group_update",
        admin("PUT", f"{groups}/{group_id}", json={**group, "name": "recorded-role-renamed"}),
    )
    group_member = f"{groups}/{group_id}/members/{user['id']}"
    keep("group_member_add", admin("PUT", group_member))
    keep("group_member_add_again", admin("PUT", group_member))
    keep("group_member_remove", admin("DELETE", group_member))
    keep("group_member_remove_again", admin("DELETE", group_member))

    keep("member_remove", admin("DELETE", f"{members}/{user['id']}"))
    keep("member_remove_again", admin("DELETE", f"{members}/{user['id']}"))
    keep("member_organizations_none", admin("GET", f"/organizations/members/{user['id']}/organizations"))
    keep("organization_group_delete", admin("DELETE", f"{groups}/{group_id}"))
    keep("organization_group_delete_again", admin("DELETE", f"{groups}/{group_id}"))
    keep("role_delete", admin("DELETE", by_id))
    keep("role_delete_again", admin("DELETE", by_id))
    admin("DELETE", f"/roles-by-id/{permission_id}").raise_for_status()

    keep("user_delete", admin("DELETE", f"/users/{user['id']}"))
    keep("user_delete_again", admin("DELETE", f"/users/{user['id']}"))
    keep("user_get_none", admin("GET", f"/users/{user['id']}"))
    keep("member_organizations_unknown_user", admin("GET", f"/organizations/members/{user['id']}/organizations"))
    keep("organization_delete", admin("DELETE", path))
    keep("organization_delete_again", admin("DELETE", path))
    keep("member_add_unknown_organization", admin("POST", members, json=user["id"]))

    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
