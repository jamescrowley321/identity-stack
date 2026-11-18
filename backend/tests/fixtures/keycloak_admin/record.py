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
DIRECTORY_CLIENT = ("identity-stack-directory", "local-only-directory-client")
TENANT = "7e5a0000-0000-4000-8000-000000000586"
USER = "05e40000-0000-4000-8000-000000000586"


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
    directory_grant = http.post(TOKEN_URL, data=GRANT, auth=DIRECTORY_CLIENT)
    directory_token = directory_grant.json()["access_token"]
    keep("directory_token", directory_grant, drop_token="recorded-directory-token")
    forbidden = {"name": "Forbidden", "alias": "forbidden"}
    directory = {"Authorization": f"Bearer {directory_token}"}
    keep("organization_create_forbidden", http.post(f"{ADMIN}/organizations", json=forbidden, headers=directory))
    keep("credential_check", http.get(f"{ADMIN}/organizations", params=first, headers=bearer))
    keep("directory_credential_check", http.get(f"{ADMIN}/organizations", params=first, headers=directory))

    # The startup role check: each client reads its own service account's realm-management roles.
    for prefix, client, headers in (("", ADMIN_CLIENT, bearer), ("directory_", DIRECTORY_CLIENT, directory)):
        by_name = {"username": f"service-account-{client[0]}", "exact": "true"}
        account = keep(f"{prefix}service_account_find", http.get(f"{ADMIN}/users", params=by_name, headers=headers))
        mappings_path = f"{ADMIN}/users/{account.json()[0]['id']}/role-mappings"
        mappings = keep(f"{prefix}service_account_role_mappings", http.get(mappings_path, headers=headers))
        # The read-only directory client is shown no client mappings at all ({}).
        realm_management = mappings.json().get("clientMappings", {}).get("realm-management")
        if realm_management is not None:
            roles_path = f"{mappings_path}/clients/{realm_management['id']}/composite"
            keep(f"{prefix}service_account_roles", http.get(roles_path, headers=headers))

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

    members = f"{path}/members"
    keep("member_add", admin("POST", members, json=user["id"]))
    keep("member_add_again", admin("POST", members, json=user["id"]))
    # A realm admin's grant markers listing the member, then the adapter trimming them as the membership ends.
    found = admin("GET", path).json()
    marked = {"type:tenant-admin": [user["id"], "another-member"], "type:viewer": [user["id"]], "note": ["kept"]}
    keep("organization_mark", admin("PUT", path, json={**found, "attributes": marked}))
    keep("organization_get_marked", admin("GET", path))
    trimmed = {"type:tenant-admin": ["another-member"], "note": ["kept"]}
    keep("organization_trim", admin("PUT", path, json={**found, "attributes": trimmed}))
    keep("organization_get", admin("GET", path))
    keep("member_remove", admin("DELETE", f"{members}/{user['id']}"))
    keep("member_remove_again", admin("DELETE", f"{members}/{user['id']}"))

    keep("user_delete", admin("DELETE", f"/users/{user['id']}"))
    keep("user_delete_again", admin("DELETE", f"/users/{user['id']}"))
    keep("user_get_none", admin("GET", f"/users/{user['id']}"))
    keep("organization_delete", admin("DELETE", path))
    keep("organization_delete_again", admin("DELETE", path))
    keep("member_add_unknown_organization", admin("POST", members, json=user["id"]))

    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
