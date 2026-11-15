#!/usr/bin/env python3
"""Record fixtures from the local Zitadel started by `make zitadel-up`.

Writes the discovery document, the JWKS and decoded access-token claims to
backend/tests/fixtures/zitadel/. To get tokens it creates a project, a role,
three applications and two machine users through the management API, and gives
the admin machine user the login-client role, so run it against a fresh
instance and `make zitadel-reset` afterwards.

    make -s zitadel-admin-pat | python3 scripts/zitadel-capture-fixtures.py

Standard library only. Signed tokens are never written, only their claims.
Nothing is written unless the whole capture succeeds.
"""

import base64
import hashlib
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ISSUER = "http://zitadel.localhost:8480"
# The first-instance human admin and its local-only password, from docker-compose.yml.
HUMAN_LOGIN_NAME = "zitadel-admin@zitadel.zitadel.localhost"
HUMAN_PASSWORD = "Local-only-Passw0rd!"
REDIRECT_URI = "http://localhost:3000/callback"
ROLE_KEY = "viewer"
OUT = Path(__file__).resolve().parent.parent / "backend" / "tests" / "fixtures" / "zitadel"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def call(method, path, *, token=None, body=None, form=None, basic=None):
    """Return (status, headers, parsed JSON body or None)."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(":".join(basic).encode()).decode()
    request = urllib.request.Request(ISSUER + path, data=data, headers=headers, method=method)
    try:
        with _opener.open(request, timeout=30) as response:
            status, response_headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        status, response_headers, raw = error.code, error.headers, error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        sys.exit(f"{method} {ISSUER}{path} failed: {reason}. Is the instance up? Run `make zitadel-up`.")
    if status >= 400:
        sys.exit(f"{method} {path} -> {status}: {raw.decode(errors='replace')}")
    try:
        return status, response_headers, json.loads(raw)
    except ValueError:
        return status, response_headers, None


def decode_segment(segment):
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def describe_token(access_token):
    """Decoded header and claims for a JWT, or a marker for an opaque token."""
    parts = access_token.split(".")
    if len(parts) != 3:
        return {"format": "opaque", "length": len(access_token)}
    return {"format": "jwt", "header": decode_segment(parts[0]), "claims": decode_segment(parts[1])}


_fixtures = {}


def record(name, value):
    _fixtures[name] = value


def write_fixtures():
    """Write every recorded fixture. Called once, after the whole capture succeeded."""
    OUT.mkdir(parents=True, exist_ok=True)
    for name, value in _fixtures.items():
        (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        print(f"wrote {OUT / name}")


def machine_user(pat, username, access_token_type=None):
    body = {"userName": username, "name": username}
    if access_token_type:
        body["accessTokenType"] = access_token_type
    _, _, created = call("POST", "/management/v1/users/machine", token=pat, body=body)
    user_id = created["userId"]
    _, _, user = call("GET", f"/management/v1/users/{user_id}", token=pat)
    _, _, secret = call("PUT", f"/management/v1/users/{user_id}/secret", token=pat, body={})
    return user_id, user["user"]["machine"], (secret["clientId"], secret["clientSecret"])


def client_credentials(credentials, scope):
    _, _, token = call(
        "POST",
        "/oauth/v2/token",
        form={"grant_type": "client_credentials", "scope": scope},
        basic=credentials,
    )
    return token


def oidc_app(pat, project_id, name, access_token_type=None, assert_roles=False):
    body = {
        "name": name,
        "redirectUris": [REDIRECT_URI],
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
        "appType": "OIDC_APP_TYPE_USER_AGENT",
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",
        "devMode": True,
    }
    if access_token_type:
        body["accessTokenType"] = access_token_type
    if assert_roles:
        body["accessTokenRoleAssertion"] = True
    _, _, created = call("POST", f"/management/v1/projects/{project_id}/apps/oidc", token=pat, body=body)
    _, _, app = call("GET", f"/management/v1/projects/{project_id}/apps/{created['appId']}", token=pat)
    return created["clientId"], app["app"]["oidcConfig"]


def human_token(pat, client_id, scope):
    """Authorization code + PKCE for the first-instance human, driven through the session API."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    _, headers, _ = call("GET", f"/oauth/v2/authorize?{query}")
    location = headers.get("Location")
    auth_requests = urllib.parse.parse_qs(urllib.parse.urlparse(location or "").query).get("authRequest")
    if not auth_requests:
        sys.exit(f"GET /oauth/v2/authorize did not redirect to Login V2 with an authRequest (Location: {location})")
    auth_request = auth_requests[0]
    _, _, session = call(
        "POST",
        "/v2/sessions",
        token=pat,
        body={"checks": {"user": {"loginName": HUMAN_LOGIN_NAME}, "password": {"password": HUMAN_PASSWORD}}},
    )
    _, _, finalized = call(
        "POST",
        f"/v2/oidc/auth_requests/{auth_request}",
        token=pat,
        body={"session": {"sessionId": session["sessionId"], "sessionToken": session["sessionToken"]}},
    )
    code = urllib.parse.parse_qs(urllib.parse.urlparse(finalized["callbackUrl"]).query)["code"][0]
    _, _, token = call(
        "POST",
        "/oauth/v2/token",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    return token


def main():
    pat = sys.stdin.read().strip()
    if not pat:
        sys.exit("pipe the admin PAT on stdin: make -s zitadel-admin-pat | python3 scripts/zitadel-capture-fixtures.py")

    _, _, discovery = call("GET", "/.well-known/openid-configuration")
    record("discovery.json", discovery)
    _, _, jwks = call("GET", urllib.parse.urlparse(discovery["jwks_uri"]).path)
    record("jwks.json", jwks)

    # Finishing an auth request for a session needs the login-client role.
    _, _, admin = call("GET", "/auth/v1/users/me", token=pat)
    call(
        "PUT",
        f"/admin/v1/members/{admin['user']['id']}",
        token=pat,
        body={"roles": ["IAM_OWNER", "IAM_LOGIN_CLIENT"]},
    )

    _, _, project = call("POST", "/management/v1/projects", token=pat, body={"name": "fixture-project"})
    project_id = project["id"]
    call(
        "POST",
        f"/management/v1/projects/{project_id}/roles",
        token=pat,
        body={"roleKey": ROLE_KEY, "displayName": "Viewer"},
    )
    project_scopes = f"urn:zitadel:iam:org:project:id:{project_id}:aud urn:zitadel:iam:org:projects:roles"
    all_scopes = f"openid profile email urn:zitadel:iam:user:resourceowner {project_scopes}"

    # Defaults: what a machine user and an application get when no token type is asked for.
    _, default_machine, default_credentials = machine_user(pat, "fixture-default")
    default_client_id, default_app = oidc_app(pat, project_id, "fixture-default-app")
    record(
        "defaults.json",
        {
            "machine_user": default_machine,
            "machine_user_token": describe_token(client_credentials(default_credentials, "openid")["access_token"]),
            "oidc_application": {key: default_app.get(key) for key in ("accessTokenType", "appType", "authMethodType")},
            "oidc_application_human_token": describe_token(
                human_token(pat, default_client_id, "openid email profile")["access_token"]
            ),
        },
    )

    # A machine user that asks for JWT access tokens, with one role granted.
    machine_id, _, credentials = machine_user(pat, "fixture-jwt", "ACCESS_TOKEN_TYPE_JWT")
    call(
        "POST",
        f"/management/v1/users/{machine_id}/grants",
        token=pat,
        body={"projectId": project_id, "roleKeys": [ROLE_KEY]},
    )
    record(
        "machine-access-token-openid.json",
        describe_token(client_credentials(credentials, "openid")["access_token"]),
    )
    record(
        "machine-access-token-project.json",
        describe_token(client_credentials(credentials, all_scopes)["access_token"]),
    )

    # A human through an application that asks for JWT access tokens, with the same role.
    by_login_name = {"queries": [{"loginNameQuery": {"loginName": HUMAN_LOGIN_NAME}}]}
    _, _, me = call("POST", "/v2/users", token=pat, body=by_login_name)
    call(
        "POST",
        f"/management/v1/users/{me['result'][0]['userId']}/grants",
        token=pat,
        body={"projectId": project_id, "roleKeys": [ROLE_KEY]},
    )
    jwt_client_id, _ = oidc_app(pat, project_id, "fixture-jwt-app", "OIDC_TOKEN_TYPE_JWT")
    record("human-access-token-openid.json", describe_token(human_token(pat, jwt_client_id, "openid")["access_token"]))
    human = human_token(pat, jwt_client_id, all_scopes)
    record("human-access-token-project.json", describe_token(human["access_token"]))
    record("human-id-token-project.json", describe_token(human["id_token"]))
    _, _, userinfo = call("GET", "/oidc/v1/userinfo", token=human["access_token"])
    record("human-userinfo-project.json", userinfo)

    # The same, through an application with accessTokenRoleAssertion on.
    roles_client_id, _ = oidc_app(pat, project_id, "fixture-jwt-roles-app", "OIDC_TOKEN_TYPE_JWT", assert_roles=True)
    record(
        "human-access-token-roles.json",
        describe_token(human_token(pat, roles_client_id, all_scopes)["access_token"]),
    )

    write_fixtures()


if __name__ == "__main__":
    main()
