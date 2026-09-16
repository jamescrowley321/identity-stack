"""Shared fixtures for integration tests.

Postgres and Redis are provisioned externally by `make test-integration` via
docker-compose.test.yml and reached via TEST_DATABASE_URL / TEST_REDIS_URL
(or DATABASE_URL / REDIS_URL). Descope fixtures pull live tokens from the
project pointed to by the Descope env vars.

Three fixture groups:
1. Descope — live OIDC/management tokens (requires env vars)
2. Postgres — pre-provisioned via compose; Alembic runs once per session
3. Redis — pre-provisioned via compose
"""

import os
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from py_identity_model import (
    ClientCredentialsTokenRequest,
    DiscoveryDocumentRequest,
    get_discovery_document,
    request_client_credentials_token,
)
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def _unavailable(reason: str) -> None:
    """Skip locally, fail in CI.

    Skipping is right on a laptop: not every contributor has Descope credentials
    or a test stack up. In CI it is the wrong answer — a skip there lets the
    ``Integration Tests`` job report green with nothing exercised, which is the
    same hollow gate the E2E suite had for months. So when ``CI`` is set, a
    missing credential or dependency is a failure, not a skip.
    """
    if os.environ.get("CI"):
        pytest.fail(
            f"{reason}. In CI this fails rather than skips: a skip here would let the "
            "Integration Tests job report green without exercising anything. Fix the "
            "secret or the test stack rather than letting the gate go hollow."
        )
    pytest.skip(reason)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        _unavailable(f"{name} is not set")
    return value


# ──────────────────────────────────────────────
# Descope integration fixtures (existing)
# ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def descope_project_id():
    return _require_env("DESCOPE_PROJECT_ID")


@pytest.fixture(scope="session")
def descope_client_id():
    return _require_env("DESCOPE_CLIENT_ID")


@pytest.fixture(scope="session")
def descope_client_secret():
    return _require_env("DESCOPE_CLIENT_SECRET")


@pytest.fixture(scope="session")
def disco_address(descope_project_id):
    return f"https://api.descope.com/{descope_project_id}/.well-known/openid-configuration"


@pytest.fixture(scope="session")
def discovery_document(disco_address):
    response = get_discovery_document(DiscoveryDocumentRequest(address=disco_address))
    assert response.is_successful, f"Discovery failed: {response.error}"
    return response


@pytest.fixture(scope="session")
def access_token(descope_client_id, descope_client_secret, discovery_document):
    """Get a valid access token via client credentials flow."""
    response = request_client_credentials_token(
        ClientCredentialsTokenRequest(
            client_id=descope_client_id,
            client_secret=descope_client_secret,
            address=discovery_document.token_endpoint,
            scope="openid",
        )
    )
    assert response.is_successful, f"Token request failed: {response.error}"
    return response.token["access_token"]


@pytest.fixture(scope="session")
def expired_token():
    return _require_env("DESCOPE_EXPIRED_TOKEN")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    """Async test client for the FastAPI app."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ──────────────────────────────────────────────
# Postgres fixtures (provisioned via docker-compose.test.yml)
# ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def postgres_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        _unavailable(
            "TEST_DATABASE_URL (or DATABASE_URL) not set — bring up test stack with "
            "`docker compose -f docker-compose.test.yml up -d --wait` and set "
            "TEST_DATABASE_URL=postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test"
        )
    return url


@pytest.fixture(scope="session")
def _run_migrations(postgres_url):
    """Run Alembic migrations against the test Postgres instance (once per session).

    Uses subprocess to avoid asyncio.run() inside Alembic's env.py from
    interfering with the pytest-asyncio session event loop.
    """
    import subprocess
    import sys

    project_root = os.path.join(os.path.dirname(__file__), "..", "..")
    env = os.environ.copy()
    env["DATABASE_URL"] = postgres_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Alembic migration failed:\nstdout={result.stdout}\nstderr={result.stderr}")


@pytest.fixture(scope="session")
def async_engine(postgres_url, _run_migrations):
    """Create an async engine pointing at the test Postgres instance."""
    engine = create_async_engine(postgres_url, echo=False, poolclass=NullPool)
    return engine


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(async_engine):
    """Per-test async session with transactional rollback for clean state.

    Uses loop_scope="session" to match the session-scoped event loop
    configured via asyncio_default_test_loop_scope in pyproject.toml.
    """
    async with async_engine.connect() as conn:
        transaction = await conn.begin()
        await conn.begin_nested()
        session_factory = async_sessionmaker(bind=conn, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:

            @event.listens_for(session.sync_session, "after_transaction_end")
            def _restart_savepoint(sync_session, trans):
                if conn.closed or not conn.in_transaction():
                    return
                if not conn.in_nested_transaction():
                    conn.sync_connection.begin_nested()

            yield session

            event.remove(session.sync_session, "after_transaction_end", _restart_savepoint)
        await transaction.rollback()


@pytest.fixture
def noop_adapter():
    """NoOpSyncAdapter instance for service tests."""
    from app.services.adapters.noop import NoOpSyncAdapter

    return NoOpSyncAdapter()


# ──────────────────────────────────────────────
# Redis fixtures (provisioned via docker-compose.test.yml)
# ──────────────────────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def redis_client():
    """Async Redis client connected to the pre-provisioned test Redis."""
    from redis.asyncio import Redis

    url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not url:
        _unavailable(
            "TEST_REDIS_URL (or REDIS_URL) not set — bring up test stack with "
            "`docker compose -f docker-compose.test.yml up -d --wait` and set "
            "TEST_REDIS_URL=redis://localhost:16379/0"
        )
    client = Redis.from_url(url, decode_responses=True)
    yield client
    await client.aclose()


# ──────────────────────────────────────────────
# Ory fixtures — real provider-signed tokens
# ──────────────────────────────────────────────
#
# One implementation serves both targets because Ory Network's OAuth2 surface
# *is* Ory Hydra: the local `hydra-test` container from docker-compose.test.yml
# (the default), and a live Ory project when every ORY_TEST_* var below is set.
#
# Tokens come out of the provider's own authorization-code flow. The login and
# consent challenges are accepted through the admin API rather than a browser,
# which is what lets a test choose the subject and the identity claims while the
# JWT itself stays genuinely provider-signed. That matters: the capstone's whole
# point is that the shipped middleware verifies a real signature against the
# provider's real JWKS (NFR-4), not that we can construct a token it likes.

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


@pytest.fixture(scope="session")
def ory_token_source() -> OryTokenSource:
    """An Ory token source: a live project when configured, else local Hydra.

    Gate semantics, chosen deliberately: no ORY_TEST_* var set is the normal
    case and falls through to the local container, so the suite exercises the
    Ory path on a laptop and in CI with no secret at all. A *partially* set
    group is a misconfiguration, not an absence, and fails loudly rather than
    silently running against Hydra while the author believes it is hitting their
    live project.
    """
    configured = [name for name in ORY_TEST_LIVE_VARS if os.environ.get(name)]
    if configured and len(configured) != len(ORY_TEST_LIVE_VARS):
        missing = ", ".join(name for name in ORY_TEST_LIVE_VARS if not os.environ.get(name))
        pytest.fail(
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
        _unavailable(
            f"hydra-test is not reachable at {HYDRA_PUBLIC_URL} ({exc}) — bring up the "
            "test stack with `docker compose -f docker-compose.test.yml up -d --wait`"
        )

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
