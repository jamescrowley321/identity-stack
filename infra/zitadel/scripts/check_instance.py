"""Read an applied infra/zitadel root back from the instance and check what the root promises.

Usage: python check_instance.py <terraform output -json file> <access_token_lifetime>

The second argument is the access_token_lifetime the root was applied with, as the
Go duration dev.tfvars sets (15m0s). The instance's admin token comes from
TF_VAR_zitadel_access_token, the variable the root itself reads. Needs httpx (the
backend venv). Exits 1 with every failed check listed.

Checks:
- No non-sensitive output contains a sensitive output's value or the admin token.
- The instance access-token lifetime is the one the root was applied with.
- allow_register is false in the instance default login policy, and every
  organization has its own login policy with allow_register false.
- Login V2 refuses a registration in every organization: the register page shows
  no registration form, only "registration is disabled". This is the self-service
  register flow only; the user API is not refused (see README.md).
- The project checks no roles and asserts none, has the four role definitions, and
  is granted to the tenant organizations (the fixture's canonical tenants) only.
- Nobody holds a role grant, in any organization.
- The SPA and the second application are the public, PKCE, JWT applications the
  root declares, without a roles claim.
- Each fixture user (infra/fixtures/test-identities.json) is in the organization of
  their home tenant with a verified email; the machine
  user issues JWT access tokens.
- The sync adapter's token (sync_admin_token) is the identity-stack-sync machine
  user's, and that user holds the instance role IAM_ORG_MANAGER and no other.
"""

import json
import os
import re
import sys
from pathlib import Path

import httpx

# A Go duration in hours, minutes and seconds, the form variables.tf accepts.
GO_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
PROJECT_ROLES = {"owner", "admin", "member", "viewer"}
# The test tenants and users the root builds.
FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "test-identities.json").read_text())
TENANT_ORGANIZATIONS = {key for key, tenant in FIXTURE["tenants"].items() if tenant["canonical"]}
HOME_ORGANIZATIONS = {key: user["home"] for key, user in FIXTURE["users"].items()}
SENSITIVE_OUTPUTS = {"test_user_passwords", "machine_user_client_secret", "sync_admin_token"}
# The sync adapter's machine user and the one instance role it holds.
SYNC_USER_NAME = "identity-stack-sync"
SYNC_INSTANCE_ROLES = ["IAM_ORG_MANAGER"]
# What Login V2 renders on its register page: the form's email field, and the
# text it shows instead of the form when the organization's policy refuses.
REGISTER_FORM_MARKER = 'data-testid="email-text-input"'
REGISTER_REFUSED_MARKER = "The registration is disabled"
TIMEOUT_SECONDS = 15

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def as_seconds(duration: str) -> str:
    """Write a Go duration (15m0s) the way Zitadel's settings API answers it (900s)."""
    match = GO_DURATION.match(duration)
    if not duration or match is None:
        raise ValueError(f"{duration!r} is not a Go duration in hours, minutes and seconds")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return f"{hours * 3600 + minutes * 60 + seconds}s"


class Instance:
    def __init__(self, issuer: str, token: str) -> None:
        self.issuer = issuer
        self.token = token

    def _call(self, method: str, path: str, org_id: str | None, body: dict | None, auth: bool = True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        if org_id:
            headers["x-zitadel-orgid"] = org_id
        response = httpx.request(
            method, f"{self.issuer}{path}", headers=headers, json=body, timeout=TIMEOUT_SECONDS, follow_redirects=True
        )
        response.raise_for_status()
        return response.text if not auth else response.json()

    def get(self, path: str, org_id: str | None = None) -> dict:
        return self._call("GET", path, org_id, None)

    def search(self, path: str, org_id: str | None = None) -> list[dict]:
        return self._call("POST", path, org_id, {}).get("result", [])

    def page(self, path: str) -> str:
        return self._call("GET", path, None, None, auth=False)


def flatten_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in flatten_strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in flatten_strings(item)]
    return []


def check_outputs(outputs: dict, token: str) -> None:
    sensitive = {name for name, output in outputs.items() if output["sensitive"]}
    check(sensitive == SENSITIVE_OUTPUTS, f"sensitive outputs are {sorted(sensitive)}, expected {sorted(SENSITIVE_OUTPUTS)}")
    secrets = [token] + [s for name in sensitive for s in flatten_strings(outputs[name]["value"])]
    check(all(secrets), "a sensitive output holds an empty value")
    for name, output in outputs.items():
        if output["sensitive"]:
            continue
        text = json.dumps(output["value"])
        check(not any(secret and secret in text for secret in secrets), f"output {name} contains a secret")


def check_settings(instance: Instance, org_ids: dict[str, str], expected_lifetime: str) -> None:
    lifetime = instance.get("/admin/v1/settings/oidc")["settings"].get("accessTokenLifetime")
    check(lifetime == expected_lifetime, f"access-token lifetime is {lifetime}, expected {expected_lifetime}")

    # A false boolean is left out of the JSON answer, so absent reads as false.
    default = instance.get("/admin/v1/policies/login")["policy"]
    check(default.get("allowRegister", False) is False, "the instance default login policy allows registration")
    for key, org_id in sorted(org_ids.items()):
        policy = instance.get("/management/v1/policies/login", org_id)["policy"]
        check(policy.get("isDefault", False) is False, f"organization {key} has no login policy of its own")
        check(policy.get("allowRegister", False) is False, f"organization {key}'s login policy allows registration")
        page = instance.page(f"/ui/v2/login/register?organization={org_id}")
        check(REGISTER_FORM_MARKER not in page, f"Login V2 offers a registration form in organization {key}")
        check(REGISTER_REFUSED_MARKER in page, f"Login V2 does not say registration is disabled in organization {key}")


def check_project(instance: Instance, outputs: dict, org_ids: dict[str, str]) -> None:
    platform = org_ids["platform"]
    project_id = outputs["project_id"]["value"]
    project = instance.get(f"/management/v1/projects/{project_id}", platform)["project"]
    for flag in ("projectRoleCheck", "hasProjectCheck", "projectRoleAssertion"):
        check(project.get(flag, False) is False, f"the project sets {flag}")

    roles = {role["key"] for role in instance.search(f"/management/v1/projects/{project_id}/roles/_search", platform)}
    check(roles == PROJECT_ROLES, f"project roles are {sorted(roles)}, expected {sorted(PROJECT_ROLES)}")

    grants = instance.search(f"/management/v1/projects/{project_id}/grants/_search", platform)
    granted = {grant["grantedOrgId"] for grant in grants}
    expected = {org_ids[key] for key in TENANT_ORGANIZATIONS}
    check(granted == expected, f"the project is granted to {sorted(granted)}, expected the tenants {sorted(expected)}")

    for key, org_id in sorted(org_ids.items()):
        user_grants = instance.search("/management/v1/users/grants/_search", org_id)
        check(user_grants == [], f"organization {key} holds {len(user_grants)} user role grant(s)")

    apps = {
        app["oidcConfig"]["clientId"]: app["oidcConfig"]
        for app in instance.search(f"/management/v1/projects/{project_id}/apps/_search", platform)
        if "oidcConfig" in app
    }
    frontend_url = outputs["frontend_environment"]["value"]["VITE_OIDC_REDIRECT_URI"]
    common = {
        "appType": "OIDC_APP_TYPE_USER_AGENT",
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        "accessTokenType": "OIDC_TOKEN_TYPE_JWT",
        "accessTokenRoleAssertion": False,
        "idTokenRoleAssertion": False,
        "redirectUris": [frontend_url],
        "postLogoutRedirectUris": [f"{frontend_url}/login"],
    }
    wanted = {
        outputs["spa_client_id"]["value"]: {
            **common,
            "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE", "OIDC_GRANT_TYPE_REFRESH_TOKEN"],
        },
        outputs["other_client_id"]["value"]: {**common, "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]},
    }
    check(set(apps) == set(wanted), f"the project's applications are {sorted(apps)}, expected {sorted(wanted)}")
    for client_id, settings in wanted.items():
        actual = apps.get(client_id, {})
        got = {field: actual.get(field, False if isinstance(value, bool) else None) for field, value in settings.items()}
        check(got == settings, f"application {client_id} is {got}, expected {settings}")


def check_users(instance: Instance, outputs: dict, org_ids: dict[str, str]) -> None:
    user_ids = outputs["test_user_ids"]["value"]
    expected = sorted(HOME_ORGANIZATIONS)
    check(sorted(user_ids) == expected, f"test users are {sorted(user_ids)}, expected {expected}")
    for key, user_id in sorted(user_ids.items()):
        user = instance.get(f"/v2/users/{user_id}")["user"]
        home = HOME_ORGANIZATIONS.get(key)
        check(user["details"]["resourceOwner"] == org_ids.get(home), f"test user {key} is not in organization {home}")
        check(user.get("human", {}).get("email", {}).get("isVerified", False) is True, f"test user {key}'s email is not verified")

    machine = instance.get(f"/v2/users/{outputs['machine_user']['value']['id']}")["user"]
    token_type = machine.get("machine", {}).get("accessTokenType")
    check(token_type == "ACCESS_TOKEN_TYPE_JWT", f"the machine user's access-token type is {token_type}")


def check_sync_user(instance: Instance, outputs: dict) -> None:
    sync = Instance(instance.issuer, outputs["sync_admin_token"]["value"])
    me = sync.get("/auth/v1/users/me")["user"]
    check(me.get("userName") == SYNC_USER_NAME, f"sync_admin_token belongs to {me.get('userName')}, not {SYNC_USER_NAME}")
    members = instance._call("POST", "/admin/v1/members/_search", None, {"queries": [{"userIdQuery": {"userId": me["id"]}}]})
    roles = [role for member in members.get("result", []) for role in member.get("roles", [])]
    check(roles == SYNC_INSTANCE_ROLES, f"{SYNC_USER_NAME} holds instance roles {roles}, expected {SYNC_INSTANCE_ROLES}")


def main() -> int:
    with open(sys.argv[1]) as handle:
        outputs = json.load(handle)
    expected_lifetime = as_seconds(sys.argv[2])
    token = os.environ["TF_VAR_zitadel_access_token"]
    instance = Instance(outputs["issuer_url"]["value"], token)
    org_ids = outputs["organization_ids"]["value"]

    check_outputs(outputs, token)
    try:
        check_settings(instance, org_ids, expected_lifetime)
        check_project(instance, outputs, org_ids)
        check_users(instance, outputs, org_ids)
        check_sync_user(instance, outputs)
    except httpx.HTTPStatusError as exc:
        response = exc.response
        failures.append(
            f"the instance answered {response.status_code} {response.reason_phrase} to {exc.request.url}; later checks did not run"
        )
    except httpx.TransportError as exc:
        failures.append(f"the instance did not answer: {exc}; later checks did not run")
    except (json.JSONDecodeError, KeyError) as exc:
        failures.append(f"the instance's answer is not the expected shape ({exc!r}); later checks did not run")

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("check_instance: the instance matches infra/zitadel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
