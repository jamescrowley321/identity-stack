"""A real, provider-signed Ory token source, shared by the integration and E2E suites.

One implementation serves both targets because Ory Network's OAuth2 surface *is*
Ory Hydra: the local ``hydra-test`` container from docker-compose.test.yml (the
default), and a live Ory project when every ``ORY_TEST_*`` var below is set.

Tokens come out of the provider's own authorization-code flow. The login and
consent challenges are accepted through the admin API rather than a browser,
which is what lets a test choose the subject and the identity claims while the
JWT itself stays genuinely provider-signed. That matters: the point of the Ory
end-to-end work is that the shipped middleware verifies a real signature against
the provider's real JWKS (NFR-4), not that we can construct a token it likes.

This lives outside ``tests/integration/conftest.py`` because the E2E suite needs
the same tokens against the real running backend — a conftest is importable in
principle but importing one suite's conftest from another drags in its fixtures
and its collection assumptions. Callers own the skip-or-fail decision: this
module raises, it never calls ``pytest.skip``.
"""

import os
from urllib.parse import parse_qs, urlparse

import httpx

ORY_TEST_LIVE_VARS = (
    "ORY_TEST_ISSUER_URL",
    "ORY_TEST_ADMIN_URL",
    "ORY_TEST_ADMIN_TOKEN",
    "ORY_TEST_CLIENT_ID",
    "ORY_TEST_CLIENT_SECRET",
    "ORY_TEST_AUDIENCE",
)

HYDRA_PUBLIC_URL = os.environ.get("HYDRA_TEST_PUBLIC_URL", "http://127.0.0.1:14444")
HYDRA_ADMIN_URL = os.environ.get("HYDRA_TEST_ADMIN_URL", "http://127.0.0.1:14445")
HYDRA_AUDIENCE = "identity-stack-api"
HYDRA_REDIRECT_URI = "http://127.0.0.1:19999/callback"
HYDRA_SCOPE = "openid offline_access email profile"


class OryTokenSource:
    """Mints real, provider-signed Ory access tokens for the end-to-end suite."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        public_url: str,
        admin_url: str,
        client_id: str,
        client_secret: str,
        admin_headers: dict[str, str] | None = None,
        redirect_uri: str = HYDRA_REDIRECT_URI,
        is_live: bool = False,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.public_url = public_url.rstrip("/")
        self.admin_url = admin_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.admin_headers = admin_headers or {}
        self.redirect_uri = redirect_uri
        self.is_live = is_live

    @property
    def disco_address(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    def mint(
        self,
        *,
        sub: str,
        email: str | None = None,
        email_verified: bool = False,
        given_name: str = "",
        family_name: str = "",
        audience: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> str:
        """Run a full authorization-code exchange and return the access token.

        ``email`` is omitted from the token entirely when None — that is how the
        suite reproduces a principal the JIT provisioner must refuse.
        """
        audience = audience or self.audience
        client_id = client_id or self.client_id
        client_secret = client_secret or self.client_secret

        session_claims: dict[str, object] = {}
        if email is not None:
            session_claims["email"] = email
            session_claims["email_verified"] = email_verified
        if given_name:
            session_claims["given_name"] = given_name
        if family_name:
            session_claims["family_name"] = family_name

        with httpx.Client(follow_redirects=False, timeout=20) as http:
            response = http.get(
                f"{self.public_url}/oauth2/auth",
                params={
                    "client_id": client_id,
                    "response_type": "code",
                    "scope": HYDRA_SCOPE,
                    "redirect_uri": self.redirect_uri,
                    "state": "ory-e2e-state-value",
                    "audience": audience,
                },
            )
            login_challenge = _redirect_param(response, "login_challenge")

            response = http.put(
                f"{self.admin_url}/admin/oauth2/auth/requests/login/accept",
                params={"login_challenge": login_challenge},
                headers=self.admin_headers,
                json={"subject": sub, "remember": False},
            )
            response.raise_for_status()

            response = http.get(response.json()["redirect_to"])
            consent_challenge = _redirect_param(response, "consent_challenge")

            response = http.put(
                f"{self.admin_url}/admin/oauth2/auth/requests/consent/accept",
                params={"consent_challenge": consent_challenge},
                headers=self.admin_headers,
                json={
                    "grant_scope": HYDRA_SCOPE.split(),
                    "grant_access_token_audience": [audience],
                    "remember": False,
                    "session": {"access_token": session_claims, "id_token": {}},
                },
            )
            response.raise_for_status()

            response = http.get(response.json()["redirect_to"])
            code = _redirect_param(response, "code")

            response = http.post(
                f"{self.public_url}/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            response.raise_for_status()
            return response.json()["access_token"]

    def register_client(self, **overrides) -> tuple[str, str]:
        """Register an OAuth2 client through the admin API. Returns (id, secret)."""
        body = {
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "redirect_uris": [self.redirect_uri],
            "audience": [self.audience],
            "scope": HYDRA_SCOPE,
        }
        body.update(overrides)
        response = httpx.post(
            f"{self.admin_url}/admin/clients",
            headers=self.admin_headers,
            json=body,
            timeout=20,
        )
        response.raise_for_status()
        registered = response.json()
        return registered["client_id"], registered["client_secret"]

    def delete_client(self, client_id: str) -> None:
        """Remove a client registered by ``register_client``.

        Hydra's in-memory store is discarded with the container, but a live Ory
        project keeps what a test registers — so anything registered mid-test is
        cleaned up rather than left to accumulate.
        """
        response = httpx.delete(
            f"{self.admin_url}/admin/clients/{client_id}",
            headers=self.admin_headers,
            timeout=20,
        )
        if response.status_code not in (204, 404):
            response.raise_for_status()


def _redirect_param(response, name: str) -> str:
    """Pull a query parameter out of a 3xx Location header, or explain what came back."""
    location = response.headers.get("location")
    if not location:
        raise AssertionError(
            f"expected a redirect carrying '{name}' but got {response.status_code}: {response.text[:300]}"
        )
    values = parse_qs(urlparse(location).query).get(name)
    if not values:
        raise AssertionError(f"redirect to {location[:200]} carries no '{name}'")
    return values[0]


class OryTokenSourceUnavailable(RuntimeError):
    """No Ory provider is reachable — the caller decides whether that skips or fails."""


class OryTokenSourceMisconfigured(RuntimeError):
    """The live ``ORY_TEST_*`` group is partially set, which is never intentional."""


def build_ory_token_source() -> OryTokenSource:
    """Return a live-project token source when configured, else the local Hydra one.

    Gate semantics, chosen deliberately: no ``ORY_TEST_*`` var set is the normal
    case and falls through to the local container, so the suite exercises the Ory
    path on a laptop and in CI with no secret at all. A *partially* set group is a
    misconfiguration, not an absence, and raises rather than silently running
    against Hydra while the author believes it is hitting their live project.
    """
    configured = [name for name in ORY_TEST_LIVE_VARS if os.environ.get(name)]
    if configured and len(configured) != len(ORY_TEST_LIVE_VARS):
        missing = ", ".join(name for name in ORY_TEST_LIVE_VARS if not os.environ.get(name))
        raise OryTokenSourceMisconfigured(
            f"Ory live-test credentials are partially configured — missing: {missing}. "
            "Set all of them to run against a live Ory project, or none to use the "
            "local hydra-test container."
        )

    if configured:
        return OryTokenSource(
            issuer=os.environ["ORY_TEST_ISSUER_URL"],
            audience=os.environ["ORY_TEST_AUDIENCE"],
            public_url=os.environ["ORY_TEST_ISSUER_URL"],
            admin_url=os.environ["ORY_TEST_ADMIN_URL"],
            client_id=os.environ["ORY_TEST_CLIENT_ID"],
            client_secret=os.environ["ORY_TEST_CLIENT_SECRET"],
            admin_headers={"Authorization": f"Bearer {os.environ['ORY_TEST_ADMIN_TOKEN']}"},
            redirect_uri=os.environ.get("ORY_TEST_REDIRECT_URI", HYDRA_REDIRECT_URI),
            is_live=True,
        )

    try:
        ready = httpx.get(f"{HYDRA_PUBLIC_URL}/health/ready", timeout=5)
        ready.raise_for_status()
    except Exception as exc:
        raise OryTokenSourceUnavailable(
            f"hydra-test is not reachable at {HYDRA_PUBLIC_URL} ({exc}) — bring up the "
            "test stack with `docker compose -f docker-compose.test.yml up -d --wait`"
        ) from exc

    source = OryTokenSource(
        issuer=HYDRA_PUBLIC_URL,
        audience=HYDRA_AUDIENCE,
        public_url=HYDRA_PUBLIC_URL,
        admin_url=HYDRA_ADMIN_URL,
        client_id="",
        client_secret="",
    )
    # Bootstrap once per session: Hydra's store is in-memory and process-global,
    # so a per-test client would leak registrations across the whole run.
    source.client_id, source.client_secret = source.register_client()
    return source
