"""Shared fixtures for repository unit tests.

Postgres is provisioned externally by `make test-unit` via docker-compose.test.yml
and reached at `TEST_DATABASE_URL` (or `DATABASE_URL`). Alembic migrations run
once per session against the pre-provisioned DB; tests get per-test transactional
rollback for isolation.
"""

import os

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def _require_test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL (or DATABASE_URL) not set — bring up test stack with "
            "`docker compose -f docker-compose.test.yml up -d --wait` and set "
            "TEST_DATABASE_URL=postgresql+asyncpg://identity_test:identity_test@localhost:15432/identity_test"
        )
    return url


@pytest.fixture(scope="session")
def postgres_url() -> str:
    return _require_test_database_url()


@pytest.fixture(scope="session")
def _run_migrations(postgres_url):
    """Run Alembic migrations against the pre-provisioned Postgres (once per session)."""
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
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Alembic migration failed:\nstdout={result.stdout}\nstderr={result.stderr}")


@pytest.fixture(scope="session")
def async_engine(postgres_url, _run_migrations):
    return create_async_engine(postgres_url, echo=False, poolclass=NullPool)


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(async_engine):
    """Per-test async session with transactional rollback for clean state."""
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
