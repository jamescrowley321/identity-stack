"""Shared fixtures for integration tests.

Two fixture groups:
1. Descope fixtures — for live integration tests against a Descope instance
2. Postgres fixtures — testcontainers-based real Postgres with Alembic migrations
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/testdb")

import pytest
from httpx import ASGITransport, AsyncClient
from py_identity_model import (
    ClientCredentialsTokenRequest,
    DiscoveryDocumentRequest,
    get_discovery_document,
    request_client_credentials_token,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} not set — skipping integration tests")
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
# Testcontainers Postgres fixtures (AC-1.5.5)
# ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def postgres_container():
    """Start a real Postgres container for the test session."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def postgres_url(postgres_container):
    """Async-compatible connection URL for the testcontainers Postgres instance."""
    # testcontainers gives psycopg2 URL — convert to asyncpg
    sync_url = postgres_container.get_connection_url()
    return sync_url.replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
def _run_migrations(postgres_url):
    """Run Alembic migrations against the test Postgres instance (once per session)."""
    from alembic import command
    from alembic.config import Config

    # Alembic needs the sync URL for offline mode, but we run online with the async URL
    # Set DATABASE_URL so migrations/env.py picks it up
    os.environ["DATABASE_URL"] = postgres_url

    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "..", "migrations"))
    command.upgrade(alembic_cfg, "head")


@pytest.fixture(scope="session")
def async_engine(postgres_url, _run_migrations):
    """Create an async engine pointing at the test Postgres instance."""
    engine = create_async_engine(postgres_url, echo=False)
    return engine


@pytest.fixture
async def db_session(async_engine):
    """Per-test async session with transactional rollback for clean state.

    Each test runs inside a transaction that is rolled back after the test,
    ensuring complete isolation between tests.
    """
    async with async_engine.connect() as conn:
        transaction = await conn.begin()
        session_factory = async_sessionmaker(bind=conn, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            yield session
        await transaction.rollback()


@pytest.fixture
def noop_adapter():
    """NoOpSyncAdapter instance for service tests."""
    from app.services.adapters.noop import NoOpSyncAdapter

    return NoOpSyncAdapter()
