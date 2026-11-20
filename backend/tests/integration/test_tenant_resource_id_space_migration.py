"""Migration 012 puts each stored tenant resource in its tenant's id space (#690).

Runs the real migration in a database of its own, created for the test and
dropped after it, so the shared test database is never migrated or written: up
to 011, rows written as the old schema held them, then up to 012.
"""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

BACKEND = os.path.join(os.path.dirname(__file__), "..", "..")
BEFORE = "011_seal_registration_tokens"
UNDER_TEST = "012_tenant_resource_id_space"


def _alembic(database_url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)}:\n{result.stdout}\n{result.stderr}"


@pytest.fixture
async def own_database(postgres_url):
    """A new, empty database on the test server; its URL. Dropped afterwards."""
    name = f"migration_012_{uuid.uuid4().hex}"
    admin = create_async_engine(postgres_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(postgres_url).set(database=name).render_as_string(hide_password=False)
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_a_row_of_a_canonical_tenant_is_canonical_and_every_other_is_descope(own_database):
    canonical_tenant = uuid.uuid4()
    rows = {
        "canonical": str(canonical_tenant),
        "descope": "T2descopeTenant",
        "uuid-of-no-canonical-tenant": str(uuid.uuid4()),
    }
    _alembic(own_database, "upgrade", BEFORE)
    engine = create_async_engine(own_database, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"),
                {"id": canonical_tenant},
            )
            for name, tenant_id in rows.items():
                await conn.execute(
                    text(
                        "INSERT INTO tenant_resources (id, tenant_id, name, description, created_at) "
                        "VALUES (:id, :tenant_id, :name, '', '2026-11-19T00:00:00')"
                    ),
                    {"id": name, "tenant_id": tenant_id, "name": name},
                )
        _alembic(own_database, "upgrade", UNDER_TEST)

        async with engine.connect() as conn:
            stored = (await conn.execute(text("SELECT name, tenant_space FROM tenant_resources"))).all()
    finally:
        await engine.dispose()

    assert dict(tuple(row) for row in stored) == {
        "canonical": "canonical",
        "descope": "descope",
        "uuid-of-no-canonical-tenant": "descope",
    }
