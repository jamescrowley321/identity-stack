"""Real, Keycloak-signed tokens from the ``keycloak-test`` container.

Tokens come out of the realm's own authorization-code flow with PKCE, through
the login form, as the fixture users in ``keycloak/fixtures/README.md``. Nothing
here signs or edits a token.

Two cases the realm does not produce as imported (a non-RS256 signature, a
token already expired) are produced by changing one attribute of the app client
through the admin API for the length of one login, then restoring it. The token
is still issued and signed by the container.

Callers own the fail decision: this module raises, it never calls ``pytest``.
"""

import base64
import hashlib
import html
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

KEYCLOAK_BASE_URL = os.environ.get("KEYCLOAK_TEST_BASE_URL", "http://keycloak-test.localhost:18180")
KEYCLOAK_REALM = "identity-stack"
KEYCLOAK_ADMIN_REALM = "master"
KEYCLOAK_ADMIN_CLIENT_ID = "admin-cli"
KEYCLOAK_ADMIN_USERNAME = "admin"
KEYCLOAK_ADMIN_PASSWORD = "admin"
# The app clients register exact URIs: any other redirect is refused.
KEYCLOAK_REDIRECT_URI = "http://localhost:3000"
KEYCLOAK_POST_LOGOUT_REDIRECT_URI = "http://localhost:3000/login"

MEMBER_TYPE_CLIENT = "member"
TENANT_ADMIN_TYPE_CLIENT = "tenant-admin"
MEMBER_APP_CLIENT = "member-app"
TENANT_ADMIN_APP_CLIENT = "tenant-admin-app"
ACME_SCOPE = "openid organization:acme"

# Client attributes Keycloak reads when it issues an access token.
ACCESS_TOKEN_ALG_ATTRIBUTE = "access.token.signed.response.alg"
ACCESS_TOKEN_LIFESPAN_ATTRIBUTE = "access.token.lifespan"

_FORM_ACTION = re.compile(r'<form[^>]*\baction="([^"]+)"')
_HTTP_TIMEOUT_SECONDS = 20


class KeycloakTokenSourceUnavailable(Exception):
    """The ``keycloak-test`` container is not reachable."""


class KeycloakFlowError(Exception):
    """The login flow did not go the way the realm fixture says it should."""


@dataclass(frozen=True)
class KeycloakUser:
    username: str
    password: str


ALICE = KeycloakUser("alice", "local-only-alice")
BOB = KeycloakUser("bob", "local-only-bob")
# In ``acme`` with no client role: her access token carries no ``aud``.
CAROL = KeycloakUser("carol", "local-only-carol")


@dataclass(frozen=True)
class KeycloakTokens:
    access_token: str
    id_token: str


def decode_unverified(token: str) -> tuple[dict, dict]:
    """Header and claims of a JWT, for assertions about what the provider issued."""
    header_b64, payload_b64, _signature = token.split(".")
    return _b64_json(header_b64), _b64_json(payload_b64)


def _b64_json(segment: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class _CookieJar:
    """Keycloak marks its session cookies ``Secure`` on a plain-HTTP origin.

    A standard cookie jar then never sends them back and the login form answers
    400, so the cookies are carried by hand.
    """

    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}

    def store(self, response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            name, _, value = header.split(";", 1)[0].partition("=")
            self._cookies[name.strip()] = value.strip()

    def header(self) -> dict[str, str]:
        return {"Cookie": "; ".join(f"{name}={value}" for name, value in self._cookies.items())}


class KeycloakTokenSource:
    """Logs fixture users in to the ``identity-stack`` realm and returns their tokens."""

    def __init__(self, *, base_url: str = KEYCLOAK_BASE_URL, realm: str = KEYCLOAK_REALM) -> None:
        self.base_url = base_url.rstrip("/")
        self.realm = realm

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/realms/{self.realm}"

    @property
    def end_session_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/logout"

    def check_reachable(self) -> None:
        try:
            response = httpx.get(f"{self.issuer}/.well-known/openid-configuration", timeout=_HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KeycloakTokenSourceUnavailable(
                f"keycloak-test is not reachable at {self.issuer} ({exc}). `make test-integration` starts it: "
                "`docker compose -f docker-compose.test.yml --profile keycloak up -d --wait`."
            ) from exc

    def login(self, user: KeycloakUser, *, client_id: str, scope: str = ACME_SCOPE) -> KeycloakTokens:
        """Authorization code flow with PKCE S256 through the identity-first login form."""
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        openid = f"{self.issuer}/protocol/openid-connect"
        cookies = _CookieJar()

        with httpx.Client(follow_redirects=False, timeout=_HTTP_TIMEOUT_SECONDS) as http:
            response = http.get(
                f"{openid}/auth",
                params={
                    "client_id": client_id,
                    "response_type": "code",
                    "scope": scope,
                    "redirect_uri": KEYCLOAK_REDIRECT_URI,
                    "state": secrets.token_urlsafe(16),
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
            )
            cookies.store(response)
            response = http.post(_form_action(response), data={"username": user.username}, headers=cookies.header())
            cookies.store(response)
            response = http.post(_form_action(response), data={"password": user.password}, headers=cookies.header())
            code = _redirect_param(response, "code")

            response = http.post(
                f"{openid}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": KEYCLOAK_REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
            )
            if response.status_code != httpx.codes.OK:
                raise KeycloakFlowError(f"token exchange failed: {response.status_code} {response.text[:500]}")
            body = response.json()
            return KeycloakTokens(access_token=body["access_token"], id_token=body["id_token"])

    def admin_access_token(self) -> str:
        """An access token from the ``master`` realm: Keycloak-signed, on another issuer."""
        response = httpx.post(
            f"{self.base_url}/realms/{KEYCLOAK_ADMIN_REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": KEYCLOAK_ADMIN_CLIENT_ID,
                "username": KEYCLOAK_ADMIN_USERNAME,
                "password": KEYCLOAK_ADMIN_PASSWORD,
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    @contextmanager
    def client_attribute(self, client_id: str, name: str, value: str) -> Iterator[None]:
        """Set one attribute on a realm client, and put the client back afterwards."""
        admin = f"{self.base_url}/admin/realms/{self.realm}/clients"
        headers = {"Authorization": f"Bearer {self.admin_access_token()}"}
        with httpx.Client(headers=headers, timeout=_HTTP_TIMEOUT_SECONDS) as http:
            response = http.get(admin, params={"clientId": client_id})
            response.raise_for_status()
            matches = [client for client in response.json() if client["clientId"] == client_id]
            if len(matches) != 1:
                raise KeycloakFlowError(f"expected one realm client {client_id!r}, found {len(matches)}")
            (original,) = matches
            changed = {**original, "attributes": {**original.get("attributes", {}), name: value}}
            http.put(f"{admin}/{original['id']}", json=changed).raise_for_status()
            try:
                yield
            finally:
                # Keycloak merges attributes on update: an attribute left out of the
                # body is kept, an empty one is removed.
                restored = {**original, "attributes": {name: "", **original.get("attributes", {})}}
                http.put(f"{admin}/{original['id']}", json=restored).raise_for_status()


def _form_action(response: httpx.Response) -> str:
    match = _FORM_ACTION.search(response.text)
    if response.status_code != httpx.codes.OK or match is None:
        raise KeycloakFlowError(f"expected a login form, got {response.status_code}: {response.text[:500]}")
    return html.unescape(match.group(1))


def _redirect_param(response: httpx.Response, name: str) -> str:
    location = response.headers.get("location", "")
    values = parse_qs(urlparse(location).query).get(name)
    if response.status_code != httpx.codes.FOUND or not values:
        raise KeycloakFlowError(f"expected a redirect carrying {name!r}, got {response.status_code} {location!r}")
    (value,) = values
    return value
