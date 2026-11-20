"""Migration 011 puts each stored tenant resource in its tenant's id space (#690).

Runs the real migration against the test Postgres: down to 010, rows written as
the old schema held them, and up to head again.
"""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

BACKEND = os.path.join(os.path.dirname(__file__), "..", "..")
BEFORE = "010_machine_credentials"


def _alembic(postgres_url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": postgres_url},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)}:\n{result.stdout}\n{result.stderr}"


@pytest.mark.asyncio
async def test_a_row_of_a_canonical_tenant_is_canonical_and_every_other_is_descope(postgres_url, _run_migrations):
    canonical_tenant = uuid.uuid4()
    rows = {
        "canonical": str(canonical_tenant),
        "descope": "T2descopeTenant",
        "uuid-of-no-canonical-tenant": str(uuid.uuid4()),
    }
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    _alembic(postgres_url, "downgrade", BEFORE)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
                {"id": canonical_tenant, "name": f"migration-011-{canonical_tenant}"},
            )
            for name, tenant_id in rows.items():
                await conn.execute(
                    text(
                        "INSERT INTO tenant_resources (id, tenant_id, name, description, created_at) "
                        "VALUES (:id, :tenant_id, :name, '', '2026-11-19T00:00:00')"
                    ),
                    {"id": f"migration-011-{name}", "tenant_id": tenant_id, "name": name},
                )
        _alembic(postgres_url, "upgrade", "head")

        async with engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT name, tenant_space FROM tenant_resources WHERE id LIKE 'migration-011-%'")
                )
            ).all()
        assert dict(tuple(row) for row in stored) == {
            "canonical": "canonical",
            "descope": "descope",
            "uuid-of-no-canonical-tenant": "descope",
        }
    finally:
        _alembic(postgres_url, "upgrade", "head")
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenant_resources WHERE id LIKE 'migration-011-%'"))
            await conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": canonical_tenant})
        await engine.dispose()
