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

from tests.helpers.ory import (
    OryTokenSource,
    OryTokenSourceMisconfigured,
    OryTokenSourceUnavailable,
    build_ory_token_source,
)


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
# The token source itself lives in tests/helpers/ory.py: the E2E suite mints the
# same tokens against the real running backend, and neither suite should have to
# import the other's conftest to get them. Only the skip-or-fail policy — which
# differs by suite — is decided here.


@pytest.fixture(scope="session")
def ory_token_source() -> OryTokenSource:
    """An Ory token source: a live project when configured, else local Hydra."""
    try:
        return build_ory_token_source()
    except OryTokenSourceMisconfigured as exc:
        # Not an absence — someone set half a credential group. Always loud.
        pytest.fail(str(exc))
    except OryTokenSourceUnavailable as exc:
        _unavailable(str(exc))
