"""Shared fixtures for repository unit tests.

Uses testcontainers Postgres with Alembic migrations and per-test
transactional rollback — the same pattern as integration/conftest.py.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/testdb")

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_container():
    """Start a real Postgres container for the test session."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def postgres_url(postgres_container):
    """Async-compatible connection URL for the testcontainers Postgres instance."""
    from urllib.parse import urlparse, urlunparse

    sync_url = postgres_container.get_connection_url()
    parsed = urlparse(sync_url)
    async_url = urlunparse(parsed._replace(scheme="postgresql+asyncpg"))
    return async_url


@pytest.fixture(scope="session")
def _run_migrations(postgres_url):
    """Run Alembic migrations against the test Postgres instance (once per session).

    Uses subprocess to avoid asyncio.run() inside Alembic's env.py from
    interfering with the pytest-asyncio session event loop.
    """
    import subprocess
    import sys

    project_root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    env = os.environ.copy()
    env["DATABASE_URL"] = postgres_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Alembic migration failed:\n{result.stderr}")


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
        await transaction.rollback()
