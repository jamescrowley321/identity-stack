"""Alembic environment configuration — async-only, no sync engine."""

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

# Import all models so SQLModel metadata picks them up
from app.models.document import Document  # noqa: F401
from app.models.identity.assignment import UserTenantRole  # noqa: F401
from app.models.identity.provider import Provider  # noqa: F401
from app.models.identity.role import Permission, Role, RolePermission  # noqa: F401
from app.models.identity.tenant import Tenant  # noqa: F401
from app.models.identity.user import IdPLink, User  # noqa: F401
from app.models.tenant import TenantResource  # noqa: F401

target_metadata = SQLModel.metadata

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required but not set")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    # Strip +asyncpg for offline SQL generation (no driver needed)
    offline_url = DATABASE_URL.replace("+asyncpg", "")
    context.configure(
        url=offline_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode — async engine, no sync fallback."""
    connectable = create_async_engine(DATABASE_URL)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
